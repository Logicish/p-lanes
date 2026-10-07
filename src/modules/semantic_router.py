# modules/semantic_router.py
#
# Author:  Logicish
# Company: Logic-Ish Designs
# Date:    5/15/2026
#
# ==================================================
# Embedding-first intent classifier (v0.7.0).
# Runs in the classifier phase for every message.
#
# Pre-checks before embedding:
#   1. Prefix strip   — removes conversational filler
#      so the embedding scores the actual intent, not
#      "hey brain, can you turn on the lights".
#   2. Emergency      — 911 / ambulance / break-in phrases
#      → intent "emergency", fixed reply in main.
#   3. Think mode     — explicit reasoning trigger
#      keywords bypass embedding (safety net; listed
#      in intents.yaml comments).
#   4. Sun times      — sunset/sunrise → query_home_state.
#
# After scoring, an action intent (one with a tool, or
# "unsupported") must also pass the request-form gate:
# statements about a topic ("my alarm is so annoying")
# go to chat instead of the feature.
#
# Embedding router — all 14 intents scored against
# BGE-M3 bucket centroids. Three decision bands:
#
#   score >= fast_path_threshold (0.82):
#     arg_extraction intent  → ctx.tool_hint + narrowed
#                              ctx.tool_candidates (tool LLM)
#     all other intents      → ctx.fast_path_tool (bypass
#                              tool LLM, execute directly)
#
#   tool_llm_threshold (0.60) <= score < fast_path:
#     general / think_mode   → conv LLM only (no tool)
#     all other intents      → ctx.tool_hint set (tool LLM
#                              with full manifest)
#
#   score < tool_llm_threshold:
#     → intent = "general", no tool routing
#
# Per-intent confidence overrides in intents.yaml can
# raise the effective minimum above tool_llm_threshold
# for intents that bleed into neighbours.
#
# ha_sensor fast-path also populates
# ctx.metadata["ha_domains"] via keyword scan so the
# query_home_state handler can narrow the HA fetch.
#
# Knows about: core/events (register),
#              core/pipeline (PipelineContext),
#              core/tool_registry (tool_for_intent),
#              providers (get_embedder).
# ==================================================

# ==================================================
# Imports
# ==================================================
import re
from pathlib import Path

import numpy as np
import structlog
import yaml

import providers
from config import is_disabled
from core.events import register
from core.pipeline import PipelineContext
import core.tool_registry as tool_registry

log = structlog.get_logger()

_INTENTS_PATH = Path(__file__).parent / "intents.yaml"


# ==================================================
# Prefix strip
# ==================================================
# Sorted longest-first so "hey brain," matches before "hey".
_FILLER_PREFIXES = sorted([
    "hey brain,", "hey brain",
    "hey alter,", "hey alter",
    "hey lyn,",   "hey lyn",
    "hey jarvis,", "hey jarvis",
    "hey sunny,",  "hey sunny",
    "can you please", "could you please",
    "can you",   "could you",
    "would you", "will you",
    "please,",   "please",
    "go ahead and",
    "actually,", "actually",
    "i need you to", "i want you to",
    "i'd like you to",
], key=len, reverse=True)


def _strip_prefix(text: str) -> str:
    """Strip one leading filler phrase so embedding scores the actual intent."""
    for prefix in _FILLER_PREFIXES:
        if text.startswith(prefix):
            return text[len(prefix):].lstrip(" ,")
    return text


# ==================================================
# HA domain keyword hints (fast-path ha_sensor only)
# ==================================================
# When query_home_state is fast-pathed, the tool LLM
# never runs to extract the 'domains' arg. This scan
# narrows the HA fetch so the handler context stays lean.
# Order matters — first match wins, so specific phrases come before
# generic ones ("garage door" must not fall into the "door" → lock rule).
# 2026-10-06 review: garage → locks, laundry → sun times, door-bell → locks.
_HA_DOMAIN_HINTS = [
    (("sunset", "sunrise", "sun set", "sun rise", "sun go down",
      "sun come up", "dawn", "dusk"),                  ["sun"]),
    (("garage", "blind", "shade", "curtain"),          ["cover"]),
    (("camera", "doorbell", "at the door"),            ["camera"]),
    (("vacuum", "roomba"),                             ["vacuum"]),
    (("tv", "television", "speaker", "music"),         ["media_player"]),
    (("window",),                                      ["binary_sensor"]),
    (("laundry", "washer", "washing machine", "dryer",
      "dishwasher"),                                   ["switch", "binary_sensor"]),
    (("light", "lamp", "bulb", "lights"),              ["light"]),
    (("door", "lock"),                                 ["lock"]),
    (("thermostat", "temperature", "heat",
      "ac", "air conditioning"),                       ["climate"]),
    (("fan",),                                         ["fan"]),
    (("switch",),                                      ["switch"]),
    (("sensor", "humidity", "motion",
      "co2", "air quality"),                           ["sensor", "binary_sensor"]),
]

