# modules/system_health.py
#
# Author:  Logicish
# Company: Logic-Ish Designs
# Date:    4/15/2026
#
# ==================================================
# Scheduled module — hourly system health snapshot.
#
# Container metrics (cpu, mem, disk, net, uptime, status)
# are sourced from HA via the Proxmox integration sensors.
# GPU metrics (VRAM, temp, utilization) are sourced from
# nvidia-smi on the local host and written to gpu_health.
#
# Schedule: every hour at :00
# Idle gate: disabled — runs regardless of user activity.
#
# Hosts polled: brain, ears, voice, omen, artist
# Each host contributes 10 columns (prefixed by host name).
# Artist is normally stopped — NULL values recorded when down.
#
# GPU note: nvidia-smi reports full physical GPU usage.
# The GPU is exposed to multiple containers but there is only
# one device; gpu_health is a separate table with no host column.
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

_RETENTION_DAYS = 90

_HOSTS = ["brain", "ears", "voice", "omen", "artist"]

# HA sensor suffix → column suffix mapping (value types are all numeric except status)
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
    """Fetch all sensor states and extract per-host metrics.
    Returns {host: {col: value}} with None for any missing/unknown sensor."""
    states = await ha.get_states(domains=["sensor"])
    lookup: dict[str, str] = {s["entity_id"]: s["state"] for s in states}

    result: dict[str, dict[str, object]] = {}
    for host in _HOSTS:
        row: dict[str, object] = {}
        for sensor_suffix, col in _SENSOR_COLS:
            entity_id = f"sensor.{host}_{sensor_suffix}"
            raw = lookup.get(entity_id)
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
# GPU stats (nvidia-smi)
# ==================================================

async def _gpu_stats() -> tuple[int | None, int | None, int | None, int | None]:
    """Query nvidia-smi for VRAM, temperature, and utilization.
    Returns (vram_used_mb, vram_total_mb, temp_c, util_pct)."""
    try:
        proc = await asyncio.create_subprocess_exec(
            "nvidia-smi",
            "--query-gpu=memory.used,memory.total,temperature.gpu,utilization.gpu",
            "--format=csv,noheader,nounits",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
        )
        stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=5)
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

@schedule(cron="0 * * * *", requires_idle=False)
async def collect():
    db = providers.get_db("system")
    if db is None or not db.is_ready:
        log.warning("system_health_skip_no_db")
        return

    ha = providers.get_provider("homeassistant")
    if ha is None or not ha.is_ready:
        log.warning("system_health_skip_no_ha")
        return

    ts = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")

    host_data = await _ha_stats(ha)
    vram_used, vram_total, gpu_temp, gpu_util = await _gpu_stats()
    llm_running = llm.is_running()

    # --- system_health row (one wide row per snapshot) ---
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
    col_list     = ", ".join(cols)
    await db.execute(
        f"INSERT INTO system_health ({col_list}) VALUES ({placeholders})",
        tuple(vals),
    )

    # --- gpu_health row ---
    await db.execute(
        """
        INSERT INTO gpu_health (ts, vram_used_mb, vram_total_mb, gpu_temp_c, gpu_util_pct)
        VALUES (?, ?, ?, ?, ?)
        """,
        (ts, vram_used, vram_total, gpu_temp, gpu_util),
    )

    # --- prune both tables ---
    prune_clause = (f"-{_RETENTION_DAYS} days",)
    await db.execute(
        "DELETE FROM system_health WHERE ts < datetime('now', ?)", prune_clause
    )
    await db.execute(
        "DELETE FROM gpu_health WHERE ts < datetime('now', ?)", prune_clause
    )

    log.info(
        "system_health_recorded",
        ts=ts,
        brain_cpu_pct=host_data["brain"].get("cpu_pct"),
        brain_mem_pct=host_data["brain"].get("mem_pct"),
        vram_used_mb=vram_used,
        gpu_temp_c=gpu_temp,
        llm_running=llm_running,
    )
