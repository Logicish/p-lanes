# providers/whisper.py
#
# Author:  Logicish
# Company: Logic-Ish Designs
# Date:    3/9/2026
#
# ==================================================
# STT provider — wraps the ears service (CT 103).
# Sends audio to /transcribe, returns text.
# Handles PCM→WAV conversion, VAD gate, and retries.
#
# Knows about: config (STT_*), providers.base.
# ==================================================

# ==================================================
# Imports
# ==================================================
import asyncio
import io
import time
import wave

import aiohttp
import structlog

import config
from providers.base import STTProvider

log = structlog.get_logger()

_HEALTH_TTL = 10.0  # seconds between health rechecks


# ==================================================
# WhisperProvider
# ==================================================

class WhisperProvider(STTProvider):

    def __init__(self):
        self._session:      aiohttp.ClientSession | None = None
        self._ready:        bool  = False
        self._health_cache: bool  = False
        self._health_ts:    float = 0.0

    # --------------------------------------------------
    # Provider identity / state
    # --------------------------------------------------

    @property
    def name(self) -> str:
        return "whisper"

    @property
    def is_ready(self) -> bool:
        return self._ready

    # --------------------------------------------------
    # Lifecycle
    # --------------------------------------------------

    async def start(self) -> bool:
        self._session = aiohttp.ClientSession(
            timeout=aiohttp.ClientTimeout(total=config.STT_TIMEOUT)
        )
        self._ready = await self._check_health()
        if self._ready:
            log.info("whisper_ready", url=config.STT_URL)
        else:
            log.warning("whisper_not_ready", url=config.STT_URL)
        return self._ready

    async def stop(self) -> None:
        self._ready = False
        if self._session:
            await self._session.close()
            self._session = None

    # --------------------------------------------------
    # Health check (cached)
    # --------------------------------------------------

    async def _check_health(self) -> bool:
        now = time.monotonic()
        if now - self._health_ts < _HEALTH_TTL:
            return self._health_cache
        try:
            async with self._session.get(
                f"{config.STT_URL}/health"
            ) as resp:
                data = await resp.json()
                ok = resp.status == 200 and data.get("ready", False)
        except Exception as e:
            log.warning("whisper_health_check_failed", error=str(e))
            ok = False
        self._health_cache = ok
        self._health_ts    = now
        return ok

    # --------------------------------------------------
    # Audio helpers
    # --------------------------------------------------

    @staticmethod
    def _pcm_to_wav(pcm: bytes, sample_rate: int) -> bytes:
        """Wrap raw s16le PCM in a WAV container."""
        buf = io.BytesIO()
        with wave.open(buf, "wb") as wf:
            wf.setnchannels(1)
            wf.setsampwidth(2)       # s16le = 2 bytes per sample
            wf.setframerate(sample_rate)
            wf.writeframes(pcm)
        return buf.getvalue()

    # --------------------------------------------------
    # Transcription
    # --------------------------------------------------

    async def transcribe(self, audio: bytes, sample_rate: int = 16000) -> str:
        """Send audio to ears /transcribe. Returns text, or "" if
        VAD found no speech or the service is unavailable."""
        if self._session is None:
            log.error("whisper_transcribe_no_session")
            return ""

        # convert PCM → WAV if caller sent raw PCM
        if not audio.startswith(b"RIFF"):
            audio = self._pcm_to_wav(audio, sample_rate)

        attempts = config.STT_RETRIES + 1
        for attempt in range(attempts):
            try:
                form = aiohttp.FormData()
                form.add_field(
                    "audio",
                    audio,
                    filename="audio.wav",
                    content_type="audio/wav",
                )
                async with self._session.post(
                    f"{config.STT_URL}/transcribe",
                    data=form,
                ) as resp:
                    if resp.status != 200:
                        body = await resp.text()
                        log.warning("whisper_bad_status",
                                    status=resp.status,
                                    body=body[:200],
                                    attempt=attempt)
                        if attempt < config.STT_RETRIES:
                            continue
                        return ""

                    data = await resp.json()

                    # VAD gate — ears returns vad:false when no speech detected
                    if not data.get("vad", True):
                        return ""

                    return data.get("text", "")

            except Exception as e:
                log.warning("whisper_transcribe_error",
                             error=str(e), attempt=attempt)
                if attempt < config.STT_RETRIES:
                    await asyncio.sleep(0.5)

        return ""
