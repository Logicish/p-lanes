# modules/timer.py
#
# Author:  Logicish
# Company: Logic-Ish Designs
# Date:    4/2/2026
#
# ==================================================
# Local timer/alarm module.
# Intercepts the 'timer_alarm' intent and runs timers
# locally via asyncio. When the timer fires, it pushes
# an alert back to the user via the broadcast bus.
#
# Timers are per-user and run in background tasks.
# Multiple concurrent timers per user are supported.
# Cancellation is not yet implemented.
#
# TODO: when HA timer entities are added to HAOS,
# consider routing timers through HA instead so they
# survive a p-lanes restart. For now, local asyncio
# is simple and sufficient.
#
# Security: USER (1) via module_permissions — guest
# cannot set timers.
#
# Knows about: core/events (register),
#              core/pipeline (PipelineContext),
#              core/broadcast (publish),
#              config (LLM_URL).
# ==================================================

# ==================================================
# Imports
# ==================================================
import asyncio
import datetime

import structlog

from core.broadcast import publish
from core.pipeline import PipelineContext
from core.tool_registry import tool

log = structlog.get_logger()

# ==================================================
# Helpers
# ==================================================

def _friendly_duration(seconds: int) -> str:
    if seconds < 60:
        return f"{seconds} second{'s' if seconds != 1 else ''}"
    minutes = seconds // 60
    rem     = seconds % 60
    if minutes < 60:
        s = f"{minutes} minute{'s' if minutes != 1 else ''}"
        if rem:
            s += f" {rem}s"
        return s
    hours   = minutes // 60
    rem_min = minutes % 60
    s = f"{hours} hour{'s' if hours != 1 else ''}"
    if rem_min:
        s += f" {rem_min} minute{'s' if rem_min != 1 else ''}"
    return s



async def _run_timer(user_id: str, seconds: int, label: str) -> None:
    """Sleep then push an alert to the user via broadcast."""
    await asyncio.sleep(seconds)
    msg = f"Timer done" + (f" — {label}" if label else "") + "."
    publish(user_id, {"event": "response", "data": msg})
    log.info("timer_fired", user_id=user_id, seconds=seconds, label=label)



def _seconds_until(target: str) -> int | None:
    """Parse HH:MM target time and return seconds until it fires today."""
    try:
        h, m   = map(int, target.strip().split(":"))
        now    = datetime.datetime.now()
        target_dt = now.replace(hour=h, minute=m, second=0, microsecond=0)
        delta  = int((target_dt - now).total_seconds())
        return delta if delta > 0 else delta + 86400   # wrap to next day if past
    except Exception:
        return None


@tool("set_timer")
async def _execute(args: dict, ctx: PipelineContext) -> str:
    duration_seconds = args.get("duration_seconds")
    target_time      = (args.get("target_time") or "").strip()
    label            = (args.get("label") or "").strip()

    if duration_seconds is not None:
        seconds = int(duration_seconds)
        if seconds <= 0:
            return "That doesn't seem like a valid duration."
        asyncio.create_task(_run_timer(ctx.user.user_id, seconds, label))
        log.info("set_timer_tool_ok", seconds=seconds, label=label)
        return f"Timer set for {_friendly_duration(seconds)}" + (f" — {label}" if label else "") + "."

    if target_time:
        seconds = _seconds_until(target_time)
        if seconds is None:
            return f"I couldn't parse the target time '{target_time}'."
        asyncio.create_task(_run_timer(ctx.user.user_id, seconds, label or "alarm"))
        log.info("set_timer_alarm_tool_ok", target_time=target_time, seconds=seconds)
        return f"Alarm set for {target_time}" + (f" — {label}" if label else "") + "."

    return "I need a duration or target time for the timer."
