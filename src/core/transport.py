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
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Callable

import structlog
from fastapi import FastAPI, Request, Query, WebSocket, WebSocketDisconnect
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field
from sse_starlette.sse import EventSourceResponse

import providers
from config import SecurityLevel, USER_PASSWORDS, DEVICE_DOMAIN_PERMISSIONS, USER_WEB_ENABLED
from core import slots, llm, broadcast, auth, turn_log, gpu_guard
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


class SnoozePayload(BaseModel):
    hours: float = Field(..., gt=0, le=168)    # max 1 week

    model_config = {
        "extra": "forbid",
    }


class DeviceControlPayload(BaseModel):
    service:      str        = Field(..., min_length=1, max_length=64)
    service_data: dict | None = None

    model_config = {
        "extra": "forbid",
        "str_strip_whitespace": True,
    }


class LoginPayload(BaseModel):
    user_id:  str = Field(..., min_length=1, max_length=64)
    password: str = Field(..., min_length=1, max_length=256)


class WorkoutExercisePayload(BaseModel):
    name:         str
    category:     str
    muscle_group: str | None = None
    equipment:    str | None = None

    model_config = {"extra": "forbid", "str_strip_whitespace": True}


class WorkoutSetPayload(BaseModel):
    exercise_id:  int
    set_order:    int
    reps:         int   | None = None
    weight:       float | None = None
    distance_m:   float | None = None
    duration_sec: int   | None = None

    model_config = {"extra": "forbid"}


class WorkoutSessionPayload(BaseModel):
    date:         str
    name:         str | None = None
    notes:        str | None = None
    duration_min: int | None = None
    sets:         list[WorkoutSetPayload] = []

    model_config = {"extra": "forbid", "str_strip_whitespace": True}

    model_config = {
        "extra": "forbid",
        "str_strip_whitespace": True,
    }