_HA_DOMAIN_DEFAULT = [
    "light", "switch", "climate", "lock",
    "cover", "fan", "sensor", "binary_sensor", "input_boolean",
]


def _match_ha_domains(text: str) -> list[str] | None:
    # None = no keyword hint → handler uses the tool LLM's pick or the default
    for keywords, domains in _HA_DOMAIN_HINTS:
        for kw in keywords:
            if kw in text:
                return domains
    return None


# ==================================================
# Think mode keyword triggers (pre-embedding safety net)
# ==================================================
_THINK_TRIGGERS = (
    "think about this",
    "think through this",
    "think through it",
    "think step by step",
    "think carefully",
    "think hard about",
    "think before you",
    "really think about",
    "reason through this",
    "reason this out",
    "work through this",
    "take your time and think",
    "let's think about",
    "analyze this carefully",
    "break this down",
    "break it down",
    "walk me through",
    "help me think through",
    "help me think about",
)


# ==================================================
# Emergency keyword net (pre-embedding)
# ==================================================
# "call emergency services" scored as web_search and got the scores/news
# off-message (review 2026-10-06). Phrases only — single words like "fire"
# or "stroke" hit slang ("I'm on fire today", "stroke of genius").
_EMERGENCY_PHRASES = (
    "911", "9-1-1", "call an ambulance", "need an ambulance", "get an ambulance",
    "call the police", "call the cops", "call the fire department",
    "emergency services", "this is an emergency", "it's an emergency", "its an emergency",
    "medical emergency",
    "heart attack", "having a stroke", "can't breathe", "cant breathe",
    "i fell and can't get up", "i fell and cant get up", "i've fallen", "i have fallen",
    "someone's breaking in", "someone is breaking in", "somebody's breaking in",
    "break-in", "intruder in", "there's an intruder",
    "there's a fire", "theres a fire", "house is on fire", "kitchen is on fire",
    "something's on fire", "something is on fire",
    "overdos", "choking", "unconscious", "not breathing", "bleeding a lot",
    "bleeding badly", "won't stop bleeding",
)


# ==================================================
# Request-form gate (action intents only)
# ==================================================
# Embeddings score what a message is about, not what it asks for — "my alarm
# is so annoying" hits timer_alarm at 0.79, "my friend called me earlier" hits
# unsupported at 0.80. An action route needs the message to look like a
# request: a statement opener (I / my / this / the …) demotes to chat unless
# a request marker rescues it ("i need a timer…", "it's too dark in here").
# Offline eval 2026-10-07: blind-set false triggers 9 → 3, no extra misses.
_STATEMENT_OPENER = re.compile(
    r"^(i|i'm|im|i've|ive|i'd|id|i'll|my|mine|this|that|these|those|it's|its|"
    r"the|our|we|we're|he|she|they|his|her|their|music|time|work|school|"
    r"nobody|everyone|everybody|someone|somebody)\b")
_REQUEST_MARKER = re.compile(
    r"\b(i need|i want|i'd like|id like|i wanna|can you|could you|would you|will you|"
    r"please|let me know|tell me|show me|remind me|wake me|set|turn|switch|play|"
    r"put|start|stop|lock|unlock|open|close|dim|add|call|text|order|in here)\b")


def _looks_like_request(text: str) -> bool:
    if not _STATEMENT_OPENER.match(text):
        return True
    return bool(_REQUEST_MARKER.search(text))


# Intents with no tool that still end the turn with a fixed reply (main._fixed_reply)
_FIXED_REPLY_INTENTS = {"unsupported"}


