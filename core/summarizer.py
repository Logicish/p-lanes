# core/summarizer.py
#
# Author:  Logicish
# Company: Logic-Ish Designs
# Date:    2/26/2026
#
# ==================================================
# Conversation summarization and context management.
# Generates summary via LLM utility slot, wipes slot
# KV cache, reinjects system prompt + persona +
# summary + recent messages.
#
# Lock behavior:
#   flag_crit → fire immediately as background task
#   flag_warn + is_idle → background timer picks it up
#   If slot_lock held when message arrives → wait up
#   to SUMMARIZE_LOCK_WAIT seconds, then drop.
#
# Scheduled behavior:
#   Timed summary via cron — summarize all, then
#   optionally restart LLM to defragment KV cache.
#
# Background loop also runs proactive health checks
# on the LLM server and triggers recovery if needed.
#
# Knows about: config (thresholds, lock wait, scheduled
#              settings, idle interval), slots (User,
#              save), llm (utility call, health, recovery).
# ==================================================

# ==================================================
# Imports
# ==================================================
import asyncio

import structlog

from config import (
    SUMMARIZE_LOCK_WAIT,
    KEEP_RECENT,
    SCHEDULED_SUMMARY,
    IDLE_CHECK_INTERVAL,
)
from core.slots import User, save_profile, get_all_users

log = structlog.get_logger()

# ==================================================
# Scheduled Summary Task
# ==================================================

_scheduler_task: asyncio.Task | None = None


async def start_scheduler():
    # start the scheduled summary background loop
    global _scheduler_task
    if not SCHEDULED_SUMMARY.get("enabled", False):
        log.info("scheduled_summary_disabled")
        return

    cron_str = SCHEDULED_SUMMARY.get("cron", "0 2 * * *")
    _scheduler_task = asyncio.create_task(_scheduler_loop(cron_str))
    log.info("scheduled_summary_started", cron=cron_str)


async def stop_scheduler():
    global _scheduler_task
    if _scheduler_task and not _scheduler_task.done():
        _scheduler_task.cancel()
        try:
            await _scheduler_task
        except asyncio.CancelledError:
            pass
    _scheduler_task = None
    log.info("scheduled_summary_stopped")


async def _scheduler_loop(cron_str: str):
    # simple cron-like loop — parses "M H * * *" format
    # only supports hour and minute for daily scheduling
    parts = cron_str.split()
    target_minute = int(parts[0]) if parts[0] != "*" else 0
    target_hour   = int(parts[1]) if parts[1] != "*" else 2

    while True:
        try:
            from datetime import datetime, timedelta
            dt = datetime.now()

            # calculate seconds until next target time
            target = dt.replace(
                hour=target_hour, minute=target_minute, second=0, microsecond=0
            )
            if target <= dt:
                target += timedelta(days=1)

            wait_seconds = (target - dt).total_seconds()
            log.info("scheduler_next_run",
                     target=target.isoformat(), wait_seconds=int(wait_seconds))

            await asyncio.sleep(wait_seconds)
            await _run_scheduled_summary()

        except asyncio.CancelledError:
            raise
        except Exception as e:
            log.error("scheduler_error", error=str(e))
            await asyncio.sleep(60)  # back off on error


async def _run_scheduled_summary():
    log.info("scheduled_summary_triggered")
    users = get_all_users()

    for uid, user in users.items():
        if uid == "utility":
            continue
        try:
            async with user.slot_lock:
                await _do_summarize(user)
        except Exception as e:
            log.error("scheduled_summary_user_failed",
                      user_id=uid, error=str(e))

    log.info("scheduled_summary_complete")

    # restart LLM if configured (defragments KV cache)
    if SCHEDULED_SUMMARY.get("restart_llm", False):
        log.info("scheduled_llm_restart")
        from core import llm
        await llm.restart()


# ==================================================
# Public API
# ==================================================

