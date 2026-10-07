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
# Hold: gpu_guard (heat / dead GPU) or a failed
# recovery cycle sets a hold reason. While held, every
# call raises LLMCallError and recovery is refused, so
# nothing relaunches llama-server against a bad card.
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
import re
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
    SAMPLING_PROFILES,
)
from core.gates import release_summarize_gate
from core.slots import User

log = structlog.get_logger()

_THINK_STRIP_RE = re.compile(r"<think>.*?</think>\s*", re.DOTALL)

# ==================================================
# Process State
# ==================================================

_process: subprocess.Popen | None = None
_env = {**os.environ, "LD_LIBRARY_PATH": "/opt/llama.cpp/build/bin"}
_session: aiohttp.ClientSession | None = None

# recovery lock prevents multiple concurrent restart attempts
_recovery_lock = asyncio.Lock()

# set by gpu_guard or a failed recovery cycle -- None = serving
_hold: str | None = None

# ==================================================
# Hold (public -- gpu_guard, main gate, /llm/restart)
# ==================================================

def hold_reason() -> str | None:
    return _hold


def set_hold(reason: str) -> None:
    global _hold
    if _hold != reason:
        log.warning("llm_hold_set", reason=reason, previous=_hold)
    _hold = reason


def clear_hold() -> None:
    global _hold
    if _hold is not None:
        log.info("llm_hold_cleared", reason=_hold)
    _hold = None


def _check_hold() -> None:
    if _hold is not None:
        raise LLMCallError(f"LLM held: {_hold}")

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
        if await health_check():
            log.info("llm_already_running", pid=_process.pid)
            return True
        # alive but not answering (wedged) -- replace it
        log.warning("llm_running_but_unhealthy", pid=_process.pid)
        await stop()

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
    await stop()    # don't leave a half-started server to be mistaken for healthy
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
        async with session.get(LLM_HEALTH_URL,
                               timeout=aiohttp.ClientTimeout(total=5)) as resp:
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
        if _hold is not None:
            log.warning("llm_recovery_refused_held", reason=_hold)
            return False
        # if someone else already recovered while we waited
        if is_running() and await health_check():
            return True
        if await _recover_with_backoff():
            return True
        # a full backoff cycle failed -- stop retrying until an admin restart
        if _hold is None:
            set_hold("recovery_failed")
        return False


async def _recover_with_backoff() -> bool:
    wait = RECOVERY_INITIAL_WAIT
    for attempt in range(1, RECOVERY_MAX_RETRIES + 1):
        log.warning("llm_recovery_attempt",
                     attempt=attempt, max=RECOVERY_MAX_RETRIES, wait=wait)
        await asyncio.sleep(wait)
        if _hold is not None:
            log.warning("llm_recovery_aborted_held", reason=_hold)
            return False

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

async def call(user: User, message: str, temperature_override: float | None = None,
               thinking: bool = False,
               extra_messages: list[dict] | None = None,
               sampling: dict | None = None) -> "LLMResponse":
    _check_hold()
    user.add_message("user", message)
    messages = user.build_messages()

    if extra_messages:
        sys_parts = [m["content"] for m in extra_messages if m.get("role") == "system"]
        non_sys   = [m for m in extra_messages if m.get("role") != "system"]
        if sys_parts and messages and messages[0].get("role") == "system":
            addition = "\n\n".join(sys_parts)
            messages[0] = {**messages[0], "content": messages[0]["content"] + "\n\n" + addition}
        if non_sys:
            messages = messages[:-1] + non_sys + [messages[-1]]

    payload = _build_payload(user, messages, stream=False,
                             temperature_override=temperature_override, thinking=thinking,
                             sampling=sampling)
    _record_request(user, payload)
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
            return await _retry_call(user, message, temperature_override=temperature_override,
                                     thinking=thinking, extra_messages=extra_messages,
                                     sampling=sampling)

        raise LLMCallError(f"LLM unreachable after recovery: {e}") from e

    elapsed = time.perf_counter() - t0
    msg  = data["choices"][0]["message"]
    text = msg["content"].strip()
    user.last_think = msg.get("reasoning_content") or ""
    if thinking:
        think_match = re.search(r"<think>(.*?)</think>", text, re.DOTALL)
        if think_match:
            user.last_think = user.last_think or think_match.group(1).strip()
            log.debug("llm_think_block", slot=user.slot,
                      chars=len(think_match.group(1)))
        text = _THINK_STRIP_RE.sub("", text).strip()

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


