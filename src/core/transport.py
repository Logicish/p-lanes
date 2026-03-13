# core/transport.py
#
# Author:  Logicish
# Company: Logic-Ish Designs
# Date:    3/13/2026
#
# ==================================================
# FastAPI HTTP server.
# Receives requests, builds a MessageEnvelope, and
# passes it to handle_message or handle_stream.
# Supports both JSON and SSE endpoints.
# GATE 1 — first security checkpoint.
#
# Envelope construction:
#   All inbound requests are normalized into a
#   MessageEnvelope before reaching the pipeline.
#   user_id is always resolved by gate1 before
#   the envelope is built — voice WS is the exception
#   (user_id may be None when voice print derives it).
#
# Broadcast listener endpoint:
#   GET /channel/listen/{user_id} — SSE subscription.
#   Requires Gate 1 auth via query param. Same-user
#   only. Returns 503 if broadcast is disabled.
#
# Admin endpoints:
#   GET /admin/dump — full prompt dump for all users.
#   GET /admin/dump/{user_id} — dump for one user.
#   Both require ADMIN-level Gate 1.
#
# Knows about: config (SecurityLevel), slots (resolve,
#              get_user), llm (health, restart, session),
#              broadcast (subscribe, enabled check),
#              envelope (MessageEnvelope, Source).
# ==================================================

# ==================================================
# Imports
# ==================================================
import asyncio
import json
from datetime import datetime
from typing import Callable

import structlog
from fastapi import FastAPI, Request, Query, WebSocket, WebSocketDisconnect
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field
from sse_starlette.sse import EventSourceResponse

import providers
from config import SecurityLevel
from core import slots, llm, broadcast
from core.envelope import MessageEnvelope, Source

log = structlog.get_logger()


# ==================================================
# Source mapping
# ==================================================

_SOURCE_MAP: dict[str, Source] = {
    "text":  Source.TEXT,
    "voice": Source.VOICE,
    "api":   Source.API,
    "ha":    Source.HA,
}


def _parse_source(input_type: str) -> Source:
    return _SOURCE_MAP.get(input_type.lower(), Source.TEXT)


# ==================================================
# Voice / sentence helpers
# ==================================================

# Common abbreviations that should not trigger sentence splits.
_ABBREVS = frozenset({
    "dr", "mr", "mrs", "ms", "st", "vs", "etc", "jr", "sr",
    "prof", "gen", "lt", "sgt", "cpl", "eg", "ie",
})


def _pop_sentence(buf: str) -> tuple[str | None, str]:
    """Extract the first complete sentence from buf.
    Splits on . ! ? followed by whitespace or end-of-string.
    Skips abbreviations (Dr., Mr., etc.) and single-letter initials.
    Returns (sentence, remainder) or (None, buf) if no complete
    sentence is found yet."""
    for i, ch in enumerate(buf):
        if ch not in ".!?":
            continue
        after = buf[i + 1] if i + 1 < len(buf) else " "
        if after not in " \t\n":
            continue
        # for '.', skip abbreviations and single-letter initials
        if ch == ".":
            j = i - 1
            while j >= 0 and buf[j].isalpha():
                j -= 1
            word = buf[j + 1:i].lower()
            if len(word) <= 1 or word in _ABBREVS:
                continue
        return buf[:i + 1].strip(), buf[i + 1:].lstrip()
    return None, buf


async def _tts_send(ws: WebSocket, tts, text: str) -> None:
    """Synthesize text and send as a binary WAV frame.
    Falls back to a JSON text frame if TTS is unavailable or fails."""
    if tts is not None and tts.is_ready:
        audio = await tts.synthesize(text)
        if audio:
            await ws.send_bytes(audio)
            return
    await ws.send_json({"event": "text", "data": text})


# ==================================================
# Payload Schemas
# ==================================================

class MessagePayload(BaseModel):
    user_id:         str        = "guest"
    message:         str        = Field(..., min_length=1, max_length=4096)
    conversation_id: str | None = None
    device_id:       str | None = None
    input_type:      str        = "text"    # text | voice | api | ha

    model_config = {
        "extra": "forbid",
        "str_strip_whitespace": True,
    }


class AdminPayload(BaseModel):
    user_id: str = Field(..., min_length=1)

    model_config = {
        "extra": "forbid",
        "str_strip_whitespace": True,
    }


