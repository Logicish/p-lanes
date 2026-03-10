# core/gates.py
#
# Author:  Logicish
# Company: Logic-Ish Designs
# Date:    3/6/2026
#
# ==================================================
# Shared summarization gate state.
# Breaks the circular dependency between llm.py and
# summarizer.py — both import from here instead of
# from each other.
#
# Knows about: nothing — this is a leaf dependency.
# ==================================================

# ==================================================
# Imports
# ==================================================
import structlog

log = structlog.get_logger()

# ==================================================
# Summarization Gate
# ==================================================
# Tracks which users currently have an async
# summarization in flight. Prevents duplicate runs.
# Gate is released when _update_flags() in llm.py
# proves the slot rebuilt with trimmed history.
# ==================================================

_summarizing_users: set[str] = set()


def is_summarizing(user_id: str) -> bool:
    return user_id in _summarizing_users


def set_summarizing(user_id: str):
    _summarizing_users.add(user_id)
    log.info("summarize_gate_set", user_id=user_id)


def release_summarize_gate(user_id: str):
    """Release the gate — called by llm._update_flags()
    when token count drops below warn threshold, proving
    the slot went cold and rebuilt with trimmed history."""
    if user_id in _summarizing_users:
        _summarizing_users.discard(user_id)
        log.info("summarize_gate_released", user_id=user_id)
