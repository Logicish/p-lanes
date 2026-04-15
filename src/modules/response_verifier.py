# modules/response_verifier.py
#
# Author:  Logicish
# Company: Logic-Ish Designs
# Date:    4/6/2026
#
# ==================================================
# Background response verifier.
# Runs in the responder phase after the local LLM
# generates a response. Non-blocking — fires as a
# background task so the user never waits on it.
#
# Pipeline:
#   1. Intent gate — only eligible intents proceed
#      (home-aware intents excluded by config)
#   2. PII gate — utility lane binary check:
#      "CLEAR" or "DIRTY". Fails safe to DIRTY.
#   3. Gemini verify — sends response text only.
#      User messages never reach Gemini.
#   4. Result — logged to /var/lib/p-lanes/verifier/
#      If flagged: warning log + file written for review.
#      Response text is never modified.
#
# Security: GUEST (0) — intent gate is the control,
#   not user level. Home-aware intents are excluded
#   regardless of who asks.
#
# Knows about: core/events (register),
#              core/pipeline (PipelineContext),
#              core/llm (call_internal),
#              core/gemini (is_available, verify).
# ==================================================

# ==================================================
# Imports
# ==================================================
import asyncio
from datetime import datetime, timezone
from pathlib import Path

import structlog
import yaml

from core.events import register
from core.pipeline import PipelineContext

log = structlog.get_logger()

_CONFIG_PATH   = Path(__file__).parent.parent / "config.yaml"
_VERIFIER_DIR  = Path("/var/lib/p-lanes/verifier")

# ==================================================
# Config
# ==================================================

def _load_verify_intents() -> frozenset[str]:
    try:
        with open(_CONFIG_PATH) as f:
            cfg = yaml.safe_load(f) or {}
        intents = cfg.get("gemini", {}).get("verify_intents", ["general", ""])
        return frozenset(intents)
    except Exception:
        return frozenset({"general", ""})

_VERIFY_INTENTS = _load_verify_intents()

# ==================================================
# PII check prompt
# ==================================================

_PII_SYSTEM = """\
You are a privacy classifier. Examine the text below and reply with a single word only.
Reply CLEAR if the text contains no personal information.
Reply DIRTY if the text contains real people's names, home addresses, room or location \
names, medical information, financial details, or any other personal data.
No explanation. One word only: CLEAR or DIRTY."""

# ==================================================
# Responder
# ==================================================

@register("response_verifier", "responder")
async def respond(ctx: PipelineContext) -> PipelineContext:
    # only verify LLM-generated responses
    if ctx.skip_processor or not ctx.response_text:
        return ctx

    if ctx.intent not in _VERIFY_INTENTS:
        return ctx

    from core import gemini
    if not gemini.is_available():
        return ctx

    # fire and forget — user already has the response
    asyncio.create_task(_verify_background(
        response_text=ctx.response_text,
        intent=ctx.intent,
        user_id=ctx.user.user_id,
        fallback_slot=ctx.user.slot,
    ))

    return ctx


# ==================================================
# Background verification pipeline
# ==================================================

async def _verify_background(
    response_text: str,
    intent:        str,
    user_id:       str,
    fallback_slot: int,
) -> None:
    try:
        # step 1: PII gate via utility lane
        pii = await _check_pii(response_text, fallback_slot)
        if pii != "CLEAR":
            log.info("verifier_pii_blocked",
                     user_id=user_id, intent=intent)
            return

        # step 2: Gemini fact-check
        from core import gemini
        result = await gemini.verify(response_text)

        if result is None:
            log.debug("verifier_skipped_no_result", user_id=user_id)
            return

        if result.strip().upper() == "OK":
            log.debug("verifier_ok", user_id=user_id, intent=intent)
            return

        # flagged — log and write for review
        log.warning("verifier_flagged",
                    user_id=user_id, intent=intent, issue=result)
        _write_flag(user_id, intent, response_text, result)

    except Exception as e:
        log.debug("verifier_background_error", error=str(e))


async def _check_pii(text: str, fallback_slot: int) -> str:
    """Ask the local utility lane whether the text contains PII.
    Returns 'CLEAR' or 'DIRTY'. Fails safe to 'DIRTY' on any error.
    """
    from core.llm import call_internal
    messages = [
        {"role": "system", "content": _PII_SYSTEM},
        {"role": "user",   "content": text},
    ]
    try:
        result = await call_internal(
            messages=messages,
            temperature=0.1,
            max_tokens=5,
            fallback_slot=fallback_slot,
        )
        word = result.content.strip().upper().split()[0]
        log.debug("verifier_pii_check", result=word)
        return word if word in ("CLEAR", "DIRTY") else "DIRTY"
    except Exception as e:
        log.debug("verifier_pii_check_failed", error=str(e))
        return "DIRTY"


# ==================================================
# Storage
# ==================================================

def _write_flag(
    user_id:       str,
    intent:        str,
    response_text: str,
    issue:         str,
) -> None:
    _VERIFIER_DIR.mkdir(parents=True, exist_ok=True)
    ts   = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    path = _VERIFIER_DIR / f"{user_id}_{ts}.md"
    lines = [
        f"# Verification Flag — {user_id} — {ts}",
        "",
        f"## Intent",
        intent or "(unclassified)",
        "",
        "## Response text",
        response_text,
        "",
        "## Gemini feedback",
        issue,
        "",
        "## Status",
        "awaiting review",
        "",
    ]
    try:
        path.write_text("\n".join(lines))
        log.debug("verifier_flag_written", path=str(path))
    except Exception as e:
        log.error("verifier_flag_write_failed", user_id=user_id, error=str(e))
