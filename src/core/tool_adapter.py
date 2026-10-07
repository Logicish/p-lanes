# core/tool_adapter.py
#
# Author:  Logicish
# Company: Logic-Ish Designs
# Date:    5/15/2026
#
# ==================================================
# Translates the tool LLM's raw tool_call output into
# a normalised internal ToolCall, and builds the
# message objects needed to inject tool results back
# into a multi-call tool LLM context.
#
# Verified against llama.cpp + Qwen3.5 live output:
#
#   choices[0].finish_reason == "tool_calls"  ← tool call
#   choices[0].finish_reason == "stop"        ← text response
#
#   tool_calls[n] = {
#       "id":   "aiX2m...",           ← at top level
#       "type": "function",
#       "function": {
#           "name":      "control_device",
#           "arguments": '{"entity":"office light","action":"on"}'
#       }
#   }
#
#   NOTE: content is "" (empty string), NOT null, when
#   the model calls a tool. Do not use content-nullness
#   as the discriminator — use finish_reason.
#
# Knows about: nothing. No imports from core/.
# ==================================================

# ==================================================
# Imports
# ==================================================
from __future__ import annotations

import json
from dataclasses import dataclass

import structlog

log = structlog.get_logger()


# ==================================================
# ToolCall result type
# ==================================================

@dataclass
class ToolCall:
    name:    str    # tool name as returned by the LLM
    args:    dict   # parsed arguments dict (may be empty)
    call_id: str    # id needed to match tool result in multi-call context


# ==================================================
# Translation
# ==================================================

def translate(tool_calls: list | None) -> ToolCall | None:
    """
    Parse the first tool call from the LLM tool_calls list.

    Returns a ToolCall, or None if the list is absent/empty/malformed.
    Only the first call is processed — multi-tool responses are not
    supported in v0.7.0 (max 1 tool call per turn).
    """
    if not tool_calls:
        return None

    raw = tool_calls[0]

    func    = raw.get("function", {})
    name    = func.get("name", "").strip()
    call_id = raw.get("id", "")

    if not name:
        log.warning("tool_adapter_missing_name", raw=str(raw)[:200])
        return None

    raw_args = func.get("arguments", "")
    args     = _parse_args(name, raw_args)

    log.debug("tool_adapter_translated",
              tool=name, call_id=call_id[:12], arg_keys=list(args.keys()))

    return ToolCall(name=name, args=args, call_id=call_id)


def _parse_args(tool_name: str, raw: str) -> dict:
    """Parse the arguments JSON string into a dict."""
    if not raw or not raw.strip():
        return {}
    try:
        parsed = json.loads(raw)
        if not isinstance(parsed, dict):
            log.warning("tool_adapter_args_not_dict",
                        tool=tool_name, type=type(parsed).__name__)
            return {}
        return parsed
    except json.JSONDecodeError as e:
        log.warning("tool_adapter_args_invalid_json",
                    tool=tool_name, error=str(e), raw=raw[:200])
        return {}


# ==================================================
# Message builders (for multi-call tool LLM context)
# ==================================================

def build_assistant_message(tool_calls_raw: list, content: str = "") -> dict:
    """
    Build the assistant message that contains the tool call(s).
    Injected into extra_messages before a subsequent tool LLM pass
    so the model sees its prior tool selection in context.
    """
    return {
        "role":       "assistant",
        "content":    content,
        "tool_calls": tool_calls_raw,
    }


def build_tool_result_message(call_id: str, result: str) -> dict:
    """
    Build the tool result message to follow the assistant tool call.
    The role "tool" and matching tool_call_id are required by the
    OpenAI protocol for multi-turn tool use.
    """
    return {
        "role":         "tool",
        "tool_call_id": call_id,
        "content":      result,
    }
