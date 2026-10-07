# modules/web_search.py
#
# Author:  Logicish
# Company: Logic-Ish Designs
# Date:    4/7/2026
#
# ==================================================
# Web search enricher.
# Runs during the enricher phase for web_search intent.
# Queries local SearXNG for results, injects top N
# snippets into ctx.enrichments. Optionally fetches
# full page content via Jina Reader when a snippet
# is too short to be useful.
#
# Pipeline:
#   1. Intent gate — only web_search intent proceeds
#   2. SearXNG query — JSON API, configurable result count
#   3. Jina fetch (optional, async) — for thin snippets
#   4. Inject results into ctx.enrichments for LLM
#
# Security: GUEST (0) — everyone can search the web.
#
# Knows about: core/events (register),
#              core/pipeline (PipelineContext),
#              core/secrets (get_secret).
# ==================================================

# ==================================================
# Imports
# ==================================================
import asyncio
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

def _load_config() -> dict:
    try:
        with open(_CONFIG_PATH) as f:
            cfg = yaml.safe_load(f) or {}
        return cfg.get("web_search", {})
    except Exception:
        return {}

_CFG = _load_config()

_SEARXNG_URL          = _CFG.get("searxng_url",          "http://127.0.0.1:8888/search")


def _load_home_location() -> str:
    try:
        with open(_CONFIG_PATH) as f:
            cfg = yaml.safe_load(f) or {}
        return cfg.get("home", {}).get("location", "")
    except Exception:
        return ""

_HOME_LOCATION = _load_home_location()
_RESULT_COUNT         = _CFG.get("result_count",         5)
_TIMEOUT              = _CFG.get("timeout",              8)
_JINA_ENABLED         = _CFG.get("jina_enabled",         True)
_JINA_URL             = _CFG.get("jina_url",             "https://r.jina.ai")
_JINA_TIMEOUT         = _CFG.get("jina_timeout",         10)
_JINA_SNIPPET_THRESH  = _CFG.get("jina_snippet_threshold", 150)
_JINA_MAX_CHARS       = _CFG.get("jina_max_chars",       2000)


def _get_jina_headers() -> dict:
    """Build Jina request headers. API key is optional."""
    headers = {"Accept": "text/plain", "X-Return-Format": "text"}
    try:
        from core.secrets import get_secret
        key = get_secret("jina_api_key")
        if key:
            headers["Authorization"] = f"Bearer {key}"
    except Exception:
        pass
    return headers


# ==================================================
# Enricher
# ==================================================



@tool("web_search")
async def _execute(args: dict, ctx: PipelineContext) -> str:
    query = args.get("query") or ctx.raw_message.strip()
    if not query:
        return "No search query provided."

    if args.get("location_context") and _HOME_LOCATION:
        query = f"{query} in {_HOME_LOCATION}"
        log.debug("web_search_tool_location_injected", location=_HOME_LOCATION)

    try:
        results = await _search(query)
    except Exception as e:
        log.warning("web_search_tool_failed", error=str(e))
        return "Web search failed — try again in a moment."

    if not results:
        return "No results found for that query."

    if _JINA_ENABLED:
        results = await _enrich_thin_snippets(results)

    lines = []
    for r in results:
        title   = r.get("title", "").strip()
        url     = r.get("url", "").strip()
        content = r.get("content", "").strip()
        header  = f"[{title}]({url})" if title else url
        lines.append(f"{header}\n{content}")

    log.info("web_search_tool_ok", results=len(results), query_preview=query[:60])
    return "\n\n".join(lines)


# ==================================================
# SearXNG
# ==================================================

async def _search(query: str) -> list[dict]:
    """Query SearXNG JSON API. Returns list of result dicts."""
    params = {
        "q":       query,
        "format":  "json",
        "language": "en",
    }
    async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
        resp = await client.get(_SEARXNG_URL, params=params)
        resp.raise_for_status()
        data = resp.json()

    results = data.get("results", [])

    # Deduplicate by URL (SearXNG can return same URL from multiple engines)
    seen = set()
    deduped = []
    for r in results:
        url = r.get("url", "")
        if url and url not in seen:
            seen.add(url)
            deduped.append(r)

    return deduped[:_RESULT_COUNT]


# ==================================================
# Jina Reader
# ==================================================

async def _enrich_thin_snippets(results: list[dict]) -> list[dict]:
    """For results whose snippet is below threshold, fetch full content via Jina."""
    tasks = []
    indices = []

    for i, r in enumerate(results):
        snippet = r.get("content", "")
        if len(snippet) < _JINA_SNIPPET_THRESH:
            tasks.append(_fetch_jina(r.get("url", "")))
            indices.append(i)

    if not tasks:
        return results

    fetched = await asyncio.gather(*tasks, return_exceptions=True)

    for i, idx in enumerate(indices):
        result = fetched[i]
        if isinstance(result, str) and result:
            results[idx]["content"] = result

    return results


async def _fetch_jina(url: str) -> str:
    """Fetch full page content via Jina Reader. Returns cleaned text or empty string."""
    if not url:
        return ""
    try:
        jina_url = f"{_JINA_URL}/{url}"
        headers  = _get_jina_headers()
        async with httpx.AsyncClient(timeout=_JINA_TIMEOUT) as client:
            resp = await client.get(jina_url, headers=headers, follow_redirects=True)
            resp.raise_for_status()
        text = resp.text.strip()
        log.debug("jina_fetch_ok", url=url[:80], chars=len(text))
        return text[:_JINA_MAX_CHARS]
    except Exception as e:
        log.debug("jina_fetch_failed", url=url[:80], error=str(e))
        return ""
