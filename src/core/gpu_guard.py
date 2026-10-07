# core/gpu_guard.py
#
# Author:  Logicish
# Company: Logic-Ish Designs
# Date:    10/6/2026
#
# ==================================================
# GPU thermal + liveness guard. Acts, not just alerts.
#
# Polls nvidia-smi every `poll_seconds` (independent of
# the 5-min health poller) and drives llm's hold flag:
#
#   temp >= hold_c        → hold: new LLM calls refused,
#                           llama-server keeps running
#   temp >= kill_c        → stop llama-server + hold
#   kills/hour > max      → latched: stays down until an
#                           admin restart (/llm/restart)
#   no reading for
#     unreachable_s       → stop llama-server + hold
#   temp < resume_c       → release own holds, restart
#                           llama-server if guard stopped it
#
# Only clears holds it set. "recovery_failed" (set by llm
# after a failed backoff cycle) and "latched" need an
# admin restart.
#
# Every transition writes a notification (system.db) so
# the SPA bell shows it.
#
# Background: 10/6/2026 the GPU sat at 82–87°C for 90 min
# under a test run, then wedged (CUDA launch failure).
# Alerts fired but nothing acted, and the recovery loop
# kept relaunching llama-server against a dead card.
# ==================================================

# ==================================================
# Imports
# ==================================================
import asyncio
import time
from collections import deque

import structlog

import providers
from config import (
    GPU_GUARD_ENABLED,
    GPU_GUARD_POLL_SECONDS,
    GPU_GUARD_HOLD_C,
    GPU_GUARD_KILL_C,
    GPU_GUARD_RESUME_C,
    GPU_GUARD_UNREACHABLE_S,
    GPU_GUARD_MAX_KILLS_PER_HOUR,
    GPU_GUARD_NOTIFY_COOLDOWN_MIN,
)
from core import llm

log = structlog.get_logger()

# holds this module owns and may release on its own
_OWN_HOLDS = frozenset(["gpu_hot", "gpu_critical", "gpu_unreachable"])

# ==================================================
# State
# ==================================================

_task: asyncio.Task | None = None
_last_temp: int | None = None
_last_ok: float = 0.0                 # monotonic time of last good reading
_kills: deque[float] = deque()        # monotonic times of heat kills
_notified: dict[str, float] = {}      # notification type → monotonic time
_seen_recovery_failed = False
_pending: list[tuple] = []            # notifications waiting for system.db


def last_temp() -> int | None:
    return _last_temp


def status() -> dict:
    return {
        "enabled":    GPU_GUARD_ENABLED,
        "gpu_temp_c": _last_temp,
        "hold":       llm.hold_reason(),
        "hold_c":     GPU_GUARD_HOLD_C,
        "kill_c":     GPU_GUARD_KILL_C,
        "resume_c":   GPU_GUARD_RESUME_C,
    }


# ==================================================
# GPU read
# ==================================================

async def read_temp() -> int | None:
    # None on any failure — a wedged GPU can hang nvidia-smi,
    # so the process is killed on timeout rather than leaked
    try:
        proc = await asyncio.create_subprocess_exec(
            "nvidia-smi",
            "--query-gpu=temperature.gpu",
            "--format=csv,noheader,nounits",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
        )
    except Exception:
        return None
    try:
        stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=5)
        if proc.returncode != 0:
            return None
        return int(stdout.decode().strip().splitlines()[0])
    except asyncio.TimeoutError:
        proc.kill()
        try:
            await asyncio.wait_for(proc.wait(), timeout=2)
        except asyncio.TimeoutError:
            pass
        return None
    except Exception:
        return None


# ==================================================
# Notifications
# ==================================================

async def _notify(ntype: str, severity: str, message: str, force: bool = False):
    now = time.monotonic()
    last = _notified.get(ntype)
    if not force and last and now - last < GPU_GUARD_NOTIFY_COOLDOWN_MIN * 60:
        return
    _notified[ntype] = now

    log_fn = log.critical if severity == "critical" else log.warning
    log_fn("gpu_guard_notify", type=ntype, message=message)

    # guard starts before providers -- queue until system.db is up
    _pending.append((ntype, severity, None, message))
    await _flush()


async def _flush():
    if not _pending:
        return
    db = providers.get_db("system")
    if db is None or not db.is_ready:
        return
    while _pending:
        try:
            await db.execute(
                "INSERT INTO notifications (type, severity, host, message) VALUES (?, ?, ?, ?)",
                _pending[0],
            )
        except Exception as e:
            log.error("gpu_guard_notify_failed", error=str(e))
            return
        _pending.pop(0)


# ==================================================
# Actions
# ==================================================

async def _stop_llm(reason: str):
    llm.set_hold(reason)
    await llm.stop()


