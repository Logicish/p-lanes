# modules/ha_query.py
#
# Author:  Logicish
# Company: Logic-Ish Designs
# Date:    4/2/2026
#
# ==================================================
# Home Assistant read-only state enricher.
# Intercepts the 'ha_sensor' intent and injects
# relevant entity states into ctx.enrichments so
# the main LLM processor answers through the user's
# persona.
#
# The semantic router's tier 2 match populates
# ctx.metadata["ha_domains"] with a narrowed domain
# list. If absent, falls back to all controllable
# domains. Keeping the snapshot lean reduces prompt
# size and improves answer quality.
#
# Does NOT set skip_processor — the persona handles
# the response, keeping the exchange in conversation
# history.
#
# Security: USER (1) via module_permissions.
#
# Knows about: core/events (register),
#              core/pipeline (PipelineContext),
#              providers (get_provider).
# ==================================================

# ==================================================
# Imports
# ==================================================
from datetime import datetime

import structlog

import providers
from config import device_excluded, TIMEZONE
from core.pipeline import PipelineContext
from core.tool_registry import tool

log = structlog.get_logger()

_DEFAULT_DOMAINS = [
    "light", "switch", "climate", "lock",
    "cover", "fan", "sensor", "binary_sensor", "input_boolean",
]


# plain words for "there are no … in the house" so the persona can
# repeat the result verbatim ("lock devices" read as a broken tool)
_DOMAIN_WORDS = {
    "lock": "smart locks", "climate": "thermostats or AC units",
    "cover": "garage doors, blinds or shades", "camera": "cameras",
    "vacuum": "robot vacuums", "media_player": "TVs or speakers",
    "binary_sensor": "door, window or motion sensors", "fan": "fans",
    "switch": "smart plugs or switches", "light": "smart lights",
    "sensor": "sensors",
}

_SUN_LABELS = {
    "sensor.sun_next_rising":  "Next sunrise",
    "sensor.sun_next_setting": "Next sunset",
    "sensor.sun_next_dawn":    "Next dawn",
    "sensor.sun_next_dusk":    "Next dusk",
}


def _local(ts: str) -> str:
    # HA reports sun times in UTC ISO — show the house's local clock
    try:
        dt = datetime.fromisoformat(ts.replace("Z", "+00:00")).astimezone(TIMEZONE)
        return dt.strftime("%A %-I:%M %p")
    except ValueError:
        return ts


async def _sun_times(ha) -> str:
    states = await ha.get_states(domains=["sensor"]) or []
    lines = [f"{_SUN_LABELS[s['entity_id']]}: {_local(s.get('state', ''))}"
             for s in states if s["entity_id"] in _SUN_LABELS]
    return "\n".join(sorted(lines)) or "Sunrise and sunset times are not available."


def _build_state_snapshot(states: list[dict]) -> str:
    lines = []
    for s in states:
        eid   = s["entity_id"]
        name  = s.get("attributes", {}).get("friendly_name", eid)
        state = s.get("state", "unknown")
        unit  = s.get("attributes", {}).get("unit_of_measurement", "")
        lines.append(f"{name}: {state}{(' ' + unit) if unit else ''}")
    return "\n".join(lines)



@tool("query_home_state")
async def _execute(args: dict, ctx: PipelineContext) -> str:
    ha = providers.get_provider("homeassistant")
    if ha is None or not ha.is_ready:
        return "Home assistant is not connected."

    # a router keyword hint ("garage" → cover) is more reliable than the tool
    # LLM's pick ("is the laundry done" → ["sensor"] dumped 115 sensors and the
    # sun times); then the LLM's domains, then everything controllable
    domains = (ctx.metadata.get("ha_domains") or args.get("domains")
               or _DEFAULT_DOMAINS)

    if "sun" in domains:
        return await _sun_times(ha)

    states = [s for s in (await ha.get_states(domains=domains) or []) if not device_excluded(s)]
    if not states:
        words = ", ".join(_DOMAIN_WORDS.get(d, d) for d in domains)
        return f"There are no {words} connected to the house system."

    log.info("query_home_state_tool_ok", domains=domains, entity_count=len(states))
    return _build_state_snapshot(states)