async def summarize_if_needed(user: User):
    # fire-and-forget background task
    # acquires slot_lock, summarizes, saves profile
    if user.slot_lock.locked():
        log.info("slot_lock_held_skip", user_id=user.user_id)
        return

    try:
        async with user.slot_lock:
            log.info("summarizing", user_id=user.user_id, slot=user.slot)
            await _do_summarize(user)
            log.info("summarization_complete", user_id=user.user_id)
    except Exception as e:
        log.error("summarization_failed", user_id=user.user_id, error=str(e))


async def check_idle_summarize(user: User):
    # called periodically — if flag_warn and user is idle, summarize
    if user.flag_warn and user.is_idle():
        log.info("idle_summarize_triggered", user_id=user.user_id)
        await summarize_if_needed(user)


async def wait_for_lock(user: User) -> bool:
    if not user.slot_lock.locked():
        return True

    log.info("waiting_for_slot_lock", user_id=user.user_id)
    for _ in range(SUMMARIZE_LOCK_WAIT):
        await asyncio.sleep(1)
        if not user.slot_lock.locked():
            return True

    log.warning("slot_lock_timeout",
                user_id=user.user_id, timeout=SUMMARIZE_LOCK_WAIT)
    return False


# ==================================================
# Background Loop — Idle Checks + Health Monitor
# ==================================================

_background_task: asyncio.Task | None = None


async def start_background_loop():
    global _background_task
    _background_task = asyncio.create_task(
        _background_loop(IDLE_CHECK_INTERVAL)
    )
    log.info("background_loop_started", interval=IDLE_CHECK_INTERVAL)


async def stop_background_loop():
    global _background_task
    if _background_task and not _background_task.done():
        _background_task.cancel()
        try:
            await _background_task
        except asyncio.CancelledError:
            pass
    _background_task = None
    log.info("background_loop_stopped")


async def _background_loop(interval: int):
    while True:
        try:
            await asyncio.sleep(interval)

            # proactive LLM health check
            await _check_llm_health()

            # idle summarization checks
            users = get_all_users()
            for uid, user in users.items():
                if user.flag_warn and user.is_idle():
                    await summarize_if_needed(user)

        except asyncio.CancelledError:
            raise
        except Exception as e:
            log.error("background_loop_error", error=str(e))


async def _check_llm_health():
    # lightweight proactive health check — if the LLM
    # server has died, attempt recovery before any user
    # hits a dead server on their next message
    from core import llm

    if not llm.is_running():
        log.warning("background_health_llm_not_running")
        await llm.attempt_recovery()
        return

    healthy = await llm.health_check()
    if not healthy:
        log.warning("background_health_check_failed")
        await llm.attempt_recovery()


# ==================================================
# Internal
# ==================================================

async def _do_summarize(user: User):
    if len(user.conversation_history) < 2:
        log.info("not_enough_history", user_id=user.user_id)
        return

    history_text = _format_history(user.conversation_history)
    existing_summary = user.summary or "(No previous summary)"

    summary_messages = [
        {
            "role": "system",
            "content": (
                "You are a summarization assistant. Produce a concise summary "
                "of the conversation below. Preserve key facts, decisions, and "
                "context the user would need if the conversation continued. "
                "Keep it under 300 words."
            ),
        },
        {
            "role": "user",
            "content": (
                f"Previous summary:\n{existing_summary}\n\n"
                f"New conversation:\n{history_text}\n\n"
                "Produce an updated summary."
            ),
        },
    ]

    try:
        from core import llm
        result = await llm.call_utility(
            messages=summary_messages,
            temperature=0.3,
            max_tokens=512,
        )
        new_summary = result.content
    except Exception as e:
        log.error("summarize_llm_error", error=str(e))
        return

    recent = user.conversation_history[-KEEP_RECENT:]

    user.summary = new_summary
    user.conversation_history = recent
    user.flag_warn = False
    user.flag_crit = False

    save_profile(user)
    log.info("summary_updated",
             user_id=user.user_id, kept_recent=len(recent))


def _format_history(history: list[dict]) -> str:
    lines = []
    for msg in history:
        role = msg["role"].capitalize()
        lines.append(f"{role}: {msg['content']}")
    return "\n".join(lines)