# modules/remember_this.py
#
# Author:  Logicish
# Company: Logic-Ish Designs
# Date:    4/18/2026
#
# ==================================================
# Conversational note ingestion.
# Triggered by "make a note", "remember this", etc.
# Stores to the requesting user's private RAG collection
# only — never to shared collections.
#
# Pipeline:
#   1. One call_internal: extracts note content from the
#      raw message and classifies as fact/technical/reminder
#   2. fact     → stored as-is, embedded, upserted
#      technical → second call_internal reformats to QA pair,
#                  then embedded and upserted
#      reminder  → redirect user to timer syntax
#   3. Confirm to user; skip_processor throughout
#
# Collection: user_{user_id} only.
# Document ID: note_{user_id}_{uuid4_hex[:8]}
#
# Knows about: core/events (register),
#              core/pipeline (PipelineContext),
#              core/llm (call_internal),
#              providers (get_provider, get_embedder).
# ==================================================

# ==================================================
# Imports
# ==================================================
import asyncio
import uuid
from datetime import datetime, timezone

import structlog

from core.pipeline import PipelineContext
from core.tool_registry import tool

log = structlog.get_logger()


# ==================================================
# Prompts
# ==================================================

_EXTRACT_SYSTEM = """\
The user issued a "remember this" type command to a home AI assistant.

Extract the content they want remembered and classify it. Reply in this exact format — two lines, nothing else:
TYPE: <fact|technical|reminder>
CONTENT: <the note content, stripped of the command itself>

TYPE definitions:
  fact      — a simple fact, date, preference, or named piece of information
               (birthday, address, schedule, name, password, setting)
  technical — a procedural note, warning, gotcha, wiring instruction, or how-to
               (setup steps, component notes, "don't do X", troubleshooting tips)
  reminder  — explicitly time-based ("remind me at 6pm", "don't forget tomorrow")

CONTENT must be just the thing to remember — not the trigger phrase."""

_QA_SYSTEM = """\
Convert the following technical note into a question-answer pair for a knowledge base.
Someone will search for this later using a natural question — write the Q to match how they'd actually ask.

Reply in this exact format — two lines, nothing else:
Q: <natural question that would retrieve this note>
A: <concise factual answer>"""


# ==================================================
# Enricher
# ==================================================


@tool("save_note")
async def _execute(args: dict, ctx: PipelineContext) -> str:
    # tool LLM passes the user's full original message verbatim
    raw_message = args.get("raw_message") or ctx.raw_message

    note_type, content = await _classify(raw_message, ctx.user.slot)

    if not content:
        return "What would you like me to remember?"

    if note_type == "reminder":
        return (
            "That sounds like a timed reminder — try 'remind me in X minutes' "
            "or 'set an alarm for' instead."
        )

    if note_type == "technical":
        stored_text = await _reformat_qa(content, ctx.user.slot)
        if not stored_text:
            stored_text = content
    else:
        stored_text = content

    success = await _store(
        stored_text=stored_text,
        original=content,
        note_type=note_type,
        user_id=ctx.user.user_id,
    )

    if not success:
        return "I couldn't save that right now — try again."

    log.info("save_note_tool_ok", note_type=note_type, chars=len(stored_text))
    return "Got it, saved as a reference note." if note_type == "technical" else "Got it, I'll remember that."


# ==================================================
# Classification
# ==================================================

async def _classify(raw_message: str, fallback_slot: int) -> tuple[str, str]:
    """Extract note content and classify type from the raw command.
    Returns (type, content). type is 'fact', 'technical', or 'reminder'.
    """
    from core.llm import call_internal

    messages = [
        {"role": "system", "content": _EXTRACT_SYSTEM},
        {"role": "user",   "content": raw_message},
    ]
    try:
        result = await call_internal(
            messages=messages,
            temperature=0.1,
            max_tokens=120,
            fallback_slot=fallback_slot,
        )
        return _parse_classify(result.content.strip())
    except Exception as e:
        log.warning("remember_classify_failed", error=str(e))
        return ("fact", "")


def _parse_classify(text: str) -> tuple[str, str]:
    note_type = "fact"
    content   = ""
    for line in text.splitlines():
        line = line.strip()
        if line.upper().startswith("TYPE:"):
            raw = line.split(":", 1)[1].strip().lower()
            if raw in ("fact", "technical", "reminder"):
                note_type = raw
        elif line.upper().startswith("CONTENT:"):
            content = line.split(":", 1)[1].strip()
    return note_type, content


# ==================================================
# QA reformatting (technical notes only)
# ==================================================

async def _reformat_qa(content: str, fallback_slot: int) -> str:
    """Reformat a technical note as a QA pair for better retrieval."""
    from core.llm import call_internal

    messages = [
        {"role": "system", "content": _QA_SYSTEM},
        {"role": "user",   "content": content},
    ]
    try:
        result = await call_internal(
            messages=messages,
            temperature=0.2,
            max_tokens=150,
            fallback_slot=fallback_slot,
        )
        text = result.content.strip()
        if text.upper().startswith("Q:") and "A:" in text.upper():
            return text
        log.warning("remember_qa_format_invalid", text=text[:80])
        return ""
    except Exception as e:
        log.warning("remember_qa_reformat_failed", error=str(e))
        return ""


# ==================================================
# Storage
# ==================================================

async def _store(
    stored_text: str,
    original:    str,
    note_type:   str,
    user_id:     str,
) -> bool:
    """Embed stored_text and upsert into the user's private RAG collection."""
    import providers

    rag     = providers.get_provider("rag")
    embedder = providers.get_embedder()

    if rag is None or not rag.is_ready:
        log.warning("remember_rag_unavailable")
        return False
    if embedder is None or not embedder.is_ready:
        log.warning("remember_embedder_unavailable")
        return False

    try:
        vecs = await embedder.embed_async([stored_text])
        embedding = vecs[0].tolist()
    except Exception as e:
        log.warning("remember_embed_failed", error=str(e))
        return False

    collection  = f"user_{user_id}"
    doc_id      = f"note_{user_id}_{uuid.uuid4().hex[:8]}"
    ts          = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    metadata = {
        "source_file": f"notes/{user_id}",
        "section":     f"{note_type}:{ts}",
        "note_type":   note_type,
        "original":    original[:500],
    }

    try:
        loop = asyncio.get_event_loop()
        await loop.run_in_executor(
            None,
            lambda: rag.upsert_batch(
                collection_name=collection,
                ids=[doc_id],
                embeddings=[embedding],
                documents=[stored_text],
                metadatas=[metadata],
            ),
        )
        log.debug("remember_upserted",
                  collection=collection,
                  doc_id=doc_id,
                  note_type=note_type)
        return True
    except Exception as e:
        log.warning("remember_upsert_failed", error=str(e))
        return False