async def _retry_call(user: User, message: str, temperature_override: float | None = None,
                      thinking: bool = False,
                      extra_messages: list[dict] | None = None,
                      sampling: dict | None = None) -> "LLMResponse":
    # single retry after successful recovery -- message is already
    # removed from history by the caller, so call() re-adds it
    log.info("llm_retrying_after_recovery", user_id=user.user_id)
    return await call(user, message, temperature_override=temperature_override,
                      thinking=thinking, extra_messages=extra_messages,
                      sampling=sampling)


# ==================================================
# Inference -- Streaming (SSE)
# ==================================================

async def call_stream(user: User, message: str, temperature_override: float | None = None,
                      thinking: bool = False,
                      extra_messages: list[dict] | None = None,
                      sampling: dict | None = None) -> AsyncIterator[str]:
    _check_hold()
    user.add_message("user", message)
    messages = user.build_messages()

    if extra_messages:
        sys_parts = [m["content"] for m in extra_messages if m.get("role") == "system"]
        non_sys   = [m for m in extra_messages if m.get("role") != "system"]
        if sys_parts and messages and messages[0].get("role") == "system":
            addition = "\n\n".join(sys_parts)
            messages[0] = {**messages[0], "content": messages[0]["content"] + "\n\n" + addition}
        if non_sys:
            messages = messages[:-1] + non_sys + [messages[-1]]

    payload = _build_payload(user, messages, stream=True,
                             temperature_override=temperature_override, thinking=thinking,
                             sampling=sampling)
    _record_request(user, payload)
    full_response = []
    reasoning     = []
    total_tokens = 0
    truncated = False
    t0 = time.perf_counter()
    # think-block filter state
    _think_buf = ""
    _past_think = not thinking  # if not thinking, pass everything through immediately

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

                # usage arrives in a final chunk with empty choices (include_usage)
                usage = chunk.get("usage")
                if usage:
                    total_tokens = usage.get("total_tokens", 0)

                # extract delta content
                choices = chunk.get("choices", [])
                if not choices:
                    continue

                delta = choices[0].get("delta", {})
                content = delta.get("content", "")
                if delta.get("reasoning_content"):
                    reasoning.append(delta["reasoning_content"])
                    # server already split the think block out (reasoning-format
                    # deepseek), so every content delta is answer text — waiting
                    # for a "</think>" that never comes swallowed the whole reply
                    # (empty think_mode answers, review 2026-10-06)
                    _past_think = True

                if content:
                    full_response.append(content)
                    if _past_think:
                        yield content
                    else:
                        _think_buf += content
                        end_idx = _think_buf.find("</think>")
                        if end_idx != -1:
                            _past_think = True
                            think_content = _think_buf[:end_idx]
                            user.last_think = think_content.replace("<think>", "").strip()
                            log.debug("llm_think_block_stream",
                                      slot=user.slot, chars=len(think_content))
                            remainder = _think_buf[end_idx + 8:].lstrip("\n")
                            if remainder:
                                yield remainder

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
                async for chunk in call_stream(user, message,
                                               temperature_override=temperature_override,
                                               thinking=thinking,
                                               extra_messages=extra_messages,
                                               sampling=sampling):
                    yield chunk
                return

        raise LLMCallError(f"LLM unreachable: {e}") from e

    elapsed = time.perf_counter() - t0
    complete_text = "".join(full_response).strip()

    _update_flags(user, total_tokens, truncated)
    user.add_message("assistant", complete_text)

    # store response metadata for streaming post-processor
    if reasoning:
        user.last_think = "".join(reasoning).strip()
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
# When utility lane is enabled:  targets guest slot
# When utility lane is disabled: targets the given
#   fallback_slot (the requesting user's slot)
# ==================================================

