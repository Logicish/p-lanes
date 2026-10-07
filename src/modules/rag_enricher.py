# modules/rag_enricher.py
#
# Author:  Logicish
# Company: Logic-Ish Designs
# Date:    4/3/2026
#
# ==================================================
# RAG enricher module.
# Runs during the enricher phase for general /
# unclassified intents. Embeds the user message,
# searches ChromaDB for relevant context, and injects
# results into ctx.enrichments for the LLM processor.
#
# Collection access:
#   All users  → shared_home, shared_cooking,
#                shared_electronics, shared_games,
#                shared_general
#   Per user   → user_{user_id}  (private notes etc.)
#
# Skipped when:
#   - intent is not "general" / ""
#   - RAG or embeddings provider unavailable
#   - No results found (enrichments not modified)
#
# Security: GUEST (0) — everyone gets RAG context.
#
# Knows about: core/events (register),
#              core/pipeline (PipelineContext),
#              providers (get_provider, get_embedder).
# ==================================================

# ==================================================
# Imports
# ==================================================
import structlog

import providers
from core.pipeline import PipelineContext
from core.tool_registry import tool

try:
    from providers.rag.provider import ALL_SHARED_COLLECTIONS
except ImportError:
    ALL_SHARED_COLLECTIONS = [
        "shared_home", "shared_cooking", "shared_electronics",
        "shared_games", "shared_general",
    ]

log = structlog.get_logger()


@tool("local_search")
async def _execute(args: dict, ctx: PipelineContext) -> str:
    query = (args.get("query") or ctx.raw_message).strip()

    rag = providers.get_provider("rag")
    if rag is None or not rag.is_ready:
        return "Local knowledge base is unavailable right now."

    embedder = providers.get_embedder()
    if embedder is None or not embedder.is_ready:
        return "Embedding service is unavailable right now."

    routing     = rag.collection_routing
    shared_cols = routing.get("local_search", ALL_SHARED_COLLECTIONS)
    col_names   = shared_cols + [f"user_{ctx.user.user_id}"]

    try:
        vecs      = await embedder.embed_async([query])
        query_vec = vecs[0].tolist()
        results   = await rag.search(query_vec, col_names)
    except Exception as e:
        log.error("local_search_tool_failed", error=str(e))
        return "Search failed — try again."

    if not results:
        return "Nothing found in the local knowledge base for that query."

    threshold = rag.distance_threshold
    results = [r for r in results if r["distance"] < threshold]
    if not results:
        return "Nothing relevant found in the local knowledge base."

    best    = results[0]["distance"]
    results = [r for r in results if r["distance"] < best * 1.5]

    lines = []
    for r in results:
        src     = r["source_file"]
        section = r["section"]
        doc     = r["doc"]
        label   = f"{src} — {section}" if section else src
        lines.append(f"[{label}]\n{doc}")

    log.info("local_search_tool_ok", results=len(results), query_preview=query[:60])
    return "\n\n".join(lines)
