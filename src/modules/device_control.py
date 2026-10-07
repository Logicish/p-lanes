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
import re

import structlog

import providers
from config import DEVICE_DOMAIN_PERMISSIONS, device_excluded
from core.pipeline import PipelineContext
from core.tool_registry import tool

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


# all domains across all permission levels — used for state fetching
_DEVICE_DOMAIN_PERMISSIONS_ALL: set[str] = {
    d for domains in DEVICE_DOMAIN_PERMISSIONS.values() for d in domains
}


# ==================================================
# Tool handler helpers
# ==================================================

# tool LLM action values → (ha_service, base_service_data)
_ACTION_TO_SERVICE: dict[str, tuple[str, dict]] = {
    "on":       ("turn_on",  {}),
    "off":      ("turn_off", {}),
    "toggle":   ("toggle",   {}),
    "dim":      ("turn_on",  {"brightness_step": -50}),
    "brighten": ("turn_on",  {"brightness_step": 50}),
    "lock":     ("lock",     {}),
    "unlock":   ("unlock",   {}),
}


# words that don't identify a specific device ("turn off the lights")
_GENERIC_WORDS  = {"the", "my", "all", "a", "light", "lights", "lamp", "lamps", "device", "devices"}
_LIGHT_WORDS    = {"light", "lights", "lamp", "lamps"}
# "bedroom lights" / "my room light" → the speaker's own room
_OWN_ROOM_WORDS = {"bedroom", "room"}
_UNREACHABLE    = {"unavailable", "unknown"}


def _tokens(text: str) -> set[str]:
    text = re.sub(r"'s\b", "", text.lower())
    return set(re.sub(r"[._\-]", " ", text).split())


def _friendly(s: dict) -> str:
    return s.get("attributes", {}).get("friendly_name", s["entity_id"])


def _find_entity(
    ref: str, states: list[dict], allowed: set[str], user_id: str,
) -> tuple[dict | None, list[dict]]:
    """Find the HA entity the tool LLM's entity string refers to.
    Returns (match, candidates) — candidates is non-empty only when a
    generic reference ("the lights") is ambiguous."""
    ref_lower = ref.lower().strip()
    ref_words = _tokens(ref_lower)
    pool      = [s for s in states
                 if s["entity_id"].split(".")[0] in allowed and not device_excluded(s)]

    # pass 1: exact friendly name
    for s in pool:
        if _friendly(s).lower() == ref_lower:
            return s, []

    # passes 2-3 only for refs that name something specific — a bare
    # "light"/"the lights" matched any "... light" here and switched on
    # someone else's lamp ("set the color to warm white", 2026-10-06)
    if ref_words - _GENERIC_WORDS:
        # pass 2: all ref words appear in friendly name
        for s in pool:
            if ref_words.issubset(_tokens(_friendly(s))):
                return s, []

        # pass 3: ref is a substring of entity_id (dots/underscores → spaces)
        for s in pool:
            eid_flat = s["entity_id"].lower().replace(".", " ").replace("_", " ")
            if ref_lower in eid_flat:
                return s, []

    # pass 4: generic reference — resolve to the speaker's own device,
    # or the only reachable one
    specific = ref_words - _GENERIC_WORDS
    if specific and not specific <= _OWN_ROOM_WORDS:
        return None, []

    cands = pool
    if ref_words & _LIGHT_WORDS:
        cands = [s for s in pool if s["entity_id"].startswith("light.")]

    # never fall through to someone else's device just because it's the only
    # one online — anything but a single obvious match becomes a question
    own = [s for s in cands if user_id.lower() in _tokens(_friendly(s))]
    if len(own) == 1:
        return own[0], []

    return None, (own or cands)


# color words HA's color_name doesn't know → color temperature
_WHITE_TEMPS_K: dict[str, int] = {
    "warm white": 2700, "warm": 2700, "soft white": 3000,
    "white": 4000, "neutral white": 4000, "natural white": 4000,
    "cool white": 5000, "cool": 5000, "daylight": 6500,
}


def _map_action(action: str, domain: str, value: str) -> tuple[str, dict, str | None]:
    """Map tool action + optional value to (ha_service, service_data, error)."""
    # "dim the lights to 50 percent" → the tool LLM sends dim + value
    if action in ("dim", "brighten") and value and re.fullmatch(r"\d{1,3}%?", value.strip()):
        action, value = "set_brightness", value.strip().rstrip("%")

    if action in _ACTION_TO_SERVICE:
        service, sdata = _ACTION_TO_SERVICE[action]
        return service, dict(sdata), None

    if action == "set_brightness":
        try:
            pct = int(value)
            if not 0 <= pct <= 100:
                return "", {}, "Brightness must be 0–100."
            return "turn_on", {"brightness": int(pct * 2.55)}, None
        except (ValueError, TypeError):
            return "", {}, "I need a brightness value (0–100)."

    if action == "set_temperature":
        try:
            return "set_temperature", {"temperature": float(value)}, None
        except (ValueError, TypeError):
            return "", {}, "I need a temperature value."

    if action == "set_color":
        if not value:
            return "", {}, "I need a color name."
        color = value.lower().strip()
        if color in _WHITE_TEMPS_K:
            return "turn_on", {"color_temp_kelvin": _WHITE_TEMPS_K[color]}, None
        return "turn_on", {"color_name": color}, None

    return "", {}, f"I don't know how to '{action}' a device."


# ==================================================
# Tool handler
# ==================================================

@tool("control_device")
async def _execute(args: dict, ctx: PipelineContext) -> str:
    ha = providers.get_provider("homeassistant")
    if ha is None or not ha.is_ready:
        return "I can't reach Home Assistant right now."

    allowed = _allowed_domains(ctx.user.security_level)
    if not allowed:
        return "You don't have permission to control devices."

    entity_ref = (args.get("entity") or "").strip()
    action     = (args.get("action") or "").strip().lower()
    value      = (args.get("value") or "").strip()

    if not entity_ref or not action:
        return "I need to know which device and what action to take."

    states = await ha.get_states(domains=list(_DEVICE_DOMAIN_PERMISSIONS_ALL))
    if not states:
        return "I can't get device states right now."

    target, candidates = _find_entity(entity_ref, states, allowed, ctx.user.user_id)
    if not target:
        if candidates:
            names = ", ".join(_friendly(c) for c in candidates)
            return f"Which one did you mean: {names}?"
        return f"I couldn't find a device matching '{entity_ref}'."

    if target.get("state") in _UNREACHABLE:
        return (f"{_friendly(target)} is {target.get('state')} — "
                "Home Assistant can't reach it right now.")

    eid      = target["entity_id"]
    domain   = eid.split(".")[0]
    friendly = target.get("attributes", {}).get("friendly_name", eid)

    if domain not in allowed:
        return f"You don't have permission to control {friendly}."

    service, service_data, err = _map_action(action, domain, value)
    if err:
        return err

    if domain not in _ALLOWED or service not in _ALLOWED[domain]:
        return f"That action isn't supported for {friendly}."

    ok = await ha.call_service(domain, service, entity_id=eid, service_data=service_data)

    label = _ACTION_LABELS.get(service, service.replace("_", " "))
    log.info("control_device_tool_ok",
             entity=eid, service=service, success=ok)
    return f"Done — {friendly} {label}." if ok else f"Something went wrong controlling {friendly}."
