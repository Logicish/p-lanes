# modules/list_commands.py
#
# Author:  Logicish
# Company: Logic-Ish Designs
# Date:    4/19/2026
#
# ==================================================
# Handles the 'list_commands' intent.
# Queries the commands table filtered to the requesting
# user's security level, groups by category, and returns
# a formatted plain-text list directly as the response.
#
# Sets skip_processor — no LLM needed; this is a
# deterministic lookup and the user wants the raw list.
#
# Security: GUEST (0) — everyone can ask what they can do.
#
# Knows about: core/events (register),
#              core/pipeline (PipelineContext),
#              providers (get_db).
# ==================================================

# ==================================================
# Imports
# ==================================================
import structlog

import providers
from core.pipeline import PipelineContext
from core.tool_registry import tool

log = structlog.get_logger()

_CATEGORY_ORDER = ["search", "weather", "home", "media", "timers", "memory", "system"]

_CATEGORY_LABELS = {
    "search":  "Search & Knowledge",
    "weather": "Weather",
    "home":    "Smart Home",
    "media":   "Media",
    "timers":  "Timers & Alarms",
    "memory":  "Notes & Memory",
    "system":  "System",
}

_SECURITY_LABELS = {0: "Guest", 1: "User", 2: "Trusted", 3: "Trusted", 4: "Admin"}



@tool("list_commands")
async def _execute(args: dict, ctx: PipelineContext) -> str:
    db = providers.get_db("system")
    if db is None or not db.is_ready:
        return "Command list unavailable — database not ready."

    rows = await db.fetchall(
        "SELECT name, syntax, description, category, min_security "
        "FROM commands WHERE min_security <= ? ORDER BY min_security, category, name",
        (ctx.user.security_level,),
    )

    if not rows:
        return "No commands found for your access level."

    by_category: dict[str, list[dict]] = {}
    for row in rows:
        cat = row["category"]
        by_category.setdefault(cat, []).append(row)

    level_label = _SECURITY_LABELS.get(ctx.user.security_level, str(ctx.user.security_level))
    lines = [f"Commands available to you (access level: {level_label}):\n"]

    for cat in _CATEGORY_ORDER:
        if cat not in by_category:
            continue
        lines.append(f"[ {_CATEGORY_LABELS.get(cat, cat.title())} ]")
        for cmd in by_category[cat]:
            req = cmd["min_security"]
            tag = f"  [lvl {req}]" if req > 0 else ""
            lines.append(f"  {cmd['name']}{tag}")
            lines.append(f"    Say: {cmd['syntax']}")
            lines.append(f"    {cmd['description']}")
        lines.append("")

    log.info("list_commands_tool_ok", count=len(rows))
    return "\n".join(lines).rstrip()