# ==================================================
# Route Factory
# ==================================================

def create_routes(
    app: FastAPI,
    handle_message: Callable,
    handle_stream: Callable,
):
    # --- GATE 1 helper ---
    def _gate1(user_id: str):
        # resolve user identity — unknown maps to guest or None
        resolved = slots.resolve_user(user_id)
        if resolved is None:
            log.warning("gate1_reject_unknown", user_id=user_id)
            return None, JSONResponse(
                status_code=403,
                content={"error": "unknown_user", "detail": "Not in slot map"},
            )

        user = slots.get_user(resolved)
        if user is None or user.security_level < SecurityLevel.GUEST:
            log.warning("gate1_reject_access", user_id=resolved)
            return None, JSONResponse(
                status_code=403,
                content={"error": "access_denied"},
            )

        return user, None

    # --- ADMIN gate helper ---
    def _gate_admin(user_id: str):
        user, error = _gate1(user_id)
        if error:
            return None, error
        if user.security_level < SecurityLevel.ADMIN:
            log.warning("gate_admin_denied", user_id=user_id,
                         level=user.security_level)
            return None, JSONResponse(
                status_code=403,
                content={"error": "access_denied",
                         "detail": "ADMIN level required"},
            )
        return user, None

    # --------------------------------------------------
    # POST /channel/chat — JSON request/response
    # --------------------------------------------------
    @app.post("/channel/chat")
    async def chat_json(payload: MessagePayload):
        user_id = payload.user_id.lower().strip()
        preview = payload.message[:60] + ("..." if len(payload.message) > 60 else "")
        log.info("request_chat", user_id=user_id, preview=preview)

        user, error = _gate1(user_id)
        if error:
            return error

        envelope = MessageEnvelope(
            user_id=user.user_id,
            source=_parse_source(payload.input_type),
            text=payload.message,
            conversation_id=payload.conversation_id,
            device_id=payload.device_id,
        )

        try:
            result = await handle_message(envelope)
        except Exception as e:
            log.error("handle_message_failed", user_id=user.user_id, error=str(e))
            return JSONResponse(
                status_code=500,
                content={"error": "internal", "detail": str(e)},
            )

        return {
            "response":        result,
            "user_id":         user.user_id,
            "conversation_id": payload.conversation_id,
            "message_id":      envelope.message_id,
            "timestamp":       datetime.now().isoformat(),
        }

    # --------------------------------------------------
    # POST /channel/chat/stream — SSE streaming
    # --------------------------------------------------
    @app.post("/channel/chat/stream")
    async def chat_stream(payload: MessagePayload):
        user_id = payload.user_id.lower().strip()
        preview = payload.message[:60] + ("..." if len(payload.message) > 60 else "")
        log.info("request_stream", user_id=user_id, preview=preview)

        user, error = _gate1(user_id)
        if error:
            return error

        envelope = MessageEnvelope(
            user_id=user.user_id,
            source=_parse_source(payload.input_type),
            text=payload.message,
            conversation_id=payload.conversation_id,
            device_id=payload.device_id,
        )

        async def event_generator():
            yield {
                "event": "init",
                "data": json.dumps({
                    "user_id":         user.user_id,
                    "conversation_id": envelope.conversation_id or "",
                    "message_id":      envelope.message_id,
                }),
            }
            try:
                async for chunk in handle_stream(envelope):
                    yield {"event": "token", "data": chunk}
                yield {
                    "event": "done",
                    "data":  envelope.conversation_id or "",
                }
            except Exception as e:
                log.error("stream_failed", user_id=user.user_id, error=str(e))
                yield {"event": "error", "data": str(e)}

        return EventSourceResponse(event_generator())

    # --------------------------------------------------
    # GET /channel/listen/{user_id} — broadcast listener
    # --------------------------------------------------
    @app.get("/channel/listen/{target_user_id}")
    async def listen_stream(
        target_user_id: str,
        user_id: str = Query(..., description="Authenticated user_id"),
    ):
        if not broadcast.is_enabled():
            return JSONResponse(
                status_code=503,
                content={"error": "broadcast_disabled",
                         "detail": "Broadcast is not enabled. A module must enable it."},
            )

        user, error = _gate1(user_id.lower().strip())
        if error:
            return error

        target = target_user_id.lower().strip()
        if user.user_id != target:
            log.warning("listen_denied_wrong_user",
                         requesting=user.user_id, target=target)
            return JSONResponse(
                status_code=403,
                content={"error": "access_denied",
                         "detail": "Can only listen to your own stream"},
            )

        queue = broadcast.subscribe(user.user_id)

        async def listener_generator():
            try:
                yield {
                    "event": "init",
                    "data": json.dumps({
                        "user_id":  user.user_id,
                        "listening": True,
                    }),
                }
                while True:
                    event = await queue.get()
                    yield event
            except asyncio.CancelledError:
                pass
            finally:
                broadcast.unsubscribe(user.user_id, queue)

        return EventSourceResponse(listener_generator())

    # --------------------------------------------------
    # POST /llm/restart — ADMIN-gated
    # --------------------------------------------------
    @app.post("/llm/restart")
    async def llm_restart(payload: AdminPayload):
        user_id = payload.user_id.lower().strip()
        log.info("llm_restart_requested", user_id=user_id)

        user, error = _gate_admin(user_id)
        if error:
            return error

        success = await llm.restart()
        return {
            "success":   success,
            "user_id":   user.user_id,
            "timestamp": datetime.now().isoformat(),
        }

    # --------------------------------------------------
    # GET /admin/dump — all users prompt dump
    # --------------------------------------------------
    @app.get("/admin/dump")
    async def admin_dump_all(
        user_id: str = Query(..., description="ADMIN user_id for auth"),
    ):
        admin, error = _gate_admin(user_id.lower().strip())
        if error:
            return error

        users = slots.get_all_users()
        dump  = {}
        for uid, user in users.items():
            if uid == "utility":
                continue
            dump[uid] = {
                "slot":        user.slot,
                "security":    user.security_level,
                "persona":     user.persona,
                "summary":     user.summary,
                "messages":    user.build_messages(),
                "flag_warn":   user.flag_warn,
                "flag_crit":   user.flag_crit,
                "is_idle":     user.is_idle(),
                "history_len": len(user.conversation_history),
            }
        return {"dump": dump, "timestamp": datetime.now().isoformat()}

    # --------------------------------------------------
    # GET /admin/dump/{target_user_id} — single user dump
    # --------------------------------------------------
    @app.get("/admin/dump/{target_user_id}")
    async def admin_dump_user(
        target_user_id: str,
        user_id: str = Query(..., description="ADMIN user_id for auth"),
    ):
        admin, error = _gate_admin(user_id.lower().strip())
        if error:
            return error

        target = target_user_id.lower().strip()
        user   = slots.get_user(target)
        if user is None:
            return JSONResponse(
                status_code=404,
                content={"error": "user_not_found",
                         "detail": f"No user '{target}'"},
            )

        return {
            "user_id":     user.user_id,
            "slot":        user.slot,
            "security":    user.security_level,
            "persona":     user.persona,
            "summary":     user.summary,
            "messages":    user.build_messages(),
            "flag_warn":   user.flag_warn,
            "flag_crit":   user.flag_crit,
            "is_idle":     user.is_idle(),
            "history_len": len(user.conversation_history),
            "timestamp":   datetime.now().isoformat(),
        }

    # --------------------------------------------------
    # GET /health
    # --------------------------------------------------
    @app.get("/health")
    async def health():
        from main import VERSION
        return {
            "status":      "ok",
            "version":     VERSION,
            "llm_running": llm.is_running(),
            "llm_pid":     llm.get_pid(),
            "broadcast":   broadcast.is_enabled(),
            "providers":   list(providers.get_all().keys()),
            "timestamp":   datetime.now().isoformat(),
        }

    # --------------------------------------------------
    # GET /slots — show active user slot info
    # --------------------------------------------------
    @app.get("/slots")
    async def slot_status():
        users = slots.get_all_users()
        info  = {}
        for uid, user in users.items():
            info[uid] = {
                "slot":        user.slot,
                "security":    user.security_level,
                "flag_warn":   user.flag_warn,
                "flag_crit":   user.flag_crit,
                "is_idle":     user.is_idle(),
                "history_len": len(user.conversation_history),
                "has_summary": bool(user.summary),
            }
        return {"slots": info}

    # --------------------------------------------------
    # WS /channel/voice — bidirectional audio I/O
    # --------------------------------------------------
    # Protocol:
    #   client → server: binary frames (WAV audio, 16kHz mono)
    #   server → client: binary frames (WAV audio, 24kHz mono)
    #                    text frames  (JSON control events)
    #
    # Control events (server → client):
    #   {"event": "ready",      "user_id": ..., "device_id": ..., "stt": bool, "tts": bool}
    #   {"event": "transcript", "text": "..."}   — STT result
    #   {"event": "silence"}                     — VAD found no speech
    #   {"event": "text",       "data": "..."}   — TTS fallback (text only)
    #   {"event": "done"}                        — response complete
    #   {"event": "error",      "detail": "..."}
    #
    # Control events (client → server):
    #   {"event": "ping"}  → {"event": "pong"}
    # --------------------------------------------------

    @app.websocket("/channel/voice")
    async def voice_ws(
        websocket: WebSocket,
        user_id:   str        = Query(..., description="Authenticated user_id"),
        device_id: str | None = Query(None, description="Satellite device identifier"),
    ):
        await websocket.accept()

        uid = user_id.lower().strip()
        user, error = _gate1(uid)
        if error:
            await websocket.send_json({"event": "error", "detail": "unauthorized"})
            await websocket.close(code=4003)
            return

        stt = providers.get_stt()
        tts = providers.get_tts()
        stt_ready = stt is not None and stt.is_ready
        tts_ready = tts is not None and tts.is_ready

        await websocket.send_json({
            "event":     "ready",
            "user_id":   user.user_id,
            "device_id": device_id,
            "stt":       stt_ready,
            "tts":       tts_ready,
        })
        log.info("voice_ws_connected", user_id=user.user_id,
                 device_id=device_id, stt=stt_ready, tts=tts_ready)

        try:
            while True:
                msg = await websocket.receive()

                # --- binary frame: audio from client ---
                if "bytes" in msg and msg["bytes"]:
                    audio = msg["bytes"]

                    if stt is None or not stt.is_ready:
                        await websocket.send_json({
                            "event":  "error",
                            "detail": "stt_unavailable",
                        })
                        continue

                    result = await stt.transcribe(audio)

                    if not result.vad or not result.text:
                        await websocket.send_json({"event": "silence"})
                        continue

                    await websocket.send_json({
                        "event": "transcript",
                        "text":  result.text,
                    })
                    log.info("voice_ws_transcript", user_id=user.user_id,
                             preview=result.text[:60])

                    envelope = MessageEnvelope(
                        user_id=user.user_id,
                        source=Source.VOICE,
                        text=result.text,
                        device_id=device_id,
                        language=result.language,
                        stt_confidence=result.stt_confidence,
                    )

                    # stream LLM with sentence-buffered TTS
                    buf = ""
                    async for chunk in handle_stream(envelope):
                        buf += chunk
                        while True:
                            sentence, buf = _pop_sentence(buf)
                            if sentence is None:
                                break
                            await _tts_send(websocket, tts, sentence)

                    # flush trailing text (no terminal punctuation)
                    if buf.strip():
                        await _tts_send(websocket, tts, buf.strip())

                    await websocket.send_json({"event": "done"})

                # --- text frame: control message from client ---
                elif "text" in msg and msg["text"]:
                    try:
                        ctrl = json.loads(msg["text"])
                    except Exception:
                        continue
                    if ctrl.get("event") == "ping":
                        await websocket.send_json({"event": "pong"})

        except WebSocketDisconnect:
            log.info("voice_ws_disconnected", user_id=user.user_id,
                     device_id=device_id)
        except Exception as e:
            log.error("voice_ws_error", user_id=user.user_id, error=str(e))
            try:
                await websocket.send_json({"event": "error", "detail": str(e)})
                await websocket.close(code=1011)
            except Exception:
                pass

    # --------------------------------------------------
    # Catch-all 404
    # --------------------------------------------------
    @app.api_route(
        "/{path:path}",
        methods=["GET", "POST", "PUT", "PATCH", "DELETE"],
    )
    async def catch_all(request: Request, path: str):
        log.warning("route_not_found", path=f"/{path}")
        return JSONResponse(
            status_code=404,
            content={"error": f"Route /{path} not found"},
        )
