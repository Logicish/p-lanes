# modules/entity_enricher.py
#
# Author:  Logicish
# Company: Logic-Ish Designs
# Date:    4/3/2026
#
# ==================================================
# Enricher — shared HA entity resolver for all device
# and media intents. Runs at priority 10 so it always
# executes before intent-specific action modules.
#
# Builds a lazy embedding index of HA entity friendly
# names, area names, and aliases. On each matching
# request, embeds the message and finds the top targets
# via cosine similarity.
#
# Writes structured match data to ctx.metadata["resolved_entities"]
# for downstream modules (device_control, media_control, etc.)
# to consume. Nothing is injected into ctx.enrichments —
# this data is structured, not LLM prompt text.
#
# Early security gate: users below level 1 (GUEST) are
# skipped immediately — no wasted RAG work.
#
# Index rebuilds automatically after REFRESH_INTERVAL
# seconds so new devices are picked up without restart.
#
# Knows about: core/events (register),
#              core/pipeline (PipelineContext),
#              config (SecurityLevel),
#              providers (get_provider, get_embedder).
# ==================================================

# ==================================================
# Imports
# ==================================================
import asyncio
import re
import time

import numpy as np
import structlog

import providers
from config import SecurityLevel, SLOT_MAP
from core.events import register
from core.pipeline import PipelineContext

log = structlog.get_logger()

_REFRESH_INTERVAL = 300   # seconds — rebuild index after 5 minutes
_MIN_SCORE        = 0.50  # minimum similarity to include a match
_TOP_N            = 3     # max matches to inject

_DEVICE_INTENTS = {"device_control", "media_control"}

# first-person room references to substitute with the user's configured area
_SELF_ROOM_PATTERNS = re.compile(
    r'\b(my room|my bedroom|my space|my area)\b',
    re.IGNORECASE,
)


def _normalize_query(text: str, user_area: str = "") -> str:
    """Fix missing apostrophes in possessives for known user names,
    and expand first-person room references to the user's area."""
    for name in SLOT_MAP:
        text = re.sub(
            rf'\b{re.escape(name)}s\b',
            f"{name}'s",
            text,
            flags=re.IGNORECASE,
        )
    if user_area:
        area_display = user_area.replace("_", " ")
        text = _SELF_ROOM_PATTERNS.sub(area_display, text)
    return text

# index state
_entries:  list[dict] | None = None
_vectors:  np.ndarray | None = None
_built_at: float             = 0.0


# ==================================================
# Index Builder
# ==================================================

async def _build_index() -> bool:
    global _entries, _vectors, _built_at

    ha       = providers.get_provider("homeassistant")
    embedder = providers.get_embedder()

    if ha is None or not ha.is_ready:
        log.warning("entity_enricher_ha_unavailable")
        return False
    if embedder is None or not embedder.is_ready:
        log.warning("entity_enricher_no_embedder")
        return False

    states, registry = await asyncio.gather(
        ha.get_states(domains=[
            "light", "switch", "climate", "lock",
            "cover", "fan", "input_boolean", "media_player",
        ]),
        ha.get_entity_registry(),
    )

    if not states:
        log.warning("entity_enricher_no_states")
        return False

    # drop explicitly excluded entities (e.g. satellite LEDs)
    excluded = ha.exclude_entity_ids
    if excluded:
        before = len(states)
        states = [s for s in states if s["entity_id"] not in excluded]
        log.debug("entity_enricher_excluded", count=before - len(states))

    entity_ids      = [s["entity_id"] for s in states]
    areas_by_entity = await ha.get_areas_for_entities(entity_ids)

    entries: list[dict]       = []
    area_map: dict[str, dict] = {}

    for s in states:
        eid     = s["entity_id"]
        domain  = eid.split(".")[0]
        name    = s.get("attributes", {}).get("friendly_name", eid)
        reg     = registry.get(eid, {})
        aliases = reg.get("aliases", []) or []

        area_name = areas_by_entity.get(eid, "")
        if not area_name:
            area_id_slug = reg.get("area_id", "")
            area_name = area_id_slug.replace("_", " ").title() if area_id_slug else ""
        area_id = area_name.lower().replace(" ", "_") if area_name else ""

        base = {
            "entity_id": eid,
            "domain":    domain,
            "name":      name,
            "area_id":   area_id,
            "area_name": area_name,
        }

        entries.append({"type": "entity", "text": name, **base})

        for alias in aliases:
            if alias:
                entries.append({"type": "entity", "text": alias, **base})

        if area_id:
            if area_id not in area_map:
                area_map[area_id] = {
                    "type":          "area",
                    "text":          area_name,
                    "area_id":       area_id,
                    "area_name":     area_name,
                    "area_entities": [],
                }
            area_map[area_id]["area_entities"].append(base)

    entries.extend(area_map.values())

    if not entries:
        return False

    texts = [e["text"] for e in entries]
    try:
        vecs  = await embedder.embed_async(texts)
        norms = np.linalg.norm(vecs, axis=1, keepdims=True)
        norms = np.where(norms == 0, 1, norms)
        vecs  = vecs / norms
    except Exception as e:
        log.error("entity_enricher_embed_failed", error=str(e))
        return False

    _entries  = entries
    _vectors  = vecs
    _built_at = time.time()

    log.info("entity_enricher_ready",
             entities=sum(1 for e in entries if e["type"] == "entity"),
             areas=sum(1 for e in entries if e["type"] == "area"))
    return True


async def _ensure_index() -> bool:
    if _entries is not None and (time.time() - _built_at) < _REFRESH_INTERVAL:
        return True
    return await _build_index()


def reset_index() -> None:
    """Force the index to rebuild on the next request. Admin use only."""
    global _built_at
    _built_at = 0.0


# ==================================================
# Enricher
# ==================================================

@register("entity_enricher", "enricher")
async def enrich(ctx: PipelineContext) -> PipelineContext:
    if ctx.intent not in _DEVICE_INTENTS:
        return ctx

    # early security gate — no wasted work for guests
    if ctx.user.security_level < SecurityLevel.USER:
        return ctx

    embedder = providers.get_embedder()
    if embedder is None or not embedder.is_ready:
        return ctx

    if not await _ensure_index():
        return ctx

    try:
        query   = _normalize_query(ctx.raw_message, ctx.user.area)
        vecs    = await embedder.embed_async([query])
        msg_vec = vecs[0]
    except Exception as e:
        log.error("entity_enricher_embed_msg_failed", error=str(e))
        return ctx

    scores  = np.dot(_vectors, msg_vec)
    top_idx = np.argsort(scores)[::-1]
    seen    = set()
    matches = []

    for idx in top_idx:
        entry = _entries[idx]
        score = float(scores[idx])

        if score < _MIN_SCORE:
            break

        key = entry.get("area_id") or entry.get("entity_id", "")
        if key in seen:
            continue
        seen.add(key)

        matches.append({**entry, "score": score})
        if len(matches) >= _TOP_N:
            break

    if not matches:
        log.debug("entity_enricher_no_match", user_id=ctx.user.user_id)
        return ctx

    ctx.metadata["resolved_entities"] = matches

    top = matches[0]
    log.info("entity_enricher_matched",
             top=top.get("area_name") or top.get("name"),
             score=f"{matches[0]['score']:.2f}",
             user_id=ctx.user.user_id)

    return ctx