class RefreshPayload(BaseModel):
    refresh_token: str = Field(..., min_length=1)

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
    # Accepts either a Bearer token (Authorization header) or a plain
    # user_id string. Token path is used by the SPA; user_id path is
    # used by satellites and internal callers.
    async def _gate1(user_id: str, request: Request | None = None):
        if request is not None:
            auth_header = request.headers.get("Authorization", "")
            if auth_header.startswith("Bearer "):
                token = auth_header[7:].strip()
                resolved_uid = await auth.verify_token(token)
                if resolved_uid is None:
                    log.warning("gate1_invalid_token")
                    return None, JSONResponse(
                        status_code=401,
                        content={"error": "invalid_token"},
                    )
                user_id = resolved_uid

        resolved = slots.resolve_user(user_id)
        if resolved is None:
            log.warning("gate1_reject_unknown", user_id=user_id)
            return None, JSONResponse(
                status_code=403,
                content={"error": "unknown_user", "detail": "Not in slot map"},
            )

        if not USER_WEB_ENABLED.get(resolved, True):
            log.warning("gate1_web_locked", user_id=resolved)
            return None, JSONResponse(
                status_code=403,
                content={"error": "access_disabled"},
            )

        user = slots.get_user(resolved)
        if user is None or user.security_level < SecurityLevel.GUEST:
            log.warning("gate1_reject_access", user_id=resolved)
            return None, JSONResponse(
                status_code=403,
                content={"error": "access_denied"},
            )

        return user, None

    # --- turn_log hook for Gate 1 rejections on message routes ---
    def _log_gate_reject(error: JSONResponse, requested: str, text: str | None,
                         source: str | None, device_id: str | None) -> None:
        try:
            reason = json.loads(error.body).get("error", "rejected")
        except Exception:
            reason = "rejected"
        turn_log.log_denied(requested, reason, text=text, source=source, device_id=device_id)

    # --- USER gate helper — minimum level for API/portal routes ---
    async def _gate_user(request: Request):
        user, error = await _gate1("", request)
        if error:
            return None, error
        if user.security_level < SecurityLevel.USER:
            return None, JSONResponse(
                status_code=401,
                content={"error": "login_required"},
            )
        return user, None

    # --- ADMIN gate helper ---
    async def _gate_admin(user_id: str, request: Request | None = None):
        user, error = await _gate1(user_id, request)
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
    async def chat_json(payload: MessagePayload, request: Request):
        user_id = payload.user_id.lower().strip()
        preview = payload.message[:60] + ("..." if len(payload.message) > 60 else "")
        log.info("request_chat", user_id=user_id, preview=preview)

        user, error = await _gate1(user_id, request)
        if error:
            _log_gate_reject(error, user_id, payload.message,
                             payload.input_type, payload.device_id)
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
    async def chat_stream(payload: MessagePayload, request: Request):
        user_id = payload.user_id.lower().strip()
        preview = payload.message[:60] + ("..." if len(payload.message) > 60 else "")
        log.info("request_stream", user_id=user_id, preview=preview)

        user, error = await _gate1(user_id, request)
        if error:
            _log_gate_reject(error, user_id, payload.message,
                             payload.input_type, payload.device_id)
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
        request: Request,
        target_user_id: str,
        user_id: str = Query("", description="Authenticated user_id (or use Bearer token)"),
    ):
        if not broadcast.is_enabled():
            return JSONResponse(
                status_code=503,
                content={"error": "broadcast_disabled",
                         "detail": "Broadcast is not enabled. A module must enable it."},
            )

        user, error = await _gate1(user_id.lower().strip(), request)
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
    async def llm_restart(payload: AdminPayload, request: Request):
        user_id = payload.user_id.lower().strip()
        log.info("llm_restart_requested", user_id=user_id)

        user, error = await _gate_admin(user_id, request)
        if error:
            return error

        # also clears gpu_guard / recovery holds; refuses while the GPU is hot or gone
        success, detail = await gpu_guard.manual_restart()
        return {
            "success":   success,
            "detail":    detail,
            "user_id":   user.user_id,
            "timestamp": datetime.now().isoformat(),
        }

    # --------------------------------------------------
    # GET /admin/dump — all users prompt dump
    # --------------------------------------------------
    @app.get("/admin/dump")
    async def admin_dump_all(
        request: Request,
        user_id: str = Query("", description="ADMIN user_id (or use Bearer token)"),
    ):
        admin, error = await _gate_admin(user_id.lower().strip(), request)
        if error:
            return error

        users = slots.get_all_users()
        dump  = {}
        for uid, user in users.items():
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
        request: Request,
        target_user_id: str,
        user_id: str = Query("", description="ADMIN user_id (or use Bearer token)"),
    ):
        admin, error = await _gate_admin(user_id.lower().strip(), request)
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
    # POST /admin/entity-index/refresh — ADMIN-gated
    # --------------------------------------------------
    @app.post("/admin/entity-index/refresh")
    async def entity_index_refresh(payload: AdminPayload, request: Request):
        user_id = payload.user_id.lower().strip()
        admin, error = await _gate_admin(user_id, request)
        if error:
            return error

        from modules.entity_enricher import reset_index
        reset_index()
        log.info("entity_index_reset", user_id=admin.user_id)
        return {
            "success":   True,
            "detail":    "Entity index cleared — will rebuild on next device request.",
            "user_id":   admin.user_id,
            "timestamp": datetime.now().isoformat(),
        }

    # --------------------------------------------------
    # POST /admin/rag/ingest — ADMIN-gated
    # --------------------------------------------------
    @app.post("/admin/rag/ingest")
    async def rag_ingest(payload: AdminPayload, request: Request):
        user_id = payload.user_id.lower().strip()
        admin, error = await _gate_admin(user_id, request)
        if error:
            return error

        rag = providers.get_provider("rag")
        if rag is None or not rag.is_ready:
            return JSONResponse(status_code=503, content={"error": "RAG provider not ready"})

        await rag.trigger_ingest()
        log.info("rag_ingest_triggered", user_id=admin.user_id)
        return {
            "success":   True,
            "detail":    "RAG ingest task started in background.",
            "user_id":   admin.user_id,
            "timestamp": datetime.now().isoformat(),
        }

    # --------------------------------------------------
    # POST /admin/users/{user_id}/web-lock — ADMIN-gated
    # Body: {"user_id": "<admin>", "locked": true|false}
    # --------------------------------------------------
    class WebLockPayload(BaseModel):
        model_config = {"str_strip_whitespace": True}
        user_id: str = ""
        locked:  bool = Field(...)

    @app.post("/admin/users/{target_uid}/web-lock")
    async def web_lock(target_uid: str, payload: WebLockPayload, request: Request):
        admin, error = await _gate_admin(payload.user_id, request)
        if error:
            return error

        target = target_uid.lower().strip()
        if target not in USER_WEB_ENABLED:
            return JSONResponse(
                status_code=404,
                content={"error": "unknown_user", "detail": f"'{target}' not in slot map"},
            )

        USER_WEB_ENABLED[target] = not payload.locked

        _users_path = Path(__file__).parent.parent / "users.yaml"
        try:
            import yaml as _yaml
            with open(_users_path) as f:
                data = _yaml.safe_load(f)
            data["users"][target]["web_enabled"] = not payload.locked
            tmp = _users_path.with_suffix(".yaml.tmp")
            with open(tmp, "w") as f:
                _yaml.dump(data, f, default_flow_style=False, allow_unicode=True)
            tmp.replace(_users_path)
        except Exception as e:
            log.error("web_lock_persist_failed", target=target, error=str(e))

        log.info("web_lock_updated", admin=admin.user_id, target=target, locked=payload.locked)
        return {
            "success":    True,
            "user_id":    target,
            "web_enabled": not payload.locked,
            "timestamp":  datetime.now().isoformat(),
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
            "llm_hold":    llm.hold_reason(),
            "gpu_guard":   gpu_guard.status(),
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
        user_id:   str | None = Query(None, description="user_id (satellite use)"),
        token:     str | None = Query(None, description="Session token (SPA use)"),
        device_id: str | None = Query(None, description="Satellite device identifier"),
    ):
        await websocket.accept()

        # resolve identity from token or user_id
        if token:
            resolved_uid = await auth.verify_token(token)
            if resolved_uid is None:
                turn_log.log_denied("", "invalid_token", source="voice", device_id=device_id)
                await websocket.send_json({"event": "error", "detail": "unauthorized"})
                await websocket.close(code=4003)
                return
            uid = resolved_uid
        elif user_id:
            uid = user_id.lower().strip()
        else:
            await websocket.send_json({"event": "error", "detail": "no_credentials"})
            await websocket.close(code=4003)
            return

        user, error = await _gate1(uid)
        if error:
            _log_gate_reject(error, uid, None, "voice", device_id)
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
    # GET /api/devices — list HA entities for device tab
    # --------------------------------------------------
    @app.get("/api/devices")
    async def api_devices(request: Request):
        user, error = await _gate_user(request)
        if error:
            return error

        ha = providers.get_provider("homeassistant")
        if ha is None or not ha.is_ready:
            return JSONResponse(status_code=503, content={"error": "ha_unavailable"})

        states = await ha.get_states()
        # apply cumulative domain permissions for this user's level
        allowed_domains: set[str] = set()
        for level in sorted(DEVICE_DOMAIN_PERMISSIONS.keys()):
            if user.security_level >= level:
                allowed_domains.update(DEVICE_DOMAIN_PERMISSIONS[level])

        devices = []
        for s in states:
            if s["entity_id"] in ha.exclude_entity_ids:
                continue
            if s["entity_id"] in ha.hidden_entity_ids:
                continue
            domain = s["entity_id"].split(".")[0]
            if domain not in allowed_domains:
                continue
            attr = s.get("attributes", {})
            devices.append({
                "entity_id":     s["entity_id"],
                "domain":        domain,
                "state":         s["state"],
                "friendly_name": attr.get("friendly_name", s["entity_id"]),
                "brightness":    attr.get("brightness"),
                "color_temp":    attr.get("color_temp"),
                "temperature":   attr.get("temperature"),
                "hvac_mode":     attr.get("hvac_mode"),
            })

        return {"devices": devices, "user_level": user.security_level}

    # --------------------------------------------------
    # POST /api/devices/{entity_id} — control a device
    # --------------------------------------------------
    @app.post("/api/devices/{entity_id}")
    async def api_device_control(
        entity_id: str,
        payload:   DeviceControlPayload,
        request:   Request,
    ):
        user, error = await _gate_user(request)
        if error:
            return error

        domain = entity_id.split(".")[0]

        # find minimum required level for this domain
        required_level = None
        for level in sorted(DEVICE_DOMAIN_PERMISSIONS.keys()):
            if domain in DEVICE_DOMAIN_PERMISSIONS[level]:
                required_level = level
                break

        if required_level is not None and user.security_level < required_level:
            log.warning("device_control_denied",
                        user_id=user.user_id, domain=domain,
                        required=required_level, actual=user.security_level)
            return JSONResponse(
                status_code=403,
                content={"error": "insufficient_permission",
                         "detail": f"Domain '{domain}' requires level {required_level}"},
            )

        ha = providers.get_provider("homeassistant")
        if ha is None or not ha.is_ready:
            return JSONResponse(status_code=503, content={"error": "ha_unavailable"})

        ok = await ha.call_service(
            domain=domain,
            service=payload.service,
            entity_id=entity_id,
            service_data=payload.service_data or {},
        )

        log.info("device_control", user_id=user.user_id,
                 entity_id=entity_id, service=payload.service, ok=ok)
        return {"success": ok, "entity_id": entity_id, "service": payload.service}

    # --------------------------------------------------
    # POST /auth/login
    # --------------------------------------------------
    @app.post("/auth/login")
    async def login(payload: LoginPayload):
        uid = payload.user_id.lower().strip()
        stored_hash = USER_PASSWORDS.get(uid, "")

        if not stored_hash or not auth.verify_password(payload.password, stored_hash):
            log.warning("login_failed", user_id=uid)
            return JSONResponse(
                status_code=401,
                content={"error": "invalid_credentials"},
            )

        if not USER_WEB_ENABLED.get(uid, True):
            log.warning("login_web_locked", user_id=uid)
            return JSONResponse(
                status_code=403,
                content={"error": "access_disabled"},
            )

        user = slots.get_user(uid)
        if user is None or user.security_level < SecurityLevel.USER:
            log.warning("login_denied_insufficient_level", user_id=uid)
            return JSONResponse(
                status_code=401,
                content={"error": "invalid_credentials"},
            )

        session = await auth.create_session(uid)
        log.info("login_success", user_id=uid)
        return {
            **session,
            "user_id":        user.user_id,
            "security_level": user.security_level,
        }

    # --------------------------------------------------
    # POST /auth/refresh
    # --------------------------------------------------
    @app.post("/auth/refresh")
    async def token_refresh(payload: RefreshPayload):
        session = await auth.refresh_session(payload.refresh_token)
        if session is None:
            return JSONResponse(
                status_code=401,
                content={"error": "invalid_refresh_token"},
            )
        return session

    # --------------------------------------------------
    # POST /auth/logout
    # --------------------------------------------------
    @app.post("/auth/logout")
    async def logout(request: Request):
        auth_header = request.headers.get("Authorization", "")
        if auth_header.startswith("Bearer "):
            await auth.revoke_session(auth_header[7:].strip())
        return {"success": True}

    # --------------------------------------------------
    # GET /notifications — unread + recent alerts
    # --------------------------------------------------
    @app.get("/notifications")
    async def get_notifications(request: Request):
        user, error = await _gate_user(request)
        if error:
            return error

        db = providers.get_db("system")
        if db is None or not db.is_ready:
            return JSONResponse(status_code=503, content={"error": "db_unavailable"})

        rows = await db.fetchall(
            "SELECT * FROM notifications ORDER BY created_at DESC LIMIT 50"
        )
        unread = sum(1 for r in rows if r.get("read_at") is None)
        return {"notifications": rows, "unread_count": unread}

    # --------------------------------------------------
    # POST /notifications/read-all — mark all read
    # (defined before /{id} routes to avoid path collision)
    # --------------------------------------------------
    @app.post("/notifications/read-all")
    async def notifications_read_all(request: Request):
        user, error = await _gate_user(request)
        if error:
            return error

        db = providers.get_db("system")
        if db is None or not db.is_ready:
            return JSONResponse(status_code=503, content={"error": "db_unavailable"})

        ts = datetime.now().isoformat()
        await db.execute(
            "UPDATE notifications SET read_at = ? WHERE read_at IS NULL", (ts,)
        )
        return {"success": True}

    # --------------------------------------------------
    # POST /notifications/{id}/read — mark one read
    # --------------------------------------------------
    @app.post("/notifications/{notif_id}/read")
    async def notification_read(notif_id: int, request: Request):
        user, error = await _gate_user(request)
        if error:
            return error

        db = providers.get_db("system")
        if db is None or not db.is_ready:
            return JSONResponse(status_code=503, content={"error": "db_unavailable"})

        await db.execute(
            "UPDATE notifications SET read_at = ? WHERE id = ?",
            (datetime.now().isoformat(), notif_id),
        )
        return {"success": True}

    # --------------------------------------------------
    # POST /notifications/{id}/snooze — snooze alert rule (ADMIN)
    # --------------------------------------------------
    @app.post("/notifications/{notif_id}/snooze")
    async def notification_snooze(
        notif_id: int,
        payload:  SnoozePayload,
        request:  Request,
    ):
        admin, error = await _gate_admin("", request)
        if error:
            return error

        db = providers.get_db("system")
        if db is None or not db.is_ready:
            return JSONResponse(status_code=503, content={"error": "db_unavailable"})

        notif = await db.fetchone(
            "SELECT * FROM notifications WHERE id = ?", (notif_id,)
        )
        if not notif:
            return JSONResponse(status_code=404, content={"error": "not_found"})

        rule_id      = f"{notif['type']}:{notif['host']}" if notif.get("host") else notif["type"]
        snooze_until = (datetime.now(timezone.utc) + timedelta(hours=payload.hours)).isoformat()

        await db.execute(
            """
            INSERT INTO alert_state (rule_id, snoozed_until)
            VALUES (?, ?)
            ON CONFLICT(rule_id) DO UPDATE SET snoozed_until = excluded.snoozed_until
            """,
            (rule_id, snooze_until),
        )
        await db.execute(
            "UPDATE notifications SET read_at = ? WHERE id = ?",
            (datetime.now().isoformat(), notif_id),
        )

        log.info("alert_snoozed", rule_id=rule_id, hours=payload.hours, user_id=admin.user_id)
        return {"success": True, "rule_id": rule_id, "snoozed_until": snooze_until}

    # --------------------------------------------------
    # GET /api/workouts/exercises — exercise library
    # --------------------------------------------------
    @app.get("/api/workouts/exercises")
    async def api_workout_exercises(request: Request):
        user, error = await _gate_user(request)
        if error:
            return error
        db = providers.get_db(user.user_id)
        rows = await db.fetchall(
            "SELECT id, name, category, muscle_group, equipment, is_custom FROM exercises ORDER BY category, name"
        )
        return {"exercises": rows}

    # --------------------------------------------------
    # POST /api/workouts/exercises — add custom exercise
    # --------------------------------------------------
    @app.post("/api/workouts/exercises")
    async def api_workout_exercise_add(payload: WorkoutExercisePayload, request: Request):
        user, error = await _gate_user(request)
        if error:
            return error
        db = providers.get_db(user.user_id)
        existing = await db.fetchone("SELECT id FROM exercises WHERE name = ?", (payload.name,))
        if existing:
            return JSONResponse(status_code=409, content={"error": "exercise_exists", "id": existing["id"]})
        row_id = await db.execute(
            "INSERT INTO exercises (name, category, muscle_group, equipment, is_custom) VALUES (?, ?, ?, ?, 1)",
            (payload.name, payload.category, payload.muscle_group, payload.equipment),
        )
        log.info("workout_exercise_added", name=payload.name, user_id=user.user_id)
        return {"id": row_id, "name": payload.name, "category": payload.category,
                "muscle_group": payload.muscle_group, "equipment": payload.equipment, "is_custom": 1}

    # --------------------------------------------------
    # GET /api/workouts/sessions — session history
    # --------------------------------------------------
    @app.get("/api/workouts/sessions")
    async def api_workout_sessions(request: Request, limit: int = 30):
        user, error = await _gate_user(request)
        if error:
            return error
        db = providers.get_db(user.user_id)
        sessions = await db.fetchall(
            """
            SELECT s.id, s.date, s.name, s.notes, s.duration_min, s.created_at,
                   COUNT(DISTINCT ws.exercise_id) AS exercise_count,
                   COUNT(ws.id)                  AS set_count,
                   ROUND(SUM(COALESCE(ws.weight,0) * COALESCE(ws.reps,0)), 1) AS total_volume
            FROM workout_sessions s
            LEFT JOIN workout_sets ws ON ws.session_id = s.id
            GROUP BY s.id
            ORDER BY s.date DESC, s.created_at DESC
            LIMIT ?
            """,
            (limit,),
        )
        return {"sessions": sessions}

    # --------------------------------------------------
    # POST /api/workouts/sessions — log a session
    # --------------------------------------------------
    @app.post("/api/workouts/sessions")
    async def api_workout_session_create(payload: WorkoutSessionPayload, request: Request):
        user, error = await _gate_user(request)
        if error:
            return error
        db = providers.get_db(user.user_id)
        session_id = await db.execute(
            "INSERT INTO workout_sessions (date, name, notes, duration_min) VALUES (?, ?, ?, ?)",
            (payload.date, payload.name, payload.notes, payload.duration_min),
        )
        for s in payload.sets:
            await db.execute(
                """INSERT INTO workout_sets
                   (session_id, exercise_id, set_order, reps, weight, distance_m, duration_sec)
                   VALUES (?, ?, ?, ?, ?, ?, ?)""",
                (session_id, s.exercise_id, s.set_order, s.reps, s.weight, s.distance_m, s.duration_sec),
            )
        log.info("workout_session_saved", session_id=session_id, sets=len(payload.sets), user_id=user.user_id)
        return {"id": session_id}

    # --------------------------------------------------
    # GET /api/workouts/sessions/{session_id} — detail
    # --------------------------------------------------
    @app.get("/api/workouts/sessions/{session_id}")
    async def api_workout_session_detail(session_id: int, request: Request):
        user, error = await _gate_user(request)
        if error:
            return error
        db = providers.get_db(user.user_id)
        session = await db.fetchone("SELECT * FROM workout_sessions WHERE id = ?", (session_id,))
        if not session:
            return JSONResponse(status_code=404, content={"error": "not_found"})
        sets = await db.fetchall(
            """
            SELECT ws.*, e.name AS exercise_name, e.category, e.muscle_group
            FROM workout_sets ws
            JOIN exercises e ON e.id = ws.exercise_id
            WHERE ws.session_id = ?
            ORDER BY ws.set_order
            """,
            (session_id,),
        )
        return {"session": session, "sets": sets}

    # --------------------------------------------------
    # DELETE /api/workouts/sessions/{session_id}
    # --------------------------------------------------
    @app.delete("/api/workouts/sessions/{session_id}")
    async def api_workout_session_delete(session_id: int, request: Request):
        user, error = await _gate_user(request)
        if error:
            return error
        db = providers.get_db(user.user_id)
        session = await db.fetchone("SELECT id FROM workout_sessions WHERE id = ?", (session_id,))
        if not session:
            return JSONResponse(status_code=404, content={"error": "not_found"})
        await db.execute("DELETE FROM workout_sets WHERE session_id = ?", (session_id,))
        await db.execute("DELETE FROM workout_sessions WHERE id = ?", (session_id,))
        log.info("workout_session_deleted", session_id=session_id, user_id=user.user_id)
        return {"success": True}

    # --------------------------------------------------
    # /app — SPA static files
    # --------------------------------------------------
    _web_dir = Path(__file__).parent.parent / "web"
    _web_dir.mkdir(exist_ok=True)
    app.mount("/app", StaticFiles(directory=str(_web_dir), html=True), name="spa")

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
