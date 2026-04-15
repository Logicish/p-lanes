# modules/device_control.py
#
# Author:  Logicish
# Company: Logic-Ish Designs
# Date:    4/3/2026
#
# ==================================================
# Enricher — executes HA device control commands.
# Runs at priority 20, after entity_enricher (10).
#
# Flow:
#   1. Security gate: build allowed domains from
#      DEVICE_DOMAIN_PERMISSIONS for this user's level.
#      Users below level 1 or with no allowed domains
#      are rejected with a friendly message.
#   2. Read ctx.metadata["resolved_entities"] written
#      by entity_enricher. If missing, bail — no blind
#      LLM calls without entity context.
#   3. Call LLM via utility slot (call_internal) with
#      resolved entity context to parse the command into
#      structured JSON. User slot stays clean.
#   4. Validate: domain in allowed set, service in
#      whitelist, entity_id or area_id known.
#   5. Call HAOS. Set ctx.response_text + skip_processor.
#
# Per-domain security is level-based and cumulative —
# see device_domain_permissions in config.yaml.
# No usernames appear here. Adjust a user's security
# level in users.yaml to change what they can control.
#
# Knows about: core/events (register),
#              core/pipeline (PipelineContext),
#              core/llm (call_internal),
#              config (DEVICE_DOMAIN_PERMISSIONS, SecurityLevel),
#              providers (get_provider).
# ==================================================

# ==================================================
# Imports
# ==================================================
import json

import structlog

import providers
from config import DEVICE_DOMAIN_PERMISSIONS, SecurityLevel
from core.events import register
from core.llm import call_internal
from core.pipeline import PipelineContext
from modules.entity_enricher import _normalize_query

log = structlog.get_logger()

# ==================================================
# Service Whitelist
# ==================================================

_ALLOWED: dict[str, set[str]] = {
    "light":         {"turn_on", "turn_off", "toggle"},
    "switch":        {"turn_on", "turn_off", "toggle"},
    "climate":       {"set_temperature", "set_hvac_mode", "turn_on", "turn_off"},
    "lock":          {"lock", "unlock"},
    "cover":         {"open_cover", "close_cover", "stop_cover"},
    "fan":           {"turn_on", "turn_off", "set_percentage"},
    "input_boolean": {"turn_on", "turn_off", "toggle"},
    "media_player":  {
        "media_play", "media_pause", "media_stop",
        "media_next_track", "media_previous_track",
        "volume_up", "volume_down", "volume_set",
        "turn_on", "turn_off",
    },
}

# ==================================================
# Action Labels
# ==================================================

_ACTION_LABELS: dict[str, str] = {
    "turn_on":              "on",
    "turn_off":             "off",
    "toggle":               "toggled",
    "lock":                 "locked",
    "unlock":               "unlocked",
    "open_cover":           "opened",
    "close_cover":          "closed",
    "stop_cover":           "stopped",
    "set_temperature":      "temperature set",
    "set_hvac_mode":        "mode set",
    "set_percentage":       "set",
    "media_play":           "playing",
    "media_pause":          "paused",
    "media_stop":           "stopped",
    "media_next_track":     "next track",
    "media_previous_track": "previous track",
    "volume_up":            "volume up",
    "volume_down":          "volume down",
    "volume_set":           "volume set",
}

# ==================================================
# Extraction Prompt
# ==================================================

_EXTRACT_SYSTEM = """\
You are a Home Assistant command generator.
Given resolved targets and a user command, output a single JSON action.

Entity target:  {"domain": "light", "service": "turn_on", "entity_id": "light.x", "service_data": {}}
Area target:    {"domain": "light", "service": "turn_on", "area_id": "master_bedroom", "service_data": {}}

Rules:
- Use area_id when the user refers to a room or multiple devices in a space
- Use entity_id for a single specific device
- service_data keys: brightness (0-255), color_name, temperature (number), hvac_mode, volume_level (0.0-1.0) — empty dict if none specified
- "my", "mine", "my room" refer to the current user listed above
- Output ONLY valid JSON. No explanation, no markdown, no code blocks.
- If the action cannot be determined: {"error": "unclear"}\
"""


# ==================================================
# Helpers
# ==================================================

def _allowed_domains(security_level: int) -> set[str]:
    """Return the set of domains this security level is permitted to control."""
    return {
        domain
        for level, domains in DEVICE_DOMAIN_PERMISSIONS.items()
        if security_level >= level
        for domain in domains
    }


def _format_resolved(resolved: list[dict], raw_message: str) -> str:
    """Format resolved entity matches into an LLM-readable prompt block."""
    lines = [f"Resolved targets for: {raw_message!r}"]
    for i, m in enumerate(resolved, 1):
        if m["type"] == "area":
            entity_list = ", ".join(
                f"{e['entity_id']} ({e['name']})"
                for e in m.get("area_entities", [])
            )
            lines.append(
                f"{i}. area: {m['area_name']} "
                f"(area_id: {m['area_id']}, score: {m['score']:.2f})\n"
                f"   entities: {entity_list or 'none'}"
            )
        else:
            area_part = f", area: {m['area_name']}" if m.get("area_name") else ""
            lines.append(
                f"{i}. entity: {m['name']} "
                f"(entity_id: {m['entity_id']}, domain: {m['domain']}"
                f"{area_part}, score: {m['score']:.2f})"
            )
    return "\n".join(lines)


# ==================================================
# Validator
# ==================================================

