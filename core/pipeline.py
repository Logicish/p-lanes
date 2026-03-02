# core/pipeline.py
#
# Author:  Logicish
# Company: Logic-Ish Designs
# Date:    2/26/2026
#
# ==================================================
# Pipeline context object. Flows through every phase:
#   classifier → enricher → processor → responder → finalizer
#
# Each phase reads and mutates the context. This is
# the single object that carries all state through
# the pipeline for one request.
#
# Knows about: slots (User type hint only).
# ==================================================

# ==================================================
# Imports
# ==================================================
from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from core.slots import User


# ==================================================
# Pipeline Context
# ==================================================

@dataclass
class PipelineContext:
    # --- input (set by transporter) ---
    user:            "User"
    raw_message:     str
    input_type:      str             = "text"      # text, voice, image
    conversation_id: str | None      = None
    extra:           dict            = field(default_factory=dict)

    # --- classifier output ---
    intent:          str             = ""           # e.g. "question", "command", "chat"
    tags:            list[str]       = field(default_factory=list)  # e.g. ["rag", "web_search"]
    requires_llm:    bool            = True         # classifier can skip LLM for simple commands

    # --- enricher output ---
    enrichments:     list[dict]      = field(default_factory=list)
    # each enrichment: {"source": "rag", "content": "..."}
    # enrichers append here — processor builds prompt from these

    # --- processor output ---
    response_text:   str             = ""
    total_tokens:    int             = 0
    truncated:       bool            = False
    elapsed:         float           = 0.0

    # --- responder output ---
    # responders can modify response_text or add metadata

    # --- finalizer output ---
    final_output:    str             = ""           # what actually gets sent to the channel

    # --- flags ---
    aborted:         bool            = False        # if True, pipeline stops early
    abort_reason:    str             = ""
    skip_processor:  bool            = False        # classifier can set this to skip LLM

    def build_enriched_prompt(self) -> str:
        # combine raw message with any enrichments for the LLM
        if not self.enrichments:
            return self.raw_message

        parts = []
        for e in self.enrichments:
            source = e.get("source", "unknown")
            content = e.get("content", "")
            parts.append(f"[Context from {source}]:\n{content}")

        context_block = "\n\n".join(parts)
        return (
            f"{context_block}\n\n"
            f"[User message]:\n{self.raw_message}"
        )