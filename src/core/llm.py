# core/llm.py
#
# Author:  Logicish
# Company: Logic-Ish Designs
# Date:    2/26/2026
#
# ==================================================
# llama.cpp server process lifecycle and all LLM
# communication. Start, stop, call, parse response.
# Token tracking from usage -- never accumulated.
# Supports blocking, streaming (SSE), and internal
# slot calls (no conversation history impact).
#
# Crash recovery: reactive (inside call/stream when
# server is unreachable) and proactive (health check
# called from background loop in summarizer).
#
# Context overflow: detects when a slot exceeds its
# context window and raises LLMContextOverflow so the
# caller can trigger summarization and retry.
#
# Knows about: config (LLM settings, thresholds,
#              paths, recovery), slots (User object).
# ==================================================

# ==================================================
# Imports
# ==================================================
import asyncio
import json
import os
import subprocess
import time
from typing import AsyncIterator

import aiohttp
import structlog

from config import (
    LLM_CMD,
    LLM_URL,
    LLM_HEALTH_URL,
    LLM_STARTUP_TIMEOUT,
    THRESHOLD_WARN,
    THRESHOLD_CRIT,
    CONTEXT_PER_SLOT,
    RECOVERY_MAX_RETRIES,
    RECOVERY_INITIAL_WAIT,
    RECOVERY_MAX_WAIT,
    SLOT_MAP,
    UTILITY_ENABLED,
)
from core.gates import release_summarize_gate
from core.slots import User

log = structlog.get_logger()

# ==================================================
# Process State
# ==================================================

_process: subprocess.Popen | None = None
_env = {**os.environ, "LD_LIBRARY_PATH": "/opt/llama.cpp/build/bin"}
_session: aiohttp.ClientSession | None = None

# recovery lock prevents multiple concurrent restart attempts
_recovery_lock = asyncio.Lock()

# ==================================================
# Session Management (shared across system)
# ==================================================

def set_session(session: aiohttp.ClientSession):
    global _session
    _session = session


def get_session() -> aiohttp.ClientSession:
    if _session is None:
        raise RuntimeError("LLM session not initialized -- call set_session() first")
    return _session


# ==================================================
# Process Management
# ==================================================