def _validate(
    action:         dict,
    allowed:        set[str],
    states:         list[dict],
    known_area_ids: set[str],
) -> str | None:
    """Return an error string if invalid, None if safe to execute."""

    if "error" in action:
        return action["error"]

    domain  = action.get("domain", "")
    service = action.get("service", "")
    eid     = action.get("entity_id", "")
    area_id = action.get("area_id", "")

    if not domain or not service:
        return "missing domain or service"

    # security: is this domain permitted for this user's level?
    if domain not in allowed:
        return f"domain '{domain}' not permitted at this security level"

    # format: is this service in the whitelist?
    if domain not in _ALLOWED or service not in _ALLOWED[domain]:
        return f"service '{domain}.{service}' not in whitelist"

    if not eid and not area_id:
        return "no entity_id or area_id"

    if eid:
        known_entities = {s["entity_id"] for s in states}
        if eid not in known_entities:
            return f"entity_id '{eid}' not known"
        if eid.split(".")[0] != domain:
            return f"entity_id '{eid}' domain mismatch with '{domain}'"

    if area_id:
        if known_area_ids and area_id not in known_area_ids:
            return f"area_id '{area_id}' not known"

    sd = action.get("service_data") or {}
    if "brightness" in sd:
        b = int(sd["brightness"])
        if not (0 <= b <= 255):
            return f"brightness {b} out of range"
    if "volume_level" in sd:
        v = float(sd["volume_level"])
        if not (0.0 <= v <= 1.0):
            return f"volume_level {v} out of range"

    return None


# ==================================================
# Enricher
# ==================================================

@register("device_control", "enricher")
async def control(ctx: PipelineContext) -> PipelineContext:
    if ctx.intent != "device_control":
        return ctx

    # security gate — build allowed domain set for this user's level
    allowed = _allowed_domains(ctx.user.security_level)
    if not allowed:
        ctx.response_text = "You don't have permission to control devices."
        ctx.skip_processor = True
        log.info("device_control_denied",
                 level=ctx.user.security_level, user_id=ctx.user.user_id)
        return ctx

    ha = providers.get_provider("homeassistant")
    if ha is None or not ha.is_ready:
        ctx.response_text = "I can't reach Home Assistant right now."
        ctx.skip_processor = True
        return ctx

    # require entity context from entity_enricher
    resolved = ctx.metadata.get("resolved_entities")
    if not resolved:
        ctx.response_text = "I couldn't identify which device you mean. Try being more specific."
        ctx.skip_processor = True
        log.info("device_control_no_entities", user_id=ctx.user.user_id)
        return ctx

    ctx.skip_processor = True

    # known area_ids from the resolved matches
    known_area_ids = {
        m["area_id"] for m in resolved
        if m["type"] == "area" and m["area_id"]
    }

    # fetch states for entity validation
    extra  = ["media_player"] if ctx.intent == "media_control" else []
    states = await ha.get_states(domains=list(_DEVICE_DOMAIN_PERMISSIONS_ALL) + extra)

    if not states:
        ctx.response_text = "I can't reach Home Assistant right now."
        return ctx

    # build extraction prompt — normalize possessives for clean LLM parsing
    command        = _normalize_query(ctx.raw_message, ctx.user.area)
    resolver_block = _format_resolved(resolved, command)
    user_content   = (
        f"Current user: {ctx.user.user_id}\n\n"
        f"{resolver_block}\n\n"
        f"Command: {command}"
    )

    messages = [
        {"role": "system", "content": _EXTRACT_SYSTEM},
        {"role": "user",   "content": user_content},
    ]

    try:
        result = await call_internal(
            messages=messages,
            temperature=0.1,
            max_tokens=128,
            fallback_slot=ctx.user.slot,
        )
        action = json.loads(result.content)
    except json.JSONDecodeError as e:
        log.warning("device_control_bad_json", error=str(e), user_id=ctx.user.user_id)
        ctx.response_text = "I couldn't figure out what to control. Try again?"
        return ctx
    except Exception as e:
        log.error("device_control_extract_failed", error=str(e), user_id=ctx.user.user_id)
        ctx.response_text = "Something went wrong. Try again?"
        return ctx

    # validate: security + whitelist + known entity/area
    err = _validate(action, allowed, states, known_area_ids)
    if err:
        log.warning("device_control_validation_failed",
                    reason=err, action=action, user_id=ctx.user.user_id)
        ctx.response_text = "I'm not sure what you want me to control. Can you be more specific?"
        return ctx

    domain       = action["domain"]
    service      = action["service"]
    eid          = action.get("entity_id", "")
    area_id      = action.get("area_id", "")
    service_data = action.get("service_data") or {}

    ok = await ha.call_service(
        domain, service,
        entity_id=eid,
        area_id=area_id,
        service_data=service_data,
    )

    # friendly label
    if area_id:
        label = area_id.replace("_", " ").title()
    else:
        label = eid.replace("_", " ").split(".")[-1]
        for s in states:
            if s["entity_id"] == eid:
                label = s.get("attributes", {}).get("friendly_name", label)
                break

    ctx.response_text = (
        f"Done — {label} {_ACTION_LABELS.get(service, service.replace('_', ' '))}."
        if ok else
        f"Something went wrong trying to control {label}."
    )

    log.info("device_control_command",
             domain=domain, service=service,
             entity_id=eid, area_id=area_id,
             success=ok, user_id=ctx.user.user_id)

    return ctx


# all domains across all permission levels — used for state fetching
_DEVICE_DOMAIN_PERMISSIONS_ALL: set[str] = {
    d for domains in DEVICE_DOMAIN_PERMISSIONS.values() for d in domains
}
