# modules/ha_query.py
#
# Author:  Logicish
# Company: Logic-Ish Designs
# Date:    4/2/2026
#
# ==================================================
# Home Assistant read-only query module.
# Intercepts the 'ha_sensor' intent and answers
# questions about current device/sensor state
# without making any changes.
#
# Flow:
#   1. Fetch relevant entity states from the HA provider.
#   2. Pass the state snapshot + user question to the LLM.
#   3. LLM answers in natural language.
#   4. Sets skip_processor = True — stays out of
#      the user's conversation slot.
#
# Security: USER (1) via module_permissions — jj and
# above can query, guest cannot.
#
# Knows about: core/events (register),
#              core/pipeline (PipelineContext),
#              providers (get_provider),
#              config (LLM_URL).
# ==================================================

# ==================================================
# Imports
# ==================================================
import aiohttp
import structlog

import providers
from config import LLM_URL
from core.events import register
from core.pipeline import PipelineContext

log = structlog.get_logger()

# ==================================================
# LLM answer prompt
# ==================================================

_QUERY_SYSTEM = """\
You are a home assistant state reader. Answer the user's question using ONLY the entity states provided.
Be brief and natural — one or two sentences. Do not suggest changes or offer to control anything.
If the relevant entity is not in the list, say you don't have that sensor.\
"""


# ==================================================
# Helpers
# ==================================================

def _build_state_snapshot(states: list[dict]) -> str:
    """Build a readable state snapshot for the LLM."""
    lines = []
    for s in states:
        eid        = s["entity_id"]
        name       = s.get("attributes", {}).get("friendly_name", eid)
        state      = s.get("state", "unknown")
        unit       = s.get("attributes", {}).get("unit_of_measurement", "")
        value      = f"{state} {unit}".strip()
        lines.append(f"{name}: {value}")
    return "\n".join(lines)


# ==================================================
# Classifier
# ==================================================

@register("ha_query", "enricher")
async def handle(ctx: PipelineContext) -> PipelineContext:
    if ctx.intent != "ha_sensor":
        return ctx

    ha = providers.get_provider("homeassistant")
    if ha is None or not ha.is_ready:
        log.warning("ha_provider_unavailable", user_id=ctx.user.user_id)
        return ctx

    ctx.skip_processor = True

    # fetch all relevant states — include sensors for queries
    states = await ha.get_states(
        domains=["light", "switch", "climate", "lock", "cover",
                 "fan", "sensor", "binary_sensor", "input_boolean"]
    )
    if not states:
        ctx.response_text = "I can't reach Home Assistant right now."
        return ctx

    snapshot   = _build_state_snapshot(states)
    user_content = f"Current home state:\n{snapshot}\n\nUser question: {ctx.raw_message}"

    payload = {
        "model":       "local",
        "messages":    [
            {"role": "system", "content": _QUERY_SYSTEM},
            {"role": "user",   "content": user_content},
        ],
        "temperature": 0.3,
        "max_tokens":  128,
    }

    try:
        async with aiohttp.ClientSession(
            timeout=aiohttp.ClientTimeout(total=10)
        ) as session:
            async with session.post(LLM_URL, json=payload) as resp:
                if resp.status != 200:
                    log.warning("ha_query_llm_bad_status", status=resp.status)
                    ctx.response_text = "I couldn't get a response from the assistant."
                    return ctx
                data = await resp.json()
                ctx.response_text = data["choices"][0]["message"]["content"].strip()
    except Exception as e:
        log.error("ha_query_llm_failed", error=str(e))
        ctx.response_text = "Something went wrong fetching the home state."

    log.info("ha_query_answered", user_id=ctx.user.user_id)
    return ctx
