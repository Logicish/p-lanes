# modules/weather_query.py
#
# Author:  Logicish
# Company: Logic-Ish Designs
# Date:    4/2/2026
#
# ==================================================
# Outside weather query module.
# Intercepts the 'outside_weather' intent and injects
# current weather data into ctx.enrichments so the
# main LLM processor answers through the user's persona.
#
# Read-only — never calls a service.
# Does NOT set skip_processor — the user's slot handles
# the LLM call, keeping the exchange in conversation
# history and persona-consistent.
#
# Security: GUEST (0) — all users can ask about weather.
# Set explicitly to 0 in module_permissions in config.yaml.
#
# Knows about: core/events (register),
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


def _format_weather(state: dict) -> str:
    """Format a HA weather entity state into a readable summary."""
    attrs     = state.get("attributes", {})
    condition = state.get("state", "unknown")
    temp      = attrs.get("temperature", "?")
    temp_unit = attrs.get("temperature_unit", "")
    humidity  = attrs.get("humidity", None)
    wind      = attrs.get("wind_speed", None)
    wind_unit = attrs.get("wind_speed_unit", "")
    forecast  = attrs.get("forecast", [])

    lines = [
        f"Current condition: {condition}",
        f"Temperature: {temp}{temp_unit}",
    ]
    if humidity is not None:
        lines.append(f"Humidity: {humidity}%")
    if wind is not None:
        lines.append(f"Wind: {wind} {wind_unit}".strip())

    if forecast:
        upcoming = forecast[:3]
        lines.append("Forecast:")
        for f in upcoming:
            day    = f.get("datetime", "?")[:10]
            cond   = f.get("condition", "?")
            high   = f.get("temperature", "?")
            low    = f.get("templow", None)
            precip = f.get("precipitation_probability", None)
            line   = f"  {day}: {cond}, high {high}{temp_unit}"
            if low is not None:
                line += f" / low {low}{temp_unit}"
            if precip is not None:
                line += f", {precip}% chance of rain"
            lines.append(line)

    return "\n".join(lines)



@tool("get_weather")
async def _execute(args: dict, ctx: PipelineContext) -> str:
    ha = providers.get_provider("homeassistant")
    if ha is None or not ha.is_ready:
        return "Weather data unavailable — home assistant is not connected."

    states = await ha.get_states(domains=["weather"])
    if not states:
        return "No weather data available right now."

    weather_entity = ha.weather_entity
    weather_state = next(
        (s for s in states if s["entity_id"] == weather_entity),
        states[0],
    )
    if not weather_state:
        return "Weather entity not found."

    log.info("get_weather_tool_ok", entity=weather_state["entity_id"])
    return _format_weather(weather_state)
