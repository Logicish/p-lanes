# core/tool_registry.py
#
# Author:  Logicish
# Company: Logic-Ish Designs
# Date:    5/15/2026
#
# ==================================================
# Tool registry for v0.7.0.
# Loads tool definitions from modules/tools.yaml and
# maintains a map of handler functions registered via
# the @tool decorator.
#
# Three roles:
#
#   1. Definition store  — tool metadata loaded from
#      tools.yaml: description, parameters, security
#      level, directive template, intent alias.
#
#   2. Handler registry  — async callables registered
#      by handler modules via @tool("tool_name").
#      Auto-populated when modules/ is imported.
#
#   3. Runtime API       — get_manifest(), execute(),
#      build_directive(), tool_for_intent() used by
#      main.py and permission_interceptor.
#
# tools.yaml is the authoritative source for tool
# metadata. The @tool decorator registers the handler
# only — if a tool name is not in tools.yaml it will
# never appear in manifests or be fast-path routed,
# but it can still be executed directly by name.
#
# Knows about: nothing at import time. PipelineContext
# is imported lazily (TYPE_CHECKING only) to avoid
# circular imports.
# ==================================================

# ==================================================
# Imports
# ==================================================
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Callable

import yaml
import structlog

from config import is_disabled

if TYPE_CHECKING:
    from core.pipeline import PipelineContext

log = structlog.get_logger()

_TOOLS_PATH = Path(__file__).parent.parent / "modules" / "tools.yaml"


# ==================================================
# Tool definition
# ==================================================

@dataclass
class ToolDef:
    name:         str
    intent:       str        # embedding intent name that maps to this tool
    description:  str
    parameters:   dict       # JSON Schema object
    min_security: int        # minimum SecurityLevel value (0-4)
    directive:    str        # template: "... {result}" injected into conv. LLM


# ==================================================
# Internal state
# ==================================================

_tools:    dict[str, ToolDef]  = {}   # name  → ToolDef
_by_intent: dict[str, str]     = {}   # intent → tool name
_handlers: dict[str, Callable] = {}   # name  → async handler fn


# ==================================================
# Loader
# ==================================================

def _load() -> None:
    global _tools, _by_intent

    if not _TOOLS_PATH.exists():
        log.warning("tool_registry_yaml_missing", path=str(_TOOLS_PATH))
        return

    try:
        raw = yaml.safe_load(_TOOLS_PATH.read_text()) or {}
    except Exception as e:
        log.error("tool_registry_yaml_parse_failed", error=str(e))
        return

    loaded:    dict[str, ToolDef] = {}
    by_intent: dict[str, str]     = {}

    for entry in raw.get("tools", []):
        name = entry.get("name", "").strip()
        if not name:
            log.warning("tool_registry_entry_no_name", entry=entry)
            continue

        intent = entry.get("intent", "").strip()

        params = entry.get("parameters", {})
        if not isinstance(params, dict):
            params = {"type": "object", "properties": {}, "required": []}

        directive = entry.get(
            "directive",
            "Relay this to the user in your persona: {result}",
        ).strip()

        loaded[name] = ToolDef(
            name         = name,
            intent       = intent,
            description  = entry.get("description", "").strip(),
            parameters   = params,
            min_security = int(entry.get("min_security", 1)),
            directive    = directive,
        )

        if intent:
            by_intent[intent] = name

    _tools     = loaded
    _by_intent = by_intent

    log.info(
        "tool_registry_loaded",
        count   = len(_tools),
        tools   = list(_tools.keys()),
    )


# ==================================================
# Registration (called by @tool decorator)
# ==================================================

def register_handler(name: str, func: Callable) -> None:
    _handlers[name] = func
    log.debug("tool_handler_registered", tool=name)


def tool(name: str) -> Callable:
    """
    Decorator that registers an async function as a tool handler.

    Usage in a handler module:
        from core.tool_registry import tool

        @tool("get_weather")
        async def _execute(args: dict, ctx: PipelineContext) -> str:
            ...
    """
    def decorator(func: Callable) -> Callable:
        register_handler(name, func)
        return func
    return decorator


