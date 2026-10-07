# modules/verify_command.py
#
# Author:  Logicish
# Company: Logic-Ish Designs
# Date:    4/18/2026
#
# ==================================================
# On-demand response verification.
# Triggered by explicit user command ("verify that",
# "fact check that", etc.). Grabs the last assistant
# response from history, searches SearXNG for context,
# then sends both to Gemini for a grounded verdict.
#
# Falls back to local LLM if Gemini is unavailable.
# Sets skip_processor — verdict is the final response.
#
# Knows about: core/events (register),
#              core/pipeline (PipelineContext),
#              core/gemini (is_available, call),
#              core/llm (call_internal).
# ==================================================

# ==================================================
# Imports
# ==================================================
from pathlib import Path

import httpx
import structlog
import yaml

from core.pipeline import PipelineContext
from core.tool_registry import tool

log = structlog.get_logger()

_CONFIG_PATH = Path(__file__).parent.parent / "config.yaml"


# ==================================================
# Config
# ==================================================

def _load_searxng_url() -> str:
    try:
        with open(_CONFIG_PATH) as f:
            cfg = yaml.safe_load(f) or {}
        return cfg.get("web_search", {}).get("searxng_url", "http://127.0.0.1:8888/search")
    except Exception:
        return "http://127.0.0.1:8888/search"

_SEARXNG_URL = _load_searxng_url()


# ==================================================
# Prompts
# ==================================================

_SYSTEM_GEMINI = """\
You are a fact-checker. You will be given a statement to verify and web search results for grounding.

Check the statement against the search results and your knowledge.
Reply in 2-3 sentences: state whether it is accurate, and call out any specific errors or important caveats.
If the search results don't cover the topic, rely on your knowledge alone.
Be direct — no preamble."""

_SYSTEM_LOCAL = """\
You are a fact-checker. Check the following statement for factual accuracy.
Reply in 2-3 sentences: state whether it is accurate, and call out any specific errors or caveats.
Be direct — no preamble."""


# ==================================================
# Enricher
# ==================================================


@tool("fact_check")
async def _execute(args: dict, ctx: PipelineContext) -> str:
    history = ctx.user.conversation_history

    last_assistant  = None
    last_user_query = None
    for m in reversed(history):
        if m["role"] == "assistant" and last_assistant is None:
            last_assistant = m["content"]
        elif m["role"] == "user" and last_assistant is not None and last_user_query is None:
            last_user_query = m["content"]
        if last_assistant and last_user_query:
            break

    if not last_assistant:
        return "There's nothing recent to verify."

    search_query = last_user_query or last_assistant[:120]
    snippets = []
    try:
        snippets = await _searxng(search_query)
    except Exception as e:
        log.warning("fact_check_search_failed", error=str(e))

    search_block = _format_snippets(snippets)

    from core import gemini
    verdict = None
    if gemini.is_available():
        verdict = await _verify_gemini(last_assistant, search_block)
    if not verdict:
        verdict = await _verify_local(last_assistant, search_block, ctx.user.slot)

    log.info("fact_check_tool_ok", search_results=len(snippets))
    return verdict or "I wasn't able to verify that right now."


# ==================================================
# SearXNG
# ==================================================

async def _searxng(query: str, n: int = 4) -> list[dict]:
    params = {"q": query, "format": "json", "language": "en"}
    async with httpx.AsyncClient(timeout=8) as client:
        resp = await client.get(_SEARXNG_URL, params=params)
        resp.raise_for_status()
        data = resp.json()

    results = data.get("results", [])
    seen, deduped = set(), []
    for r in results:
        url = r.get("url", "")
        if url and url not in seen:
            seen.add(url)
            deduped.append(r)
    return deduped[:n]


def _format_snippets(results: list[dict]) -> str:
    if not results:
        return ""
    lines = []
    for r in results:
        title   = r.get("title", "").strip()
        snippet = r.get("content", "").strip()
        url     = r.get("url", "").strip()
        header  = f"[{title}]" if title else url
        lines.append(f"{header}: {snippet}")
    return "\n\n".join(lines)


# ==================================================
# Verification backends
# ==================================================

async def _verify_gemini(statement: str, search_block: str) -> str | None:
    from core import gemini
    user_content = f"Statement:\n{statement}"
    if search_block:
        user_content += f"\n\nWeb search results:\n{search_block}"
    messages = [
        {"role": "system", "content": _SYSTEM_GEMINI},
        {"role": "user",   "content": user_content},
    ]
    try:
        result = await gemini.call(messages, temperature=0.2, max_tokens=256)
        log.info("verify_gemini_ok", chars=len(result.content))
        return result.content.strip() or None
    except Exception as e:
        log.warning("verify_gemini_failed", error=str(e))
        return None


async def _verify_local(
    statement:     str,
    search_block:  str,
    fallback_slot: int,
) -> str | None:
    from core.llm import call_internal
    user_content = statement
    if search_block:
        user_content = (
            f"Web search results for context:\n{search_block}"
            f"\n\nStatement to verify:\n{statement}"
        )
    messages = [
        {"role": "system", "content": _SYSTEM_LOCAL},
        {"role": "user",   "content": user_content},
    ]
    try:
        result = await call_internal(
            messages=messages,
            temperature=0.2,
            max_tokens=200,
            fallback_slot=fallback_slot,
        )
        text = result.content.strip()
        log.info("verify_local_ok", chars=len(text))
        if not text:
            log.warning("verify_local_empty_response")
            return None
        return text
    except Exception as e:
        log.warning("verify_local_failed", error=str(e))
        return None
