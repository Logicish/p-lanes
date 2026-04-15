# modules/semantic_router.py
#
# Author:  Logicish
# Company: Logic-Ish Designs
# Date:    3/19/2026
#
# ==================================================
# Semantic intent classifier. Runs in the classifier
# phase for every message.
#
# On first request, embeds all example phrases from
# intents.yaml and computes a normalized centroid
# vector per bucket. Subsequent requests embed the
# incoming message and find the closest bucket via
# cosine similarity (dot product on normalized vecs).
#
# Above confidence_threshold → ctx.intent set.
# Below threshold            → ctx.intent left ""
#                              (LLM handles it).
#
# Skips classification if ctx.intent is already set
# (e.g. hello_world pre-empted it).
#
# Knows about: core/events (register),
#              core/pipeline (PipelineContext),
#              providers (get_embedder).
# ==================================================

# ==================================================
# Imports
# ==================================================
from pathlib import Path

import numpy as np
import structlog
import yaml

import providers
from core.events import register
from core.pipeline import PipelineContext

log = structlog.get_logger()

_INTENTS_PATH = Path(__file__).parent / "intents.yaml"

# computed once on first request, then cached
_bucket_vectors:      dict[str, np.ndarray] | None = None
_intent_temperatures: dict[str, float]             = {}
_intent_confidence:   dict[str, float]             = {}
_threshold:  float = 0.55   # minimum score to consider a classification
_required:   float = 0.65   # minimum score for a definitive classification


# ==================================================
# Bucket initializer (lazy)
# ==================================================

async def _ensure_buckets() -> bool:
    """Embed all intent examples and compute bucket centroids.
    Called once on the first incoming request. Returns True
    on success, False if embedder is unavailable."""
    global _bucket_vectors, _threshold

    if _bucket_vectors is not None:
        return True

    embedder = providers.get_embedder()
    if embedder is None or not embedder.is_ready:
        log.warning("semantic_router_no_embedder")
        return False

    try:
        with open(_INTENTS_PATH) as f:
            cfg = yaml.safe_load(f) or {}

        _threshold            = cfg.get("confidence_threshold", 0.55)
        _required             = cfg.get("confidence_required",  0.65)
        _intent_temperatures.update(cfg.get("intent_temperatures", {}))
        _intent_confidence.update(cfg.get("intent_confidence", {}))
        buckets_raw           = cfg.get("buckets", {})
        bucket_vectors        = {}

        for intent, examples in buckets_raw.items():
            if not examples:
                continue
            vecs     = await embedder.embed_async(examples)
            centroid = np.mean(vecs, axis=0)
            norm     = np.linalg.norm(centroid)
            if norm > 0:
                centroid = centroid / norm
            bucket_vectors[intent] = centroid

        _bucket_vectors = bucket_vectors
        log.info("semantic_router_ready",
                 buckets=list(_bucket_vectors.keys()),
                 threshold=_threshold,
                 required=_required)
        return True

    except Exception as e:
        log.error("semantic_router_init_failed", error=str(e))
        return False


# ==================================================
# Classifier
# ==================================================

@register("semantic_router", "classifier")
async def classify(ctx: PipelineContext) -> PipelineContext:
    # skip if already classified by an earlier module
    if ctx.intent:
        return ctx

    embedder = providers.get_embedder()
    if embedder is None or not embedder.is_ready:
        return ctx

    if not await _ensure_buckets():
        return ctx

    # embed the incoming message
    try:
        vecs    = await embedder.embed_async([ctx.raw_message])
        msg_vec = vecs[0]
    except Exception as e:
        log.error("semantic_router_embed_failed", error=str(e))
        return ctx

    # cosine similarity — vectors are normalized so dot product suffices
    best_intent = ""
    best_score  = 0.0

    for intent, centroid in _bucket_vectors.items():
        score = float(np.dot(msg_vec, centroid))
        if score > best_score:
            best_score  = score
            best_intent = intent

    required_for_intent = _intent_confidence.get(best_intent, _required)

    if best_score >= required_for_intent:
        ctx.intent = best_intent
        ctx.tags   = [f"score:{best_score:.3f}"]
        if best_intent in _intent_temperatures:
            ctx.temperature_override = _intent_temperatures[best_intent]
        log.info("intent_classified",
                 intent=best_intent,
                 score=f"{best_score:.3f}",
                 temperature=ctx.temperature_override,
                 user_id=ctx.user.user_id)
    elif best_score >= _threshold:
        log.info("intent_low_confidence",
                 best=best_intent,
                 score=f"{best_score:.3f}",
                 user_id=ctx.user.user_id)
    else:
        log.debug("intent_below_threshold",
                  best=best_intent,
                  score=f"{best_score:.3f}",
                  user_id=ctx.user.user_id)

    return ctx
