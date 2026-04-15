# modules/weather_query.py
#
# Author:  Logicish
# Company: Logic-Ish Designs
# Date:    4/2/2026
#
# ==================================================
# Outside weather query module.
# Intercepts the 'outside_weather' intent and answers
# weather questions using the HA weather entity.
#
# Read-only — never calls a service.
# Sets skip_processor = True — stays out of the
# user's conversation slot.
#
# TODO: weather entity is currently hardcoded to
# 'weather.forecast_home'. Make this configurable
# in providers/homeassistant/config.yaml when
# multiple weather sources need selection.
#
# Security: GUEST (0) — all users can ask about weather.
# No module_permissions entry needed (default is USER=1).
# Set explicitly to 0 in config.yaml if guest access wanted.
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

_WEATHER_SYSTEM = """\
You are a weather assistant. Answer the user's weather question using ONLY the data provided.
Be conversational and brief — one or two sentences. Include relevant details like temperature,
conditions, and precipitation if asked. Do not make up forecasts not in the data.\
"""


def _format_weather(state: dict) -> str:
    """Format a HA weather entity state into a readable summary."""
    attrs    = state.get("attributes", {})
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
            day       = f.get("datetime", "?")[:10]
            cond      = f.get("condition", "?")
            high      = f.get("temperature", "?")
            low       = f.get("templow", None)
            precip    = f.get("precipitation_probability", None)
            line      = f"  {day}: {cond}, high {high}{temp_unit}"
            if low is not None:
                line += f" / low {low}{temp_unit}"
            if precip is not None:
                line += f", {precip}% chance of rain"
            lines.append(line)

    return "\n".join(lines)


@register("weather_query", "classifier")
async def handle(ctx: PipelineContext) -> PipelineContext:
    if ctx.intent != "outside_weather":
        return ctx

    ha = providers.get_provider("homeassistant")
    if ha is None or not ha.is_ready:
        log.warning("ha_provider_unavailable_weather", user_id=ctx.user.user_id)
        return ctx

    ctx.skip_processor = True

    states = await ha.get_states(domains=["weather"])
    if not states:
        ctx.response_text = "I can't reach the weather service right now."
        return ctx

    # prefer the configured entity, fall back to first available
    weather_entity = ha.weather_entity
    weather_state = next(
        (s for s in states if s["entity_id"] == weather_entity),
        states[0] if states else None
    )

    if not weather_state:
        ctx.response_text = "No weather data available."
        return ctx

    summary      = _format_weather(weather_state)
    user_content = f"Weather data:\n{summary}\n\nUser question: {ctx.raw_message}"

    payload = {
        "model":       "local",
        "messages":    [
            {"role": "system", "content": _WEATHER_SYSTEM},
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
                    log.warning("weather_llm_bad_status", status=resp.status)
                    ctx.response_text = "Couldn't get a weather response."
                    return ctx
                data = await resp.json()
                ctx.response_text = data["choices"][0]["message"]["content"].strip()
    except Exception as e:
        log.error("weather_llm_failed", error=str(e))
        ctx.response_text = "Something went wrong fetching the weather."

    log.info("weather_query_answered", user_id=ctx.user.user_id)
    return ctx
