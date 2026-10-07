# modules/alert_engine.py
#
# Author:  Logicish
# Company: Logic-Ish Designs
# Date:    4/21/2026
#
# ==================================================
# Scheduled module — alert threshold evaluator.
#
# Runs every 5 minutes (after health_poller writes).
# Reads the last N rows from system_health_5min /
# gpu_health_5min and evaluates each configured rule.
# Fires a notification when a rule breaches for
# `cycles` consecutive polls and the cooldown has
# elapsed and the rule is not snoozed.
#
# Alert state (cooldown anchor + snooze) lives in
# the alert_state table. Streak detection is derived
# entirely from raw 5-min rows — no counter to corrupt.
#
# Rule config lives in config.yaml under alert_rules.
# ==================================================

# ==================================================
# Imports
# ==================================================
from datetime import datetime, timezone, timedelta
from pathlib import Path

import structlog
import yaml

import providers
from core.scheduler import schedule

log = structlog.get_logger()

_CONFIG_PATH = Path(__file__).parent.parent / "config.yaml"

_RUNNING_STATUSES = frozenset(["running", "online"])


# ==================================================
# Config loader
# ==================================================

def _load_rules() -> dict:
    with open(_CONFIG_PATH) as f:
        cfg = yaml.safe_load(f)
    return cfg.get("alert_rules", {})


# ==================================================
# Metric extraction
# ==================================================

def _get_value(row: dict, metric: str, host: str | None) -> float | str | None:
    """Extract a metric value from a row, handling computed metrics."""
    if metric == "disk_pct" and host:
        used  = row.get(f"{host}_disk_used_gb")
        total = row.get(f"{host}_disk_total_gb")
        if used is not None and total and total > 0:
            return round(used / total * 100, 2)
        return None
    if host:
        return row.get(f"{host}_{metric}")
    return row.get(metric)


# ==================================================
# Condition evaluation
# ==================================================

def _breaches(row: dict, rule: dict, host: str | None) -> bool:
    """Return True if this single row breaches the rule condition."""
    condition = rule.get("condition", "gt")

    if condition == "status_not_running":
        status = row.get(f"{host}_status") if host else row.get("status")
        if not status:
            return False
        return status not in _RUNNING_STATUSES

    val = _get_value(row, rule.get("metric", ""), host)
    if condition == "missing":
        return val is None
    if val is None:
        return False

    if condition == "gt":
        return float(val) > float(rule["threshold"])
    if condition == "lt":
        return float(val) < float(rule["threshold"])
    if condition == "eq":
        return val == rule["value"]

    return False


# ==================================================
# Message builder
# ==================================================

def _build_message(rule_name: str, rule: dict, host: str | None, row: dict) -> str:
    try:
        if rule_name == "container_down":
            status = row.get(f"{host}_status", "unknown")
            return f"{host} container is not responding (status: {status})"
        if rule_name == "llm_down":
            return "LLM server (llama.cpp) is not running or not answering"
        if rule_name in ("disk_warning", "disk_critical"):
            val = _get_value(row, "disk_pct", host)
            pct = f"{val:.1f}%" if val is not None else "unknown"
            return f"{host} disk at {pct} (threshold: {rule['threshold']}%)"
        if rule_name == "cpu_spike":
            val = _get_value(row, "cpu_pct", host)
            pct = f"{val:.1f}%" if val is not None else "unknown"
            return f"{host} CPU sustained above {rule['threshold']}% — currently {pct}"
        if rule_name == "mem_warning":
            val = _get_value(row, "mem_pct", host)
            pct = f"{val:.1f}%" if val is not None else "unknown"
            return f"{host} memory at {pct} (threshold: {rule['threshold']}%)"
        if rule_name == "gpu_unreachable":
            return "GPU not responding to nvidia-smi (driver or card failure)"
        if rule_name == "gpu_temp":
            temp = row.get("gpu_temp_c", "unknown")
            return f"GPU temperature at {temp}°C (threshold: {rule['threshold']}°C)"
    except Exception:
        pass
    return f"{rule_name}" + (f" on {host}" if host else "")


# ==================================================
# Scheduled job
# ==================================================

@schedule(cron="*/5 * * * *", requires_idle=False)
async def evaluate():
    db = providers.get_db("system")
    if db is None or not db.is_ready:
        return

    rules_cfg = _load_rules()
    if not rules_cfg.get("enabled", True):
        return

    now     = datetime.now(timezone.utc)
    now_str = now.isoformat()

    for rule_name, rule in rules_cfg.get("rules", {}).items():
        cycles     = int(rule.get("cycles", 1))
        cooldown_h = float(rule.get("cooldown_h", 1.0))
        severity   = rule.get("severity", "warning")
        hosts      = rule.get("hosts")        # None = system-wide
        source     = rule.get("source", "system")

        table = "system_health_5min" if source == "system" else "gpu_health_5min"

        rows = await db.fetchall(
            f"SELECT * FROM {table} ORDER BY ts DESC LIMIT ?",
            (cycles,),
        )

        if len(rows) < cycles:
            continue  # not enough data yet — wait for poller to accumulate

        host_list: list[str | None] = hosts if hosts else [None]

        for host in host_list:
            rule_id   = f"{rule_name}:{host}" if host else rule_name
            all_breach = all(_breaches(row, rule, host) for row in rows)

            if not all_breach:
                # condition cleared — reset active_since so next breach is fresh
                await db.execute(
                    "UPDATE alert_state SET active_since = NULL WHERE rule_id = ?",
                    (rule_id,),
                )
                continue

            # --- condition is breaching for all `cycles` consecutive polls ---
            state = await db.fetchone(
                "SELECT * FROM alert_state WHERE rule_id = ?", (rule_id,)
            )

            # check snooze
            if state and state.get("snoozed_until"):
                try:
                    snooze_dt = datetime.fromisoformat(state["snoozed_until"])
                    if now < snooze_dt:
                        continue
                except ValueError:
                    pass

            # check cooldown
            if state and state.get("last_alerted"):
                try:
                    last_dt = datetime.fromisoformat(state["last_alerted"])
                    if now - last_dt < timedelta(hours=cooldown_h):
                        continue
                except ValueError:
                    pass

            # fire
            message = _build_message(rule_name, rule, host, rows[0])
            await db.execute(
                "INSERT INTO notifications (type, severity, host, message) VALUES (?, ?, ?, ?)",
                (rule_name, severity, host, message),
            )

            # upsert alert_state — preserve active_since if already set
            await db.execute(
                """
                INSERT INTO alert_state (rule_id, active_since, last_alerted)
                VALUES (?, ?, ?)
                ON CONFLICT(rule_id) DO UPDATE SET
                    active_since = COALESCE(alert_state.active_since, excluded.active_since),
                    last_alerted = excluded.last_alerted
                """,
                (rule_id, now_str, now_str),
            )

            log.info(
                "alert_fired",
                rule_id=rule_id,
                severity=severity,
                message=message,
            )