async def call_internal(
    messages: list[dict],
    temperature: float = 0.3,
    max_tokens: int = 512,
    fallback_slot: int | None = None,
) -> "LLMResponse":
    _check_hold()
    # determine which slot to use
    if UTILITY_ENABLED:
        slot_id = SLOT_MAP.get("guest")
        if slot_id is None:
            # config mismatch -- utility enabled but guest not in slot map
            # fall back to user slot if available
            if fallback_slot is not None:
                slot_id = fallback_slot
                log.warning("guest_slot_missing_using_fallback",
                             fallback_slot=fallback_slot)
            else:
                raise LLMCallError(
                    "Guest slot not configured and no fallback provided"
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
        "chat_template_kwargs": {"enable_thinking": False},
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

    except (aiohttp.ServerDisconnectedError, aiohttp.ClientConnectorError,
            asyncio.TimeoutError) as e:
        log.warning("llm_internal_unreachable", slot=slot_id, error=str(e))

        if await attempt_recovery():
            log.info("llm_internal_retrying_after_recovery", slot=slot_id)
            try:
                session = get_session()
                async with session.post(LLM_URL, json=payload) as resp:
                    if resp.status != 200:
                        err = await resp.text()
                        log.error("llm_internal_call_failed",
                                   slot=slot_id, status=resp.status, error=err)
                        raise LLMCallError(
                            f"Internal call on slot {slot_id} returned {resp.status}")
                    data = await resp.json()
            except (aiohttp.ServerDisconnectedError, aiohttp.ClientConnectorError,
                    asyncio.TimeoutError) as e2:
                raise LLMCallError(
                    f"LLM unreachable for internal call on slot {slot_id} "
                    f"after recovery: {e2}") from e2
        else:
            raise LLMCallError(
                f"LLM unreachable for internal call on slot {slot_id}: {e}") from e

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
    fallback_slot: int | None = None,
) -> "LLMResponse":
    return await call_internal(
        messages=messages,
        temperature=temperature,
        max_tokens=max_tokens,
        fallback_slot=fallback_slot,
    )


# ==================================================
# Inference -- Tool LLM (function calling, no history)
# ==================================================
# Guest-slot call with an OpenAI-format tools list.
# Used by the tool pipeline for arg-extraction and
# uncertain routing. Never touches conversation history.
# Always disables thinking (tool calls need clean JSON).
#
# Returns (content, tool_calls_raw, finish_reason):
#   content        — stripped text, or None if empty
#   tool_calls_raw — raw tool_calls list, or None
#   finish_reason  — "tool_calls" | "stop" | other
#
# finish_reason is the correct discriminator — content
# is "" (empty string) not null for tool calls.
# ==================================================

async def call_with_tools(
    messages: list[dict],
    tools: list[dict],
    temperature: float = 0.1,
    max_tokens: int = 256,
    fallback_slot: int | None = None,
) -> tuple[str | None, list | None, str]:
    _check_hold()
    if UTILITY_ENABLED:
        slot_id = SLOT_MAP.get("guest")
        if slot_id is None:
            if fallback_slot is not None:
                slot_id = fallback_slot
                log.warning("guest_slot_missing_using_fallback",
                             fallback_slot=fallback_slot)
            else:
                raise LLMCallError(
                    "Guest slot not configured and no fallback provided"
                )
    else:
        if fallback_slot is None:
            raise LLMCallError(
                "Utility lane disabled and no fallback_slot provided."
            )
        slot_id = fallback_slot

    router = SAMPLING_PROFILES.get("router", {})
    payload = {
        "model":        "local",
        "messages":     messages,
        "tools":        tools,
        # router profile sets the sampler; explicit args still win
        "top_p":            router.get("top_p", 0.8),
        "top_k":            router.get("top_k", 20),
        "min_p":            router.get("min_p", 0.0),
        "presence_penalty": router.get("presence_penalty", 0.0),
        "temperature":  temperature,
        "max_tokens":   max_tokens,
        "stream":       False,
        "id_slot":      slot_id,
        "cache_prompt": False,
        "chat_template_kwargs": {"enable_thinking": False},
    }

    t0 = time.perf_counter()

    try:
        session = get_session()
        async with session.post(LLM_URL, json=payload) as resp:
            if resp.status != 200:
                err = await resp.text()
                log.error("llm_tool_call_failed",
                           slot=slot_id, status=resp.status, error=err)
                raise LLMCallError(f"Tool call on slot {slot_id} returned {resp.status}")
            data = await resp.json()

    except (aiohttp.ServerDisconnectedError, aiohttp.ClientConnectorError,
            asyncio.TimeoutError) as e:
        log.warning("llm_tool_call_unreachable", slot=slot_id, error=str(e))

        if await attempt_recovery():
            log.info("llm_tool_call_retrying_after_recovery", slot=slot_id)
            try:
                session = get_session()
                async with session.post(LLM_URL, json=payload) as resp:
                    if resp.status != 200:
                        err = await resp.text()
                        log.error("llm_tool_call_failed",
                                   slot=slot_id, status=resp.status, error=err)
                        raise LLMCallError(
                            f"Tool call on slot {slot_id} returned {resp.status}")
                    data = await resp.json()
            except (aiohttp.ServerDisconnectedError, aiohttp.ClientConnectorError,
                    asyncio.TimeoutError) as e2:
                raise LLMCallError(
                    f"LLM unreachable for tool call on slot {slot_id} "
                    f"after recovery: {e2}") from e2
        else:
            raise LLMCallError(
                f"LLM unreachable for tool call on slot {slot_id}: {e}") from e

    elapsed = time.perf_counter() - t0
    choice = data["choices"][0]
    msg    = choice["message"]

    content_raw    = (msg.get("content") or "").strip()
    content        = content_raw if content_raw else None
    tool_calls_raw = msg.get("tool_calls")
    finish_reason  = choice.get("finish_reason", "stop")

    log.info("llm_tool_call_response",
             slot=slot_id, elapsed=f"{elapsed:.2f}s",
             finish_reason=finish_reason,
             has_tool_calls=tool_calls_raw is not None)

    return content, tool_calls_raw, finish_reason