# ==================================================
# Embedding state (lazy — computed once on first use)
# ==================================================
_bucket_vectors:         dict[str, np.ndarray] | None = None
_intent_profiles:        dict[str, str]               = {}
_intent_confidence:      dict[str, float]             = {}
_fast_path_threshold:    float                        = 0.82
_tool_llm_threshold:     float                        = 0.60
_arg_extraction_intents: set[str]                     = set()


async def _ensure_buckets() -> bool:
    global _bucket_vectors, _fast_path_threshold, _tool_llm_threshold
    global _arg_extraction_intents

    if _bucket_vectors is not None:
        return True

    embedder = providers.get_embedder()
    if embedder is None or not embedder.is_ready:
        log.warning("semantic_router_no_embedder")
        return False

    try:
        with open(_INTENTS_PATH) as f:
            cfg = yaml.safe_load(f) or {}

        _fast_path_threshold    = cfg.get("fast_path_threshold", 0.82)
        _tool_llm_threshold     = cfg.get("tool_llm_threshold",  0.60)
        _arg_extraction_intents = set(cfg.get("arg_extraction_intents", []))

        _intent_profiles.update(cfg.get("intent_profiles", {}))
        _intent_confidence.update(cfg.get("intent_confidence", {}))

        buckets_raw    = cfg.get("buckets", {})
        bucket_vectors = {}

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
                 buckets        = list(_bucket_vectors.keys()),
                 fast_path_thresh = _fast_path_threshold,
                 tool_llm_thresh  = _tool_llm_threshold,
                 arg_extraction   = list(_arg_extraction_intents))
        return True

    except Exception as e:
        log.error("semantic_router_init_failed", error=str(e))
        return False


# ==================================================
# Classifier
# ==================================================