# ==================================================
# Runtime API
# ==================================================

def get_manifest(
    security_level: int,
    candidates: list[str] | None = None,
) -> list[dict]:
    """
    Return an OpenAI-format tools list for the tool LLM call.

    Filters by:
      - min_security <= security_level
      - candidates list (tool names), if provided
      - handler must be registered (no-handler tools are skipped)
    """
    result = []
    for name, td in _tools.items():
        if td.min_security > security_level:
            continue
        if candidates is not None and name not in candidates:
            continue
        if is_disabled(name):
            continue
        if name not in _handlers:
            log.debug("tool_manifest_skip_no_handler", tool=name)
            continue
        result.append({
            "type": "function",
            "function": {
                "name":        td.name,
                "description": td.description,
                "parameters":  td.parameters,
            },
        })
    return result


async def execute(
    name: str,
    args: dict,
    ctx: "PipelineContext",
) -> str:
    """
    Execute a registered tool handler and return its result string.
    Never raises — errors are captured and returned as a plain string
    so the directive injection and LLM narration path always continues.
    """
    if is_disabled(name):
        log.warning("tool_execute_disabled", tool=name)
        return "That feature is turned off right now."

    handler = _handlers.get(name)
    if handler is None:
        log.warning("tool_execute_no_handler", tool=name)
        return f"Tool '{name}' has no handler registered yet."

    try:
        result = await handler(args, ctx)
        log.info("tool_executed", tool=name, result_chars=len(str(result)))
        return str(result)
    except Exception as e:
        log.error("tool_execute_failed", tool=name, error=str(e))
        return f"Something went wrong running '{name}'."


def build_directive(name: str, result: str) -> str:
    """
    Format the directive template for this tool with the actual result.
    Falls back to a generic directive if the tool is not in the registry.
    """
    td = _tools.get(name)
    if td is None:
        return f"Relay this to the user in your persona: {result}"
    if _is_failure(name, result):
        # 2026-10-06 review: on "not found" / "which one?" results the
        # persona still claimed success ("front door locked, 84% battery").
        return _FAILURE_DIRECTIVE.replace("{result}", result)
    return td.directive.replace("{result}", result)


# results that mean nothing was done / nothing was found
_FAILURE_PREFIXES = (
    "i couldn't find", "there are no", "which one did you mean",
    "i can't", "i need", "you don't have permission", "that action isn't",
    "something went wrong", "home assistant is not", "i don't know how",
    "brightness must",
)

_FAILURE_DIRECTIVE = (
    "The tool returned this, and NOTHING was changed or found: {result}\n"
    "Tell the user exactly that in one or two short sentences, in your persona. "
    "If it asks which device, ask the same question. Do not say anything was "
    "turned on, off, set, locked, started or stopped. Do not describe a state, "
    "temperature, battery level or picture. Do not suggest fixes, device IDs, "
    "API keys or troubleshooting steps."
)


def is_failure(name: str, result: str) -> bool:
    """True when a tool result means nothing was done / nothing was found."""
    return _is_failure(name, result)


def _is_failure(name: str, result: str) -> bool:
    r = (result or "").strip().lower()
    if name == "control_device":
        return not r.startswith("done")
    return r.startswith(_FAILURE_PREFIXES) or " is unavailable" in r or " is unknown" in r


def tool_for_intent(intent: str) -> str | None:
    """
    Return the tool name that corresponds to an embedding intent name.
    Used by the semantic router to resolve fast_path_tool from an intent.
    Returns None if no tool maps to this intent.
    """
    return _by_intent.get(intent)


def get_tool(name: str) -> ToolDef | None:
    return _tools.get(name)


def is_registered(name: str) -> bool:
    """True if the tool has both a definition and a registered handler, and isn't disabled."""
    return name in _tools and name in _handlers and not is_disabled(name)


def handler_count() -> int:
    return len(_handlers)


def tool_count() -> int:
    return len(_tools)


# ==================================================
# Load at import time
# ==================================================

_load()
