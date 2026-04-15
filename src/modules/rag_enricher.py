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
from core.events import register
from core.pipeline import PipelineContext
from providers.rag.provider import ALL_SHARED_COLLECTIONS

log = structlog.get_logger()

# Intents that should receive RAG enrichment.
# Empty string covers messages that fell through classification.
# local_search gets RAG first — web_search falls back if RAG is empty.
_RAG_INTENTS = {"general", "", "local_search"}


# ==================================================
# Enricher
# ==================================================

@register("rag_enricher", "enricher")
async def handle(ctx: PipelineContext) -> PipelineContext:
    if ctx.intent not in _RAG_INTENTS:
        return ctx

    rag = providers.get_provider("rag")
    if rag is None or not rag.is_ready:
        return ctx

    embedder = providers.get_embedder()
    if embedder is None or not embedder.is_ready:
        return ctx

    # Determine which collections this user can search (#15: collection routing)
    routing     = rag.collection_routing
    shared_cols = routing.get(ctx.intent, ALL_SHARED_COLLECTIONS)
    user_col    = f"user_{ctx.user.user_id}"
    col_names   = shared_cols + [user_col]

    try:
        vecs = await embedder.embed_async([ctx.raw_message])
        query_vec = vecs[0].tolist()

        results = await rag.search(query_vec, col_names)
        if not results:
            return ctx

        # #13: drop results that don't meet the absolute distance threshold
        threshold = rag.distance_threshold
        results = [r for r in results if r["distance"] < threshold]
        if not results:
            log.debug("rag_all_below_threshold",
                      user_id=ctx.user.user_id,
                      threshold=threshold)
            return ctx

        # #16: drop results more than 1.5x the best match distance
        best    = results[0]["distance"]
        results = [r for r in results if r["distance"] < best * 1.5]

        # Format results into a context block for the LLM
        lines = []
        for r in results:
            src     = r["source_file"]
            section = r["section"]
            doc     = r["doc"]
            label   = f"{src} — {section}" if section else src
            lines.append(f"[{label}]\n{doc}")

        content = "\n\n".join(lines)
        # #14: label tells the LLM this context is optional
        ctx.enrichments.append({
            "source":  "knowledge base — use only if relevant to the question",
            "content": content,
        })

        log.debug("rag_enriched",
                  user_id=ctx.user.user_id,
                  results=len(results),
                  best_distance=round(best, 4),
                  threshold=threshold,
                  intent=ctx.intent)

    except Exception as e:
        log.error("rag_enricher_failed", user_id=ctx.user.user_id, error=str(e))

    return ctx
