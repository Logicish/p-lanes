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

    # --- processor output ---
    response_text: str   = ""
    total_tokens:  int   = 0
    truncated:     bool  = False
    elapsed:       float = 0.0

    # --- responder output ---
    # responders can modify response_text or add metadata

    # --- finalizer output ---
    final_output: str = ""     # what actually gets sent to the channel

    # --- flags ---
    aborted:        bool = False
    abort_reason:   str  = ""
    skip_processor: bool = False

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
