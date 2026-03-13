# core/envelope.py
#
# Author:  Logicish
# Company: Logic-Ish Designs
# Date:    3/13/2026
#
# ==================================================
# MessageEnvelope — the normalized input contract.
# All providers normalize their raw input into this
# structure before handing off to core. p-lanes only
# ever sees this envelope regardless of source
# (text, voice, API, Home Assistant, etc.).
#
# Flows through the entire pipeline via PipelineContext.
#
# Knows about: nothing — leaf dependency.
# ==================================================

# ==================================================
# Imports
# ==================================================
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from uuid import uuid4


# ==================================================
# Enums
# ==================================================

class Source(Enum):
    TEXT  = "text"   # plain text channel (web UI, CLI, etc.)
    VOICE = "voice"  # voice/satellite channel
    API   = "api"    # direct API caller
    HA    = "ha"     # Home Assistant integration


class AttachmentType(Enum):
    IMAGE = "image"
    VIDEO = "video"


# ==================================================
# Attachment
# ==================================================

@dataclass
class Attachment:
    type: AttachmentType
    data: bytes
    mime: str          # e.g. "image/jpeg", "video/mp4"


# ==================================================
# MessageEnvelope
# ==================================================

@dataclass
class MessageEnvelope:
    # --- required ---
    user_id:          str | None              # None = unidentified; core maps to guest
    source:           Source
    text:             str | None              # None if purely attachment-based input

    # --- auto-generated ---
    message_id:       str = field(
        default_factory=lambda: str(uuid4())
    )
    timestamp:        datetime = field(
        default_factory=lambda: datetime.now(timezone.utc)
    )

    # --- routing ---
    conversation_id:  str | None             = None
    device_id:        str | None             = None  # return address for output routing

    # --- STT metadata ---
    language:         str | None             = None  # STT-detected language code (e.g. "en")
    stt_confidence:   float | None           = None  # Whisper language probability 0.0-1.0

    # --- voice print metadata ---
    voice_confidence: float | None           = None  # speaker match confidence 0.0-1.0

    # --- multimodal ---
    attachments:      list[Attachment] | None = None  # images, video frames (future)
