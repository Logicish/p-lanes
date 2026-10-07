# modules/daily_rollup.py
#
# Author:  Logicish
# Company: Logic-Ish Designs
# Date:    4/21/2026
#
# ==================================================
# Scheduled module — daily health rollup.
#
# Runs at 0005 UTC (5 min after midnight) each day.
# Aggregates the previous day's system_health_5min and
# gpu_health_5min rows into a single health_daily row.
#
# Aggregation strategy per metric:
#   cpu / mem pct    → avg + peak (max)
#   disk_used_gb     → end-of-day (last row)
#   net_in/out_gb    → delta (last - first; cumulative counters)
#   uptime_h         → min (lowest value = reboot occurred)
#   status           → end-of-day (last row)
#   gpu vram/temp/util → avg + peak
#   llm_running      → uptime % (fraction of samples = 1)
#
# 2-year rolling window on health_daily (730 days).
# ==================================================

# ==================================================
# Imports
# ==================================================
from datetime import datetime, timezone, timedelta

import structlog

import providers
from core.scheduler import schedule

log = structlog.get_logger()

_HOSTS           = ["brain", "ears", "voice", "omen", "artist"]
_RETENTION_DAYS  = 730


# ==================================================
# Aggregation helpers
# ==================================================

def _avg(vals: list) -> float | None:
    return round(sum(vals) / len(vals), 4) if vals else None

def _peak(vals: list) -> float | None:
    return max(vals) if vals else None

def _low(vals: list) -> float | None:
    return min(vals) if vals else None

def _delta(vals: list) -> float | None:
    return round(vals[-1] - vals[0], 4) if len(vals) >= 2 else None

def _eod(vals: list):
    return vals[-1] if vals else None

def _nonnull(rows: list, key: str) -> list:
    return [r[key] for r in rows if r.get(key) is not None]


# ==================================================
# Scheduled job
# ==================================================

@schedule(cron="5 0 * * *", requires_idle=False)
async def rollup():
    db = providers.get_db("system")
    if db is None or not db.is_ready:
        log.warning("daily_rollup_skip_no_db")
        return

    yesterday = (datetime.now(timezone.utc) - timedelta(days=1)).strftime("%Y-%m-%d")

    existing = await db.fetchone(
        "SELECT 1 FROM health_daily WHERE date = ?", (yesterday,)
    )
    if existing:
        log.info("daily_rollup_already_done", date=yesterday)
        return

    rows_sys = await db.fetchall(
        "SELECT * FROM system_health_5min WHERE date(ts) = ? ORDER BY ts",
        (yesterday,),
    )
    rows_gpu = await db.fetchall(
        "SELECT * FROM gpu_health_5min WHERE date(ts) = ? ORDER BY ts",
        (yesterday,),
    )

    if not rows_sys:
        log.warning("daily_rollup_no_data", date=yesterday)
        return

    rec: dict = {"date": yesterday, "sample_count": len(rows_sys)}

    # --- per-host aggregates ---
    for host in _HOSTS:
        cpu_vals     = _nonnull(rows_sys, f"{host}_cpu_pct")
        mem_vals     = _nonnull(rows_sys, f"{host}_mem_pct")
        disk_vals    = _nonnull(rows_sys, f"{host}_disk_used_gb")
        net_in_vals  = _nonnull(rows_sys, f"{host}_net_in_gb")
        net_out_vals = _nonnull(rows_sys, f"{host}_net_out_gb")
        uptime_vals  = _nonnull(rows_sys, f"{host}_uptime_h")

        rec[f"{host}_cpu_avg"]        = _avg(cpu_vals)
        rec[f"{host}_cpu_peak"]       = _peak(cpu_vals)
        rec[f"{host}_mem_avg"]        = _avg(mem_vals)
        rec[f"{host}_mem_peak"]       = _peak(mem_vals)
        rec[f"{host}_disk_eod"]       = _eod(disk_vals)
        rec[f"{host}_net_in_delta"]   = _delta(net_in_vals)
        rec[f"{host}_net_out_delta"]  = _delta(net_out_vals)
        rec[f"{host}_uptime_min"]     = _low(uptime_vals)
        rec[f"{host}_status_eod"]     = rows_sys[-1].get(f"{host}_status")

    # --- GPU aggregates ---
    if rows_gpu:
        vram_vals = _nonnull(rows_gpu, "vram_used_mb")
        temp_vals = _nonnull(rows_gpu, "gpu_temp_c")
        util_vals = _nonnull(rows_gpu, "gpu_util_pct")
        rec["gpu_vram_avg"]  = _avg(vram_vals)
        rec["gpu_vram_peak"] = _peak(vram_vals)
        rec["gpu_temp_avg"]  = _avg(temp_vals)
        rec["gpu_temp_peak"] = _peak(temp_vals)
        rec["gpu_util_avg"]  = _avg(util_vals)
        rec["gpu_util_peak"] = _peak(util_vals)

    # --- LLM uptime % ---
    llm_vals = [r["llm_running"] for r in rows_sys]
    rec["llm_uptime_pct"] = round(sum(llm_vals) / len(llm_vals) * 100, 1) if llm_vals else None

    # --- insert ---
    cols = list(rec.keys())
    vals = [rec[c] for c in cols]
    await db.execute(
        f"INSERT OR IGNORE INTO health_daily ({', '.join(cols)}) VALUES ({', '.join('?' * len(vals))})",
        tuple(vals),
    )

    # --- prune health_daily to 2-year window ---
    await db.execute(
        "DELETE FROM health_daily WHERE date < date('now', ?)",
        (f"-{_RETENTION_DAYS} days",),
    )

    log.info("daily_rollup_done", date=yesterday, samples=len(rows_sys))