async def start(session: aiohttp.ClientSession) -> bool:
    global _process
    set_session(session)

    if _process and _process.poll() is None:
        log.info("llm_already_running", pid=_process.pid)
        return True

    log.info("llm_starting")
    try:
        _process = subprocess.Popen(
            LLM_CMD,
            env=_env,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
    except Exception as e:
        log.error("llm_launch_failed", error=str(e))
        return False

    deadline = time.time() + LLM_STARTUP_TIMEOUT
    while time.time() < deadline:
        if await health_check():
            log.info("llm_ready", pid=_process.pid)
            return True
        await asyncio.sleep(1)

    log.error("llm_startup_timeout", timeout=LLM_STARTUP_TIMEOUT)
    return False


async def stop() -> None:
    global _process
    if not _process or _process.poll() is not None:
        return

    pid = _process.pid
    log.info("llm_stopping", pid=pid)
    _process.terminate()
    try:
        loop = asyncio.get_event_loop()
        await loop.run_in_executor(None, lambda: _process.wait(timeout=10))
        log.info("llm_stopped_clean")
    except subprocess.TimeoutExpired:
        log.warning("llm_force_kill", pid=pid)
        _process.kill()
    _process = None


async def restart() -> bool:
    log.info("llm_restarting")
    await stop()
    await asyncio.sleep(2)
    return await start(get_session())


def is_running() -> bool:
    return _process is not None and _process.poll() is None


def get_pid() -> int | None:
    return _process.pid if is_running() else None


# ==================================================
# Health Check (public -- used by background monitor)
# ==================================================

async def health_check() -> bool:
    try:
        session = get_session()
        async with session.get(LLM_HEALTH_URL) as resp:
            return resp.status == 200
    except (aiohttp.ClientConnectorError, asyncio.TimeoutError):
        return False
    except Exception:
        return False


# ==================================================
# Crash Recovery
# ==================================================

async def attempt_recovery() -> bool:
    # public entry point -- called by both call() and
    # the background health monitor. Lock prevents
    # concurrent restart attempts from racing.
    async with _recovery_lock:
        # if someone else already recovered while we waited
        if is_running() and await health_check():
            return True
        return await _recover_with_backoff()


async def _recover_with_backoff() -> bool:
    wait = RECOVERY_INITIAL_WAIT
    for attempt in range(1, RECOVERY_MAX_RETRIES + 1):
        log.warning("llm_recovery_attempt",
                     attempt=attempt, max=RECOVERY_MAX_RETRIES, wait=wait)
        await asyncio.sleep(wait)

        try:
            success = await start(get_session())
            if success:
                log.info("llm_recovery_success", attempt=attempt)
                return True
        except Exception as e:
            log.error("llm_recovery_start_failed",
                       attempt=attempt, error=str(e))

        wait = min(wait * 2, RECOVERY_MAX_WAIT)

    log.critical("llm_recovery_failed",
                  max_retries=RECOVERY_MAX_RETRIES)
    return False


# ==================================================
# Error Detection Helpers
# ==================================================

def _is_context_overflow(status: int, error_text: str) -> bool:
    # detect llama.cpp context size exceeded error
    if status != 400:
        return False
    try:
        err = json.loads(error_text)
        return err.get("error", {}).get("type") == "exceed_context_size_error"
    except (json.JSONDecodeError, AttributeError):
        return False


# ==================================================
# Inference -- Blocking
# ==================================================

async def call(user: User, message: str) -> "LLMResponse":
    user.add_message("user", message)
    messages = user.build_messages()

    payload = _build_payload(user, messages, stream=False)
    t0 = time.perf_counter()

    try:
        session = get_session()
        async with session.post(LLM_URL, json=payload) as resp:
            if resp.status != 200:
                err = await resp.text()

                # context overflow -- let caller handle summarization
                if _is_context_overflow(resp.status, err):
                    user.conversation_history.pop()
                    log.warning("llm_context_overflow",
                                user_id=user.user_id, slot=user.slot)
                    raise LLMContextOverflow(
                        f"Slot {user.slot} context exceeded for {user.user_id}"
                    )

                log.error("llm_call_failed", status=resp.status, error=err)
                user.conversation_history.pop()
                raise LLMCallError(f"LLM returned {resp.status}")
            data = await resp.json()

    except (aiohttp.ClientConnectorError, asyncio.TimeoutError) as e:
        user.conversation_history.pop()
        log.warning("llm_unreachable_attempting_recovery", error=str(e))

        # attempt crash recovery -- retry once if successful
        if await attempt_recovery():
            return await _retry_call(user, message)

        raise LLMCallError(f"LLM unreachable after recovery: {e}") from e

    elapsed = time.perf_counter() - t0
    text = data["choices"][0]["message"]["content"].strip()

    usage        = data.get("usage", {})
    total_tokens = usage.get("total_tokens", 0)
    truncated    = data.get("truncated", False)

    _update_flags(user, total_tokens, truncated)
    user.add_message("assistant", text)

    log.info("llm_response",
             slot=user.slot, elapsed=f"{elapsed:.2f}s",
             tokens=total_tokens, chars=len(text))

    return LLMResponse(
        content=text,
        elapsed=elapsed,
        total_tokens=total_tokens,
        truncated=truncated,
    )


async def _retry_call(user: User, message: str) -> "LLMResponse":
    # single retry after successful recovery -- message is already
    # removed from history by the caller, so call() re-adds it
    log.info("llm_retrying_after_recovery", user_id=user.user_id)
    return await call(user, message)


# ==================================================
# Inference -- Streaming (SSE)
# ==================================================

async def call_stream(user: User, message: str) -> AsyncIterator[str]:
    user.add_message("user", message)
    messages = user.build_messages()

    payload = _build_payload(user, messages, stream=True)
    full_response = []
    total_tokens = 0
    truncated = False
    t0 = time.perf_counter()

    try:
        session = get_session()
        async with session.post(LLM_URL, json=payload) as resp:
            if resp.status != 200:
                err = await resp.text()

                # context overflow
                if _is_context_overflow(resp.status, err):
                    user.conversation_history.pop()
                    log.warning("llm_stream_context_overflow",
                                user_id=user.user_id, slot=user.slot)
                    raise LLMContextOverflow(
                        f"Slot {user.slot} context exceeded for {user.user_id}"
                    )

                log.error("llm_stream_failed", status=resp.status, error=err)
                user.conversation_history.pop()
                raise LLMCallError(f"LLM returned {resp.status}")

            async for line in resp.content:
                decoded = line.decode("utf-8").strip()
                if not decoded or not decoded.startswith("data: "):
                    continue

                json_str = decoded[6:]  # strip "data: "
                if json_str == "[DONE]":
                    break

                try:
                    chunk = json.loads(json_str)
                except json.JSONDecodeError:
                    continue

                # extract delta content
                choices = chunk.get("choices", [])
                if not choices:
                    continue

                delta = choices[0].get("delta", {})
                content = delta.get("content", "")

                if content:
                    full_response.append(content)
                    yield content

                # check for usage in final chunk
                usage = chunk.get("usage")
                if usage:
                    total_tokens = usage.get("total_tokens", 0)

                if chunk.get("truncated"):
                    truncated = True

    except (aiohttp.ClientConnectorError, asyncio.TimeoutError) as e:
        user.conversation_history.pop()

        # if nothing has been yielded yet, try recovery
        if not full_response:
            log.warning("llm_stream_unreachable_attempting_recovery", error=str(e))
            if await attempt_recovery():
                log.info("llm_stream_retrying_after_recovery",
                         user_id=user.user_id)
                async for chunk in call_stream(user, message):
                    yield chunk
                return

        raise LLMCallError(f"LLM unreachable: {e}") from e

    elapsed = time.perf_counter() - t0
    complete_text = "".join(full_response).strip()

    _update_flags(user, total_tokens, truncated)
    user.add_message("assistant", complete_text)

    # store response metadata for streaming post-processor
    user.last_total_tokens = total_tokens
    user.last_elapsed = elapsed
    user.last_truncated = truncated

    log.info("llm_stream_complete",
             slot=user.slot, elapsed=f"{elapsed:.2f}s",
             tokens=total_tokens, chars=len(complete_text))


# ==================================================
# Inference -- Internal (background tasks)
# ==================================================
# Generalized internal call that can target any slot.
# Used for summarization, think-mode reviews, prompt
# rewrites, and other tasks that don't belong in a
# user's conversation history.
#
# When utility lane is enabled:  targets utility slot
# When utility lane is disabled: targets the given
#   fallback_slot (the requesting user's slot)
# ==================================================

async def call_internal(
    messages: list[dict],
    temperature: float = 0.3,
    max_tokens: int = 512,
    fallback_slot: int | None = None,
) -> "LLMResponse":
    # determine which slot to use
    if UTILITY_ENABLED:
        slot_id = SLOT_MAP.get("utility")
        if slot_id is None:
            # config mismatch -- utility enabled but not in slot map
            # fall back to user slot if available
            if fallback_slot is not None:
                slot_id = fallback_slot
                log.warning("utility_slot_missing_using_fallback",
                             fallback_slot=fallback_slot)
            else:
                raise LLMCallError(
                    "Utility slot not configured and no fallback provided"
                )
    else:
        if fallback_slot is None:
            raise LLMCallError(
                "Utility lane disabled and no fallback_slot provided. "
                "Caller must pass the user's slot for in-place operation."
            )
        slot_id = fallback_slot

    payload = {
        "model":        "local",
        "messages":     messages,
        "temperature":  temperature,
        "max_tokens":   max_tokens,
        "stream":       False,
        "id_slot":      slot_id,
        "cache_prompt": False,
    }

    t0 = time.perf_counter()

    try:
        session = get_session()
        async with session.post(LLM_URL, json=payload) as resp:
            if resp.status != 200:
                err = await resp.text()
                log.error("llm_internal_call_failed",
                           slot=slot_id, status=resp.status, error=err)
                raise LLMCallError(f"Internal call on slot {slot_id} returned {resp.status}")
            data = await resp.json()

    except (aiohttp.ClientConnectorError, asyncio.TimeoutError) as e:
        log.warning("llm_internal_unreachable", slot=slot_id, error=str(e))
        raise LLMCallError(f"LLM unreachable for internal call on slot {slot_id}: {e}") from e

    elapsed = time.perf_counter() - t0
    text = data["choices"][0]["message"]["content"].strip()

    usage        = data.get("usage", {})
    total_tokens = usage.get("total_tokens", 0)

    log.info("llm_internal_response",
             slot=slot_id, elapsed=f"{elapsed:.2f}s",
             tokens=total_tokens, chars=len(text))

    return LLMResponse(
        content=text,
        elapsed=elapsed,
        total_tokens=total_tokens,
        truncated=False,
    )


# ==================================================
# Backward Compatibility -- call_utility wraps
# call_internal for any existing callers
# ==================================================

async def call_utility(
    messages: list[dict],
    temperature: float = 0.3,
    max_tokens: int = 512,
) -> "LLMResponse":
    return await call_internal(
        messages=messages,
        temperature=temperature,
        max_tokens=max_tokens,
        fallback_slot=None,
    )


# ==================================================
# Shared Helpers
# ==================================================

def _build_payload(user: User, messages: list[dict], stream: bool) -> dict:
    return {
        "model":             "local",
        "messages":          messages,
        "temperature":       user.temperature,
        "min_p":             user.min_p,
        "top_k":             user.top_k,
        "repeat_penalty":    user.repeat_penalty,
        "frequency_penalty": user.frequency_penalty,
        "max_tokens":        user.max_tokens,
        "stream":            stream,
        "id_slot":           user.slot,
        "cache_prompt":      True,
    }


def _update_flags(user: User, total_tokens: int, truncated: bool):
    if truncated or total_tokens > (CONTEXT_PER_SLOT * THRESHOLD_WARN):
        user.flag_warn = True
        if total_tokens > (CONTEXT_PER_SLOT * THRESHOLD_CRIT):
            user.flag_crit = True
            log.warning("token_critical",
                         user_id=user.user_id, slot=user.slot, tokens=total_tokens)
        else:
            log.info("token_warn",
                     user_id=user.user_id, slot=user.slot, tokens=total_tokens)
    else:
        user.flag_warn = False
        user.flag_crit = False
        release_summarize_gate(user.user_id)


# ==================================================
# Response Object
# ==================================================

class LLMResponse:
    __slots__ = ("content", "elapsed", "total_tokens", "truncated")

    def __init__(self, content: str, elapsed: float,
                 total_tokens: int, truncated: bool):
        self.content      = content
        self.elapsed      = elapsed
        self.total_tokens = total_tokens
        self.truncated    = truncated


class LLMCallError(Exception):
    pass


class LLMContextOverflow(Exception):
    pass