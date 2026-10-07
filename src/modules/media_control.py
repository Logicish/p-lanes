# modules/media_control.py
#
# Author:  Logicish
# Company: Logic-Ish Designs
# Date:    5/15/2026
#
# ==================================================
# Tool handler for the 'media_control' intent.
# Targets the active media_player entity in HA.
#
# Actions (from tools.yaml):
#   play, pause, stop, resume, skip, previous,
#   volume_up, volume_down, set_volume, mute, unmute
#
# Entity selection: prefers a playing entity, then
# any active entity, then the first media_player found.
#
# 'play' with a query string is acknowledged but
# freeform music search is not yet implemented —
# it requires knowing the user's music service.
#
# Security: GUEST (0) — everyone can control media.
#
# Knows about: core/tool_registry (tool),
#              core/pipeline (PipelineContext),
#              providers (get_provider).
# ==================================================

# ==================================================
# Imports
# ==================================================
import structlog

import providers
from core.pipeline import PipelineContext
from core.tool_registry import tool

log = structlog.get_logger()

# ==================================================
# Action → HA service mapping
# ==================================================

_ACTIONS: dict[str, tuple[str, dict]] = {
    "play":      ("media_play",           {}),
    "resume":    ("media_play",           {}),
    "pause":     ("media_pause",          {}),
    "stop":      ("media_stop",           {}),
    "skip":      ("media_next_track",     {}),
    "next":      ("media_next_track",     {}),
    "previous":  ("media_previous_track", {}),
    "volume_up": ("volume_up",            {}),
    "volume_down": ("volume_down",        {}),
    "mute":      ("volume_mute",          {"is_volume_muted": True}),
    "unmute":    ("volume_mute",          {"is_volume_muted": False}),
}

_ACTION_LABELS: dict[str, str] = {
    "media_play":           "playing",
    "media_pause":          "paused",
    "media_stop":           "stopped",
    "media_next_track":     "skipped to next track",
    "media_previous_track": "went back",
    "volume_up":            "volume up",
    "volume_down":          "volume down",
    "volume_set":           "volume set",
    "volume_mute":          "muted",
}


# ==================================================
# Entity selection
# ==================================================

def _find_media_player(states: list[dict]) -> dict | None:
    """Return best media player — prefer playing, then any active, else first."""
    playing = [s for s in states if s.get("state") == "playing"]
    if playing:
        return playing[0]
    active = [s for s in states if s.get("state") in ("paused", "idle", "on")]
    if active:
        return active[0]
    return states[0] if states else None


# ==================================================
# Tool handler
# ==================================================

@tool("media_control")
async def _execute(args: dict, ctx: PipelineContext) -> str:
    ha = providers.get_provider("homeassistant")
    if ha is None or not ha.is_ready:
        return "I can't reach Home Assistant right now."

    action = (args.get("action") or "").strip().lower()
    value  = args.get("value")
    query  = (args.get("query") or "").strip()

    if not action:
        return "I need to know what media action to take."

    states = await ha.get_states(domains=["media_player"])
    if not states:
        return "No media players found."

    player = _find_media_player(states)
    if not player:
        return "No media player available."

    eid      = player["entity_id"]
    friendly = player.get("attributes", {}).get("friendly_name", eid)

    # set_volume: special case, value is 0-100
    if action == "set_volume":
        if value is None:
            return "I need a volume level (0–100) for set_volume."
        try:
            level = max(0.0, min(1.0, int(value) / 100))
        except (ValueError, TypeError):
            return "Volume must be a number (0–100)."
        ok = await ha.call_service(
            "media_player", "volume_set",
            entity_id=eid,
            service_data={"volume_level": level},
        )
        label = f"{int(value)}%"
        log.info("media_control_tool_ok", entity=eid, service="volume_set", success=ok)
        return f"Volume set to {label} on {friendly}." if ok else f"Couldn't set volume on {friendly}."

    # play with query — freeform search not yet supported
    if action == "play" and query:
        ok = await ha.call_service("media_player", "media_play", entity_id=eid)
        log.info("media_control_tool_ok", entity=eid, service="media_play", success=ok)
        note = f" (freeform play for '{query}' isn't supported yet — resuming what's queued)"
        return (f"Resuming {friendly}." if ok else f"Couldn't start {friendly}.") + note

    if action not in _ACTIONS:
        return f"I don't know how to '{action}' media."

    service, service_data = _ACTIONS[action]
    ok = await ha.call_service(
        "media_player", service,
        entity_id=eid,
        service_data=service_data if service_data else None,
    )

    label = _ACTION_LABELS.get(service, service.replace("_", " "))
    log.info("media_control_tool_ok", entity=eid, service=service, success=ok)
    return f"{friendly.title()} {label}." if ok else f"Couldn't control {friendly}."
