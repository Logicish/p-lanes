# modules/turn_log_retention.py
#
# Author:  Logicish
# Company: Logic-Ish Designs
# Date:    10/6/2026
#
# ==================================================
# Nightly size control for the turn log
# (users/<uid>/history.db, written by core/turn_log).
#
# Per user, in order:
#   1. Age trim — drop payload (full messages + think)
#      from unflagged turns older than
#      turn_log.payload_retention_days. The row itself
#      (text, reply, intent, tool, timing) is kept.
#   2. Hard cap — while used size > turn_log.max_db_mb:
#      drop oldest unflagged payloads, then (only if no
#      payloads remain) delete oldest unflagged rows.
#   3. incremental_vacuum + WAL truncate — return freed
#      pages to disk.
#   4. Log size stats so growth is visible in the logs.
#
# Flagged turns (flag != 0) are never trimmed.
# All limits live in config.yaml under turn_log.
#
# Knows about: config, core/scheduler (schedule),
#              providers (get_db).
# ==================================================

# ==================================================
# Imports
# ==================================================
from datetime import datetime, timedelta, timezone
from pathlib import Path

import structlog

import providers
from config import (
    SLOT_MAP,
    TURN_LOG_MAX_DB_MB,
    TURN_LOG_PAYLOAD_DAYS,
    TURN_LOG_RETENTION_CRON,
)
from core.scheduler import schedule

log = structlog.get_logger()

_BATCH = 500


# ==================================================
# Job
# ==================================================

@schedule(cron=TURN_LOG_RETENTION_CRON, requires_idle=False, max_duration=1800)
async def turn_log_retention():
    for user_id in SLOT_MAP:
        try:
            await trim(user_id)
        except Exception as e:
            log.error("turn_log_retention_failed", user_id=user_id, error=str(e))


async def trim(user_id: str) -> dict | None:
    db = providers.get_db(f"history:{user_id}")
    if db is None or not db.is_ready:
        return None

    cap_bytes = int(TURN_LOG_MAX_DB_MB * 1024 * 1024)
    cutoff    = (datetime.now(timezone.utc) - timedelta(days=TURN_LOG_PAYLOAD_DAYS)).isoformat()

    # 1. age trim
    await db.execute(
        "UPDATE turns SET payload = NULL, payload_bytes = 0 "
        "WHERE payload IS NOT NULL AND flag = 0 AND ts < ?",
        (cutoff,),
    )

    # 2. hard cap
    payloads_dropped = rows_deleted = 0
    while await _used_bytes(db) > cap_bytes:
        ids = await _oldest_unflagged(db, with_payload=True)
        if ids:
            await db.execute(
                f"UPDATE turns SET payload = NULL, payload_bytes = 0 "
                f"WHERE turn_id IN ({','.join('?' * len(ids))})",
                tuple(ids),
            )
            payloads_dropped += len(ids)
            continue

        ids = await _oldest_unflagged(db, with_payload=False)
        if not ids:
            log.warning("turn_log_cap_only_flagged", user_id=user_id)
            break
        await db.execute(
            f"DELETE FROM turns WHERE turn_id IN ({','.join('?' * len(ids))})",
            tuple(ids),
        )
        rows_deleted += len(ids)

    # 3. give freed pages back to the filesystem, then shrink the WAL
    await db.pragma("PRAGMA incremental_vacuum")
    await db.pragma("PRAGMA wal_checkpoint(TRUNCATE)")

    # 4. stats
    stats = await db.fetchone(
        "SELECT COUNT(*) AS turns, "
        "       COALESCE(SUM(flag != 0), 0) AS flagged, "
        "       COALESCE(SUM(payload IS NOT NULL), 0) AS with_payload, "
        "       COALESCE(SUM(payload_bytes), 0) AS payload_bytes, "
        "       MIN(ts) AS oldest "
        "FROM turns"
    ) or {}
    stats.update(
        user_id          = user_id,
        file_mb          = round(_file_bytes(db.db_path) / 1048576, 2),
        cap_mb           = TURN_LOG_MAX_DB_MB,
        payloads_dropped = payloads_dropped,
        rows_deleted     = rows_deleted,
    )
    log.info("turn_log_retention", **stats)
    return stats


# ==================================================
# Helpers
# ==================================================

async def _used_bytes(db) -> int:
    pc = (await db.pragma("PRAGMA page_count"))[0][0]
    fl = (await db.pragma("PRAGMA freelist_count"))[0][0]
    ps = (await db.pragma("PRAGMA page_size"))[0][0]
    return (pc - fl) * ps


async def _oldest_unflagged(db, with_payload: bool) -> list[str]:
    where = "flag = 0 AND payload IS NOT NULL" if with_payload else "flag = 0"
    rows = await db.fetchall(
        f"SELECT turn_id FROM turns WHERE {where} ORDER BY ts LIMIT ?", (_BATCH,),
    )
    return [r["turn_id"] for r in rows]


def _file_bytes(db_path: str) -> int:
    p = Path(db_path)
    return sum(f.stat().st_size for f in (p, p.with_name(p.name + "-wal")) if f.exists())
