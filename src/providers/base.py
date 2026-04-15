# providers/base.py
#
# Author:  Logicish
# Company: Logic-Ish Designs
# Date:    3/13/2026
#
# ==================================================
# Abstract base classes and result types for providers.
# Providers run at the transport layer, NOT as pipeline
# modules. STT runs before the pipeline (audio → text),
# TTS runs after (text → audio).
#
# Implementations: providers/whisper/, providers/kokoro/
#
# Knows about: nothing — this is a leaf dependency.
# ==================================================

# ==================================================
# Imports
# ==================================================
from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import AsyncIterator


# ==================================================
# STT Result
# ==================================================

@dataclass
class TranscribeResult:
    """Structured result from an STT provider.
    Carries the transcribed text plus metadata for
    envelope population (language, confidence, VAD).
    """
    text:           str
    vad:            bool        = True   # False = no speech detected, text will be ""
    language:       str | None  = None   # detected language code, e.g. "en"
    stt_confidence: float | None = None  # language probability 0.0-1.0


# ==================================================
# Base Provider
# ==================================================

class Provider(ABC):
    """Base lifecycle contract for all providers."""

    @property
    @abstractmethod
    def name(self) -> str:
        """Unique provider name (e.g. 'whisper', 'kokoro')."""
        ...

    @abstractmethod
    async def start(self) -> bool:
        """Initialize the provider. Load models, allocate
        resources. Return True if ready, False on failure."""
        ...

    @abstractmethod
    async def stop(self) -> None:
        """Shutdown and release all resources."""
        ...

    @property
    @abstractmethod
    def is_ready(self) -> bool:
        """Whether the provider is initialized and ready
        to handle requests."""
        ...


# ==================================================
# STT Provider
# ==================================================

class STTProvider(Provider):
    """Speech-to-text provider interface.
    Receives audio bytes, returns a TranscribeResult.
    Called by transport before the pipeline."""

    @abstractmethod
    async def transcribe(
        self,
        audio: bytes,
        sample_rate: int = 16000,
    ) -> TranscribeResult:
        """Transcribe audio to text.

        Args:
            audio:       Raw audio bytes (PCM s16le or WAV)
            sample_rate: Sample rate of the input audio

        Returns:
            TranscribeResult with text, VAD flag, language,
            and STT confidence. text is "" when vad is False.
        """
        ...


# ==================================================
# TTS Provider
# ==================================================

class TTSProvider(Provider):
    """Text-to-speech provider interface.
    Receives text, returns synthesized audio.
    Called by transport/responder after the pipeline."""

    @abstractmethod
    async def synthesize(self, text: str) -> bytes:
        """Synthesize text to audio.

        Args:
            text: Complete text to synthesize.

        Returns:
            Audio bytes (format determined by implementation).
        """
        ...

    async def synthesize_stream(self, text: str) -> AsyncIterator[bytes]:  # type: ignore[override]
        """Streaming synthesis — yields audio chunks as
        sentences are processed. Default implementation
        falls back to non-streaming synthesize().

        Override this for sentence-buffered streaming TTS.

        Args:
            text: Complete text to synthesize.

        Yields:
            Audio byte chunks.
        """
        yield await self.synthesize(text)


# ==================================================
# Embedding Provider
# ==================================================

class EmbeddingProvider(Provider):
    """Text embedding provider interface.
    Encodes text into dense vectors for semantic
    similarity and intent classification.
    Called by pipeline classifier modules."""

    @abstractmethod
    def embed(self, texts: list[str]) -> list:
        """Embed a list of texts synchronously.

        Args:
            texts: List of strings to encode.

        Returns:
            List of embedding vectors (numpy arrays).
        """
        ...

    @abstractmethod
    async def embed_async(self, texts: list[str]) -> list:
        """Embed a list of texts asynchronously.
        Runs embed() in a thread executor so the
        event loop is not blocked.

        Args:
            texts: List of strings to encode.

        Returns:
            List of embedding vectors (numpy arrays).
        """
        ...
