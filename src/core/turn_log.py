# core/turn_log.py
#
# Author:  Logicish
# Company: Logic-Ish Designs
# Date:    10/6/2026
#
# ==================================================
# Conversation turn log. One row per turn in the
# user's history.db (turns table), written in the
# background so logging can never slow or break a turn.
#
# Captured per turn: user text, final reply, status,
# intent + router tags, tool name/args/result, injected
# directive, sampling, tokens, timing, persona hash,
# and a zlib-compressed payload with the exact messages
# sent to the conv LLM + the think block.
#
# Every exit path is logged — ok, denied, aborted,
# busy, overflow, error, disconnected, held (gpu_guard
# / failed recovery) — failures are
# the most useful rows for tuning.
#
# Persona text is stored once per version in the
# prompts table and referenced by hash, so any turn
# can be traced to the prompt that produced it.
#
# Size control lives in modules/turn_log_retention.py.
#
# Knows about: config, providers (get_db),
#              envelope (MessageEnvelope).
# ==================================================

# ==================================================
# Imports
# ==================================================
from __future__ import annotations

import asyncio
import hashlib
import json
import time
import zlib
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import TYPE_CHECKING

import structlog

import providers
from config import TURN_LOG_ENABLED

if TYPE_CHECKING:
    from core.envelope import MessageEnvelope
    from core.pipeline import PipelineContext
    from core.slots import User

log = structlog.get_logger()

# strong refs so background writes aren't garbage-collected mid-flight
_pending: set[asyncio.Task] = set()


# ==================================================
# Turn record
# ==================================================

@dataclass
class Turn:
    envelope:       "MessageEnvelope"
    status:         str                     = "pending"
    response:       str                     = ""
    error:          str                     = ""
    user:           "User | None"           = None
    ctx:            "PipelineContext | None" = None
    requested_user: str | None              = None
    t0:             float                   = field(default_factory=time.perf_counter)


def begin(envelope: "MessageEnvelope") -> Turn:
    return Turn(envelope=envelope)


def submit(turn: Turn) -> None:
    """Snapshot the turn now and write it in the background. Never raises."""
    if not TURN_LOG_ENABLED:
        return
    try:
        row, persona = _build_row(turn)
    except Exception as e:
        log.error("turn_log_build_failed", error=str(e))
        return
    _spawn(_write(row, persona))


def log_denied(
    requested_user: str,
    reason:         str,
    text:           str | None = None,
    source:         str | None = None,
    device_id:      str | None = None,
) -> None:
    """Record a request rejected at Gate 1 (unknown user, web-locked, bad token).
    Stored in guest's history.db since there's no resolved user."""
    if not TURN_LOG_ENABLED:
        return
    from uuid import uuid4
    row = _empty_row()
    row.update(
        turn_id        = str(uuid4()),
        ts             = _now_iso(),
        user_id        = "guest",
        requested_user = requested_user or None,
        source         = source,
        device_id      = device_id,
        status         = "denied",
        error          = reason,
        user_text      = text,
    )
    _spawn(_write(row, None))


# ==================================================
# Row building (runs synchronously at submit time so
# later history/ctx changes can't leak into the record)
# ==================================================

def _build_row(turn: Turn) -> tuple[dict, str | None]:
    env  = turn.envelope
    ctx  = turn.ctx
    user = turn.user

    row = _empty_row()
    row.update(
        turn_id          = env.message_id,
        ts               = env.timestamp.astimezone(timezone.utc).isoformat(),
        user_id          = user.user_id if user else "guest",
        requested_user   = turn.requested_user or (None if user else env.user_id),
        source           = env.source.value if env.source else None,
        device_id        = env.device_id,
        conversation_id  = env.conversation_id,
        stt_confidence   = env.stt_confidence,
        voice_confidence = env.voice_confidence,
        status           = turn.status,
        error            = turn.error or None,
        user_text        = env.text,
        response         = turn.response,
        total_ms         = int((time.perf_counter() - turn.t0) * 1000),
    )

    if ctx is not None:
        row.update(
            intent       = ctx.intent or None,
            tags         = json.dumps(ctx.tags) if ctx.tags else None,
            tool         = ctx.tool_name,
            tool_args    = json.dumps(ctx.tool_args) if ctx.tool_args is not None else None,
            tool_result  = ctx.tool_result,
            directive    = ctx.directive,
            thinking     = int(bool(ctx.thinking)),
            temperature  = ctx.temperature_override,
            total_tokens = ctx.total_tokens or None,
            llm_elapsed  = ctx.elapsed or None,
            truncated    = int(bool(ctx.truncated)),
        )

    persona = None
    if user is not None:
        persona = user.persona
        row["prompt_hash"] = _hash(persona)
        if row["temperature"] is None:
            row["temperature"] = user.temperature

        req = user.last_request
        if req is not None:
            # the temperature actually sent (sampling profile may set it)
            sent = (req.get("sampling") or {}).get("temperature")
            if sent is not None:
                row["temperature"] = sent
            blob = zlib.compress(
                json.dumps({**req, "think": user.last_think or None},
                           ensure_ascii=False).encode("utf-8"),
                level=6,
            )
            row["payload"]       = blob
            row["payload_bytes"] = len(blob)

    return row, persona


def _empty_row() -> dict:
    return {
        "turn_id": None, "ts": None, "user_id": None, "requested_user": None,
        "source": None, "device_id": None, "conversation_id": None,
        "stt_confidence": None, "voice_confidence": None,
        "status": None, "error": None, "user_text": None, "response": None,
        "intent": None, "tags": None, "tool": None, "tool_args": None,
        "tool_result": None, "directive": None, "thinking": 0,
        "temperature": None, "prompt_hash": None, "total_tokens": None,
        "llm_elapsed": None, "total_ms": None, "truncated": 0,
        "payload": None, "payload_bytes": 0,
    }


def _hash(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


# ==================================================
# Storage
# ==================================================

def _spawn(coro) -> None:
    try:
        task = asyncio.get_running_loop().create_task(coro)
    except RuntimeError:
        coro.close()
        return
    _pending.add(task)
    task.add_done_callback(_pending.discard)


async def _write(row: dict, persona: str | None) -> None:
    try:
        db = providers.get_db(f"history:{row['user_id']}")
        if db is None or not db.is_ready:
            log.warning("turn_log_db_unavailable", user_id=row["user_id"])
            return

        if persona is not None:
            await db.execute(
                "INSERT OR IGNORE INTO prompts (hash, first_seen, persona) VALUES (?, ?, ?)",
                (row["prompt_hash"], _now_iso(), persona),
            )

        cols = ", ".join(row.keys())
        qs   = ", ".join("?" for _ in row)
        await db.execute(
            f"INSERT OR REPLACE INTO turns ({cols}) VALUES ({qs})",
            tuple(row.values()),
        )
        log.debug("turn_logged", user_id=row["user_id"], status=row["status"],
                  payload_bytes=row["payload_bytes"])
    except Exception as e:
        log.error("turn_log_write_failed", user_id=row.get("user_id"), error=str(e))


async def drain() -> None:
    """Wait for in-flight writes (called on shutdown)."""
    if _pending:
        await asyncio.gather(*list(_pending), return_exceptions=True)