# ==================================================
# Vision Call — image + text prompt, no slot history
# ==================================================

async def call_vision(
    image_path: str,
    prompt: str,
    temperature: float = 0.2,
    max_tokens: int = 1024,
) -> str | None:
    """Send an image + text prompt to the vision model.

    Encodes the image as base64 and posts directly to the
    OpenAI-compatible endpoint. Does not touch any slot or
    conversation history. Returns the response text or None
    on failure.
    """
    if _hold is not None:
        log.warning("llm_vision_refused_held", reason=_hold)
        return None
    import base64
    from pathlib import Path

    try:
        suffix = Path(image_path).suffix.lower().lstrip(".")
        mime   = "image/jpeg" if suffix in ("jpg", "jpeg") else f"image/{suffix}"
        with open(image_path, "rb") as f:
            b64 = base64.b64encode(f.read()).decode()
    except Exception as e:
        log.error("call_vision_image_read_failed", path=image_path, error=str(e))
        return None

    payload = {
        "model":       "local",
        "temperature": temperature,
        "max_tokens":  max_tokens,
        "stream":      False,
        "messages": [
            {
                "role": "user",
                "content": [
                    {"type": "image_url", "image_url": {"url": f"data:{mime};base64,{b64}"}},
                    {"type": "text",      "text": prompt},
                ],
            }
        ],
    }

    try:
        session = get_session()
        async with session.post(LLM_URL, json=payload) as resp:
            if resp.status != 200:
                err = await resp.text()
                log.error("call_vision_failed", status=resp.status, error=err)
                return None
            data = await resp.json()
            return data["choices"][0]["message"]["content"].strip()
    except Exception as e:
        log.error("call_vision_error", error=str(e))
        return None


# ==================================================
# Shared Helpers
# ==================================================

def _record_request(user: User, payload: dict) -> None:
    # snapshot for core/turn_log — copy the list so later history edits don't leak in
    user.last_request = {
        "messages": list(payload["messages"]),
        "sampling": {k: v for k, v in payload.items() if k not in ("messages", "stream", "model")},
    }
    user.last_think = ""


def _build_payload(user: User, messages: list[dict], stream: bool,
                   temperature_override: float | None = None,
                   thinking: bool = False,
                   sampling: dict | None = None) -> dict:
    # base = user's profile.json values (persona defaults); a per-turn
    # sampling profile (config sampling_profiles, picked in main) overrides
    # any key it sets. temperature_override (per-intent / caller) wins last.
    payload = {
        "model":            "local",
        "messages":         messages,
        "temperature":      user.temperature,
        "top_p":            user.top_p,
        "top_k":            user.top_k,
        "min_p":            user.min_p,
        "presence_penalty": user.presence_penalty,
        "max_tokens":       user.max_tokens * 4 if thinking else user.max_tokens,
    }
    if sampling:
        payload.update(sampling)
    if temperature_override is not None:
        payload["temperature"] = temperature_override
    payload.update({
        "stream":           stream,
        # without this llama-server sends no usage when streaming → tokens=0 →
        # flags never set, summarizer only fires via context overflow (20s stalls)
        **({"stream_options": {"include_usage": True}} if stream else {}),
        "id_slot":          user.slot,
        "cache_prompt":     True,
        "chat_template_kwargs": {"enable_thinking": thinking},
    })
    if not thinking:
        payload.pop("thinking_budget_tokens", None)
    return payload


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