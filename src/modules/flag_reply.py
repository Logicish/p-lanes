# modules/flag_reply.py
#
# Author:  Logicish
# Company: Logic-Ish Designs
# Date:    4/6/2026
#
# ==================================================
# Reply flagging module.
# Detects when a user marks the previous response as
# wrong or off. Stores the bad exchange to disk for
# admin review and eventual RAG correction.
#
# Trigger phrases (case-insensitive):
#   "flag that", "flag this", "that's wrong",
#   "that was wrong", "wrong answer", "that's incorrect",
#   "incorrect", "flag the last response", "mark that wrong"
#
# On trigger:
#   - Finds the last assistant message in history
#   - Writes a markdown file to /var/lib/p-lanes/flagged/
#   - Responds with a confirmation, skips LLM
#   - Does NOT modify conversation history
#
# Security: USER (1) — guests cannot flag.
# Phase:    classifier, priority 5 (before semantic router)
#
# Knows about: core/events (register),
#              core/pipeline (PipelineContext),
#              config (SecurityLevel).
# ==================================================

# ==================================================
# Imports
# ==================================================
import re
from datetime import datetime, timezone
from pathlib import Path

import structlog

from config import SecurityLevel
from core.events import register
from core.pipeline import PipelineContext

log = structlog.get_logger()

_FLAGGED_DIR = Path("/var/lib/p-lanes/flagged")

_FLAG_PATTERN = re.compile(
    r'\b('
    r'flag that|flag this|flag the last( response)?'
    r'|that\'?s wrong|that was wrong'
    r'|wrong answer|that\'?s incorrect'
    r'|mark that wrong|incorrect'
    r')\b',
    re.IGNORECASE,
)


# ==================================================
# Classifier
# ==================================================

@register("flag_reply", "classifier")
async def classify(ctx: PipelineContext) -> PipelineContext:
    if ctx.intent:
        return ctx

    if ctx.user.security_level < SecurityLevel.USER:
        return ctx

    if not _FLAG_PATTERN.search(ctx.raw_message):
        return ctx

    ctx.intent         = "flag_reply"
    ctx.skip_processor = True

    # find last assistant and user turns from history
    history = ctx.user.conversation_history
    last_assistant = next(
        (m["content"] for m in reversed(history) if m["role"] == "assistant"),
        None,
    )
    last_user = next(
        (m["content"] for m in reversed(history) if m["role"] == "user"),
        None,
    )

    if not last_assistant:
        ctx.response_text = "Nothing to flag — no previous response found."
        return ctx

    _write_flag(ctx.user.user_id, last_user, last_assistant)

    ctx.response_text = "Flagged. I'll note that response was off."
    log.info("reply_flagged", user_id=ctx.user.user_id)
    return ctx


# ==================================================
# Storage
# ==================================================

def _write_flag(user_id: str, query: str | None, response: str) -> None:
    _FLAGGED_DIR.mkdir(parents=True, exist_ok=True)
    ts    = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    path  = _FLAGGED_DIR / f"{user_id}_{ts}.md"
    lines = [
        f"# Flagged — {user_id} — {ts}",
        "",
        "## Query",
        query or "(unknown)",
        "",
        "## Bad response",
        response,
        "",
        "## Status",
        "awaiting review",
        "",
    ]
    try:
        path.write_text("\n".join(lines))
        log.debug("flag_written", path=str(path))
    except Exception as e:
        log.error("flag_write_failed", user_id=user_id, error=str(e))