@register("semantic_router", "classifier")
async def classify(ctx: PipelineContext) -> PipelineContext:
    if ctx.intent:
        return ctx

    msg_lower = ctx.raw_message.lower().strip()

    # --- emergency safety net — fixed 911 reply, no LLM (main._fixed_reply) ---
    for phrase in _EMERGENCY_PHRASES:
        if phrase in msg_lower:
            ctx.intent = "emergency"
            ctx.tags   = ["emergency_keyword"]
            log.warning("emergency_keyword_match",
                        phrase=phrase, user_id=ctx.user.user_id)
            return ctx

    # --- think mode keyword safety net ---
    for trigger in _THINK_TRIGGERS:
        if trigger in msg_lower:
            ctx.intent           = "think_mode"
            ctx.thinking         = True
            ctx.sampling_profile = "think"
            ctx.tags             = ["think_keyword"]
            log.info("think_mode_keyword_match",
                     trigger=trigger, user_id=ctx.user.user_id)
            return ctx

    # --- sun times safety net — "what time is sunset" scored as clock and
    #     the model invented a time; HA has the real one (2026-10-06) ---
    if _match_ha_domains(msg_lower) == ["sun"] and not is_disabled("query_home_state"):
        ctx.intent           = "ha_sensor"
        ctx.fast_path_tool   = "query_home_state"
        ctx.metadata["ha_domains"] = ["sun"]
        ctx.sampling_profile = "relay"
        ctx.tags             = ["sun_keyword"]
        log.info("sun_keyword_match", user_id=ctx.user.user_id)
        return ctx

    # --- embedding router ---
    embedder = providers.get_embedder()
    if embedder is None or not embedder.is_ready:
        log.warning("semantic_router_no_embedder")
        return ctx

    if not await _ensure_buckets():
        return ctx

    # strip prefix for embedding — improves score on command phrases
    query = _strip_prefix(msg_lower) or msg_lower

    try:
        vecs    = await embedder.embed_async([query])
        msg_vec = vecs[0]
    except Exception as e:
        log.error("semantic_router_embed_failed", error=str(e))
        return ctx

    # score all buckets, keep best
    best_intent = ""
    best_score  = 0.0
    for bucket_intent, centroid in _bucket_vectors.items():
        score = float(np.dot(msg_vec, centroid))
        if score > best_score:
            best_score  = score
            best_intent = bucket_intent

    # per-intent minimum can raise the floor above tool_llm_threshold
    effective_floor = _intent_confidence.get(best_intent, _tool_llm_threshold)

    # --- below minimum → general, no tool routing ---
    if best_score < effective_floor:
        ctx.intent = "general"
        ctx.tags   = [f"below_floor:{best_intent}:{best_score:.3f}"]
        log.debug("intent_below_threshold",
                  best=best_intent, score=f"{best_score:.3f}",
                  user_id=ctx.user.user_id)
        return ctx

    tool_name = tool_registry.tool_for_intent(best_intent)

    # --- statement about a feature topic, not a request → chat ---
    if (tool_name is not None or best_intent in _FIXED_REPLY_INTENTS) \
            and not _looks_like_request(msg_lower):
        ctx.intent = "general"
        ctx.tags   = [f"not_request:{best_intent}:{best_score:.3f}"]
        log.info("intent_not_request",
                 best=best_intent, score=f"{best_score:.3f}",
                 user_id=ctx.user.user_id)
        return ctx

    # --- something the house can't do at all (calls, texts, deliveries,
    #     directions) → fixed reply, no LLM ---
    if best_intent in _FIXED_REPLY_INTENTS:
        ctx.intent = best_intent
        ctx.tags   = [f"{best_intent}:{best_intent}:{best_score:.3f}"]
        log.info("intent_fixed_reply",
                 intent=best_intent, score=f"{best_score:.3f}",
                 user_id=ctx.user.user_id)
        return ctx

    # --- disabled feature → general, no tool routing ---
    if is_disabled(tool_name) or is_disabled(best_intent):
        ctx.intent    = "general"
        ctx.tags      = [f"disabled:{best_intent}:{best_score:.3f}"]
        # 2026-10-06 review: replies invented reasons ("no live NTP sync"),
        # asked for "the API key / device ID", or promised to remember it
        ctx.directive = (
            "The feature needed for this request is switched off, so nothing "
            "was done and you have no data for it. In ONE short sentence, in your "
            "persona, tell the user you can't do that yet. Do not guess or hint at "
            "any result. Do not give a reason, do not ask for an ID, key or name, "
            "do not offer a workaround, and do not promise to remember or do it later."
        )
        log.info("intent_disabled_fallback",
                 intent=best_intent, score=f"{best_score:.3f}",
                 user_id=ctx.user.user_id)
        return ctx

    # set intent and temperature
    ctx.intent = best_intent
    ctx.sampling_profile = _intent_profiles.get(best_intent, "chat")
    if best_intent == "think_mode":
        ctx.thinking = True

    # ha_sensor fast-path: populate domain hint for the handler
    if best_intent == "ha_sensor":
        hint = _match_ha_domains(msg_lower)
        if hint:
            ctx.metadata["ha_domains"] = hint

    # --- no tool behind this intent (general, think_mode, chat_play,
    #     clock, knowledge) → conv LLM only, never the tool LLM ---
    if tool_name is None:
        ctx.tags = [f"conv_only:{best_intent}:{best_score:.3f}"]
        log.info("intent_conv_only",
                 intent=best_intent, score=f"{best_score:.3f}",
                 user_id=ctx.user.user_id)
        return ctx

    # --- high confidence band ---
    if best_score >= _fast_path_threshold:
        if best_intent in _arg_extraction_intents:
            # arg extraction needed even at high confidence —
            # narrow manifest to this tool so tool LLM focuses
            ctx.tool_hint       = tool_name
            ctx.tool_candidates = [tool_name] if tool_name else None
            ctx.tags            = [f"fast_arg:{best_intent}:{best_score:.3f}"]
            log.info("intent_fast_arg",
                     intent=best_intent, score=f"{best_score:.3f}",
                     tool=tool_name, user_id=ctx.user.user_id)
        else:
            # fast path — execute directly, bypass tool LLM
            ctx.fast_path_tool = tool_name
            ctx.tags           = [f"fast_path:{best_intent}:{best_score:.3f}"]
            log.info("intent_fast_path",
                     intent=best_intent, score=f"{best_score:.3f}",
                     tool=tool_name, user_id=ctx.user.user_id)

    # --- uncertain band ---
    else:
        # actionable but uncertain — hint tool LLM with best guess,
        # send full manifest so it can select or reject
        ctx.tool_hint = tool_name
        ctx.tags      = [f"tool_llm:{best_intent}:{best_score:.3f}"]
        log.info("intent_tool_llm",
                 intent=best_intent, score=f"{best_score:.3f}",
                 tool_hint=tool_name, user_id=ctx.user.user_id)

    return ctx
