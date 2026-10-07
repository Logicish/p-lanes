# modules/health_poller.py
#
# Author:  Logicish
# Company: Logic-Ish Designs
# Date:    4/21/2026
#
# ==================================================
# Scheduled module — 5-minute system health snapshot.
#
# Same data sources as system_health (hourly) but at
# 5-minute resolution. Feeds alert_engine (threshold
# evaluation) and daily_rollup (aggregation).
#
# Writes to: system_health_5min, gpu_health_5min
# Retention: 7 days (alert_engine uses last N rows;
#            daily_rollup consumes and they age out)
#
# Schedule: every 5 minutes
# Idle gate: disabled — runs regardless of user activity.
# ==================================================

# ==================================================
# Imports
# ==================================================
import asyncio
from datetime import datetime, timezone

import structlog

import providers
from core import llm
from core.scheduler import schedule

log = structlog.get_logger()

_RETENTION_DAYS = 7

_HOSTS = ["brain", "ears", "voice", "omen", "artist"]

_SENSOR_COLS = [
    ("cpu_usage",               "cpu_pct"),
    ("memory_usage",            "mem_used_gb"),
    ("max_memory_usage",        "mem_total_gb"),
    ("memory_usage_percentage", "mem_pct"),
    ("disk_usage",              "disk_used_gb"),
    ("max_disk_usage",          "disk_total_gb"),
    ("network_input",           "net_in_gb"),
    ("network_output",          "net_out_gb"),
    ("uptime",                  "uptime_h"),
    ("status",                  "status"),
]


# ==================================================
# HA container stats
# ==================================================

async def _ha_stats(ha) -> dict[str, dict[str, object]]:
    states = await ha.get_states(domains=["sensor"])
    lookup: dict[str, str] = {s["entity_id"]: s["state"] for s in states}
    result: dict[str, dict[str, object]] = {}
    for host in _HOSTS:
        row: dict[str, object] = {}
        for sensor_suffix, col in _SENSOR_COLS:
            raw = lookup.get(f"sensor.{host}_{sensor_suffix}")
            if raw is None or raw in ("unknown", "unavailable", ""):
                row[col] = None
            elif col == "status":
                row[col] = raw
            else:
                try:
                    row[col] = round(float(raw), 4)
                except (ValueError, TypeError):
                    row[col] = None
        result[host] = row
    return result


# ==================================================
# GPU stats
# ==================================================

async def _gpu_stats() -> tuple[int | None, int | None, int | None, int | None]:
    try:
        proc = await asyncio.create_subprocess_exec(
            "nvidia-smi",
            "--query-gpu=memory.used,memory.total,temperature.gpu,utilization.gpu",
            "--format=csv,noheader,nounits",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
        )
        try:
            stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=5)
        except asyncio.TimeoutError:
            proc.kill()     # wedged GPU can hang nvidia-smi — don't leak it
            return None, None, None, None
        parts = stdout.decode().strip().split(",")
        if len(parts) == 4:
            return (
                int(parts[0].strip()),
                int(parts[1].strip()),
                int(parts[2].strip()),
                int(parts[3].strip()),
            )
    except Exception:
        pass
    return None, None, None, None


# ==================================================
# Scheduled job
# ==================================================

@schedule(cron="*/5 * * * *", requires_idle=False)
async def poll():
    db = providers.get_db("system")
    if db is None or not db.is_ready:
        log.warning("health_poller_skip_no_db")
        return

    ha = providers.get_provider("homeassistant")
    if ha is None or not ha.is_ready:
        log.warning("health_poller_skip_no_ha")
        return

    ts = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")

    host_data = await _ha_stats(ha)
    vram_used, vram_total, gpu_temp, gpu_util = await _gpu_stats()
    # process alive AND answering — a process stuck in a crash/restart
    # loop used to read as running (10/6/2026)
    llm_running = llm.is_running() and await llm.health_check()

    # --- system_health_5min ---
    cols = ["ts"]
    vals: list = [ts]
    for host in _HOSTS:
        row = host_data[host]
        for _, col in _SENSOR_COLS:
            cols.append(f"{host}_{col}")
            vals.append(row[col])
    cols.append("llm_running")
    vals.append(1 if llm_running else 0)

    placeholders = ", ".join("?" * len(vals))
    await db.execute(
        f"INSERT INTO system_health_5min ({', '.join(cols)}) VALUES ({placeholders})",
        tuple(vals),
    )

    # --- gpu_health_5min ---
    await db.execute(
        """
        INSERT INTO gpu_health_5min (ts, vram_used_mb, vram_total_mb, gpu_temp_c, gpu_util_pct)
        VALUES (?, ?, ?, ?, ?)
        """,
        (ts, vram_used, vram_total, gpu_temp, gpu_util),
    )

    # --- prune ---
    prune = (f"-{_RETENTION_DAYS} days",)
    await db.execute("DELETE FROM system_health_5min WHERE ts < datetime('now', ?)", prune)
    await db.execute("DELETE FROM gpu_health_5min WHERE ts < datetime('now', ?)", prune)

    log.debug(
        "health_poller_recorded",
        ts=ts,
        brain_cpu_pct=host_data["brain"].get("cpu_pct"),
        llm_running=llm_running,
    )