async def _resume():
    reason = llm.hold_reason()
    llm.clear_hold()
    if reason == "gpu_hot":
        log.info("gpu_guard_resumed", temp=_last_temp)
        await _notify("gpu_resumed", "info",
                      f"GPU cooled to {_last_temp}°C — LLM requests resumed", force=True)
        return

    # guard stopped llama-server — bring it back
    log.info("gpu_guard_restarting_llm", temp=_last_temp, after=reason)
    if await llm.start(llm.get_session()):
        await _notify("gpu_resumed", "info",
                      f"GPU at {_last_temp}°C — LLM restarted after {reason}", force=True)
    else:
        llm.set_hold("recovery_failed")
        await _notify("llm_recovery_failed", "critical",
                      f"LLM failed to restart after {reason} — admin restart required",
                      force=True)


async def _tick():
    global _last_temp, _last_ok, _seen_recovery_failed

    await _flush()
    hold = llm.hold_reason()

    # recovery_failed is set inside llm — surface it once per occurrence
    if hold == "recovery_failed" and not _seen_recovery_failed:
        _seen_recovery_failed = True
        await _notify("llm_recovery_failed", "critical",
                      "LLM crashed and could not be restarted — admin restart required",
                      force=True)
    elif hold != "recovery_failed":
        _seen_recovery_failed = False

    temp = await read_temp()
    now  = time.monotonic()
    _last_temp = temp

    # --- unreachable ---
    if temp is None:
        if now - _last_ok >= GPU_GUARD_UNREACHABLE_S and hold != "gpu_unreachable":
            log.critical("gpu_guard_unreachable", silent_s=round(now - _last_ok))
            if hold is None or hold in _OWN_HOLDS:
                await _stop_llm("gpu_unreachable")
            await _notify("gpu_unreachable", "critical",
                          "GPU not responding to nvidia-smi — LLM stopped", force=True)
        return
    _last_ok = now

    # --- critical: stop llama-server ---
    if temp >= GPU_GUARD_KILL_C:
        if hold in ("gpu_critical", "latched"):
            return
        if hold == "recovery_failed":
            await llm.stop()    # keep the admin-only hold, just make sure it's off
            return
        while _kills and now - _kills[0] > 3600:
            _kills.popleft()
        _kills.append(now)
        if len(_kills) > GPU_GUARD_MAX_KILLS_PER_HOUR:
            await _stop_llm("latched")
            await _notify("gpu_latched", "critical",
                          f"GPU hit {temp}°C — {len(_kills)} heat shutdowns in an hour. "
                          f"LLM stays off until an admin restart.", force=True)
        else:
            await _stop_llm("gpu_critical")
            await _notify("gpu_critical", "critical",
                          f"GPU at {temp}°C (limit {GPU_GUARD_KILL_C}°C) — LLM stopped "
                          f"until it cools below {GPU_GUARD_RESUME_C}°C", force=True)
        return

    # --- hot: refuse new work ---
    if temp >= GPU_GUARD_HOLD_C:
        if hold is None:
            llm.set_hold("gpu_hot")
            await _notify("gpu_hot", "warning",
                          f"GPU at {temp}°C (hold {GPU_GUARD_HOLD_C}°C) — pausing LLM "
                          f"requests until below {GPU_GUARD_RESUME_C}°C")
        return

    # --- cooled ---
    if temp < GPU_GUARD_RESUME_C and hold in _OWN_HOLDS:
        await _resume()


# ==================================================
# Admin restart (clears every hold)
# ==================================================

async def manual_restart() -> tuple[bool, str]:
    if GPU_GUARD_ENABLED:
        temp = await read_temp()
        if temp is None:
            return False, "GPU not responding to nvidia-smi — refusing restart"
        if temp >= GPU_GUARD_RESUME_C:
            return False, f"GPU at {temp}°C — wait until below {GPU_GUARD_RESUME_C}°C"
    _kills.clear()
    llm.clear_hold()
    log.info("gpu_guard_manual_restart", temp=_last_temp)
    ok = await llm.restart()
    if not ok:
        llm.set_hold("recovery_failed")
        return False, "LLM failed to start"
    return True, "restarted"


# ==================================================
# Lifecycle
# ==================================================

async def _loop():
    global _last_ok
    _last_ok = time.monotonic()
    while True:
        try:
            await _tick()
        except asyncio.CancelledError:
            raise
        except Exception as e:
            log.error("gpu_guard_tick_error", error=str(e))
        await asyncio.sleep(GPU_GUARD_POLL_SECONDS)


async def start():
    global _task
    if not GPU_GUARD_ENABLED:
        log.warning("gpu_guard_disabled")
        return
    _task = asyncio.create_task(_loop())
    log.info("gpu_guard_started", poll_s=GPU_GUARD_POLL_SECONDS,
             hold_c=GPU_GUARD_HOLD_C, kill_c=GPU_GUARD_KILL_C,
             resume_c=GPU_GUARD_RESUME_C)


async def stop():
    global _task
    if _task and not _task.done():
        _task.cancel()
        try:
            await _task
        except asyncio.CancelledError:
            pass
    _task = None
