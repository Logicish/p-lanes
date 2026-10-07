# core/pipeline.py
#
# Author:  Logicish
# Company: Logic-Ish Designs
# Date:    3/13/2026
#
# ==================================================
# Pipeline context object. Initialized from a
# MessageEnvelope and flows through every phase:
#   classifier → enricher → processor → responder → finalizer
#
# The envelope is stored directly on the context and
# accessible to all pipeline modules throughout.
# Convenience properties delegate to the envelope
# so modules don't need to nest into ctx.envelope.
#
# Knows about: slots (User), envelope (MessageEnvelope).
# ==================================================

# ==================================================
# Imports
# ==================================================
from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from core.slots import User
    from core.envelope import MessageEnvelope, Source, Attachment


# ==================================================
# Pipeline Context
# ==================================================

@dataclass
class PipelineContext:
    # --- input (set at construction) ---
    user:     "User"
    envelope: "MessageEnvelope"

    # --- classifier output ---
    intent:       str       = ""
    tags:         list[str] = field(default_factory=list)
    requires_llm: bool      = True

    # --- enricher output ---
    enrichments: list[dict] = field(default_factory=list)
    # each enrichment: {"source": "rag", "content": "..."}

    # --- inter-module structured data ---
    metadata: dict = field(default_factory=dict)
    # keyed by module name convention, e.g. metadata["resolved_entities"]

    # --- processor output ---
    response_text: str   = ""
    total_tokens:  int   = 0
    truncated:     bool  = False
    elapsed:       float = 0.0

    # --- responder output ---
    # responders can modify response_text or add metadata

    # --- finalizer output ---
    final_output: str = ""     # what actually gets sent to the channel

    # --- tool pipeline (set by semantic router / main loop) ---
    fast_path_tool:   str | None       = None   # tool name if fast-path resolved
    tool_hint:        str | None       = None   # tool name suggested by router (tool LLM path)
    tool_candidates:  list[str] | None = None   # narrowed tool list for get_manifest()
    tool_call_count:  int              = 0      # number of tool LLM round-trips this turn
    directive:        str | None       = None   # directive injected into conv LLM extra_messages

    # --- tool record (set by main.py, read by core/turn_log) ---
    tool_name:   str | None  = None
    tool_args:   dict | None = None
    tool_result: str | None  = None

    # --- flags ---
    aborted:      bool = False
    abort_reason: str  = ""

    # --- sampling overrides (set by classifier, applied by processor) ---
    temperature_override: float | None = None
    thinking:             bool         = False
    # name of a config sampling_profiles entry; router sets it from
    # intent_profiles, main._pick_profile may override (relay / feature_off)
    sampling_profile:     str | None   = None

    # --- envelope convenience accessors ---

    @property
    def raw_message(self) -> str:
        return self.envelope.text or ""

    @property
    def source(self) -> "Source":
        return self.envelope.source

    @property
    def conversation_id(self) -> str | None:
        return self.envelope.conversation_id

    @property
    def device_id(self) -> str | None:
        return self.envelope.device_id

    @property
    def language(self) -> str | None:
        return self.envelope.language

    @property
    def stt_confidence(self) -> float | None:
        return self.envelope.stt_confidence

    @property
    def voice_confidence(self) -> float | None:
        return self.envelope.voice_confidence

    @property
    def attachments(self) -> "list[Attachment] | None":
        return self.envelope.attachments

    # --- prompt builder ---

    def build_enriched_prompt(self) -> str:
        if not self.enrichments:
            return self.raw_message

        parts = []
        for e in self.enrichments:
            src     = e.get("source", "unknown")
            content = e.get("content", "")
            parts.append(f"[Context from {src}]:\n{content}")

        context_block = "\n\n".join(parts)
        return (
            f"{context_block}\n\n"
            f"[User message]:\n{self.raw_message}"
        )
