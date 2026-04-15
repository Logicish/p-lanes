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
import json
import re

import aiohttp
import structlog

from config import LLM_URL
from core.broadcast import publish
from core.events import register
from core.pipeline import PipelineContext

log = structlog.get_logger()

# ==================================================
# LLM extraction prompt
# ==================================================

_TIMER_SYSTEM = """\
You are a timer parser. Extract the duration from the user message.
Respond ONLY with a single line of valid JSON. No explanation, no markdown.

Format: {"seconds": 300, "label": "pasta"}

Rules:
- seconds: total duration in seconds (integer)
- label: short description of what the timer is for, or "" if not specified
- Convert minutes/hours: "5 minutes" = 300, "1 hour" = 3600, "90 seconds" = 90
- If no duration can be determined, respond: {"error": "unclear"}\
"""

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


async def _extract_timer(message: str) -> dict | None:
    payload = {
        "model":       "local",
        "messages":    [
            {"role": "system", "content": _TIMER_SYSTEM},
            {"role": "user",   "content": message},
        ],
        "temperature": 0.1,
        "max_tokens":  64,
    }
    try:
        async with aiohttp.ClientSession(
            timeout=aiohttp.ClientTimeout(total=10)
        ) as session:
            async with session.post(LLM_URL, json=payload) as resp:
                if resp.status != 200:
                    return None
                data = await resp.json()
                text = data["choices"][0]["message"]["content"].strip()
                return json.loads(text)
    except json.JSONDecodeError:
        return None
    except Exception as e:
        log.error("timer_extract_failed", error=str(e))
        return None


async def _run_timer(user_id: str, seconds: int, label: str) -> None:
    """Sleep then push an alert to the user via broadcast."""
    await asyncio.sleep(seconds)
    msg = f"Timer done" + (f" — {label}" if label else "") + "."
    publish(user_id, {"event": "response", "data": msg})
    log.info("timer_fired", user_id=user_id, seconds=seconds, label=label)


# ==================================================
# Classifier
# ==================================================

@register("timer", "classifier")
async def handle(ctx: PipelineContext) -> PipelineContext:
    if ctx.intent != "timer_alarm":
        return ctx

    ctx.skip_processor = True

    parsed = await _extract_timer(ctx.raw_message)

    if parsed is None or "error" in parsed:
        ctx.response_text = "I couldn't figure out the duration. Try something like 'set a timer for 10 minutes'."
        return ctx

    seconds = int(parsed.get("seconds", 0))
    label   = parsed.get("label", "").strip()

    if seconds <= 0:
        ctx.response_text = "That doesn't seem like a valid duration."
        return ctx

    asyncio.create_task(_run_timer(ctx.user.user_id, seconds, label))

    duration = _friendly_duration(seconds)
    ctx.response_text = f"Timer set for {duration}" + (f" — {label}" if label else "") + "."

    log.info("timer_started", user_id=ctx.user.user_id, seconds=seconds, label=label)
    return ctx
