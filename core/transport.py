# core/transport.py
#
# Author:  Logicish
# Company: Logic-Ish Designs
# Date:    2/26/2026
#
# ==================================================
# FastAPI HTTP server.
# Receives requests, authenticates, identifies user,
# calls handle_message or handle_stream, returns
# response. Supports both JSON and SSE endpoints.
# GATE 1 — first security checkpoint.
#
# Knows about: config (SecurityLevel), slots (resolve,
#              get_user), llm (health, restart, session).
# ==================================================

# ==================================================
# Imports
# ==================================================
from datetime import datetime
from typing import Callable

import structlog
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field
from sse_starlette.sse import EventSourceResponse

from config import SecurityLevel
from core import slots, llm

log = structlog.get_logger()


# ==================================================
# Payload Schema
# ==================================================

class MessagePayload(BaseModel):
    user_id:         str        = "guest"
    message:         str        = Field(..., min_length=1, max_length=4096)
    conversation_id: str | None = None
    input_type:      str        = "text"    # text, voice, image
    extra:           dict | None = None

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

        try:
            result = await handle_message(user.user_id, payload.message)
        except Exception as e:
            log.error("handle_message_failed", user_id=user.user_id, error=str(e))
            return JSONResponse(
                status_code=500,
                content={"error": "internal", "detail": str(e)},
            )

        return {
            "response":  result,
            "user_id":   user.user_id,
            "timestamp": datetime.now().isoformat(),
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

        async def event_generator():
            try:
                async for chunk in handle_stream(user.user_id, payload.message):
                    yield {"event": "token", "data": chunk}
                yield {"event": "done", "data": ""}
            except Exception as e:
                log.error("stream_failed", user_id=user.user_id, error=str(e))
                yield {"event": "error", "data": str(e)}

        return EventSourceResponse(event_generator())

    # --------------------------------------------------
    # POST /llm/restart — uses shared session
    # --------------------------------------------------
    @app.post("/llm/restart")
    async def llm_restart():
        log.info("llm_restart_requested")
        success = await llm.restart()
        return {
            "success":   success,
            "timestamp": datetime.now().isoformat(),
        }

    # --------------------------------------------------
    # GET /health
    # --------------------------------------------------
    @app.get("/health")
    async def health():
        return {
            "status":      "ok",
            "llm_running": llm.is_running(),
            "llm_pid":     llm.get_pid(),
            "timestamp":   datetime.now().isoformat(),
        }

    # --------------------------------------------------
    # GET /slots — show active user slot info
    # --------------------------------------------------
    @app.get("/slots")
    async def slot_status():
        users = slots.get_all_users()
        info = {}
        for uid, user in users.items():
            info[uid] = {
                "slot":       user.slot,
                "security":   user.security_level,
                "flag_warn":  user.flag_warn,
                "flag_crit":  user.flag_crit,
                "is_idle":    user.is_idle(),
                "history_len": len(user.conversation_history),
                "has_summary": bool(user.summary),
            }
        return {"slots": info}

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