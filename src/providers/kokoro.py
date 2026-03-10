# providers/kokoro.py
#
# Author:  Logicish
# Company: Logic-Ish Designs
# Date:    3/9/2026
#
# ==================================================
# TTS provider — wraps the voice service (CT 104).
# Sends text to /synthesize, returns WAV bytes.
# No retry on failure — caller gets empty bytes and
# falls back to text-only response.
#
# Knows about: config (TTS_*), providers.base.
# ==================================================

# ==================================================
# Imports
# ==================================================
import time

import aiohttp
import structlog

import config
from providers.base import TTSProvider

log = structlog.get_logger()

_HEALTH_TTL = 10.0  # seconds between health rechecks


# ==================================================
# KokoroProvider
# ==================================================

class KokoroProvider(TTSProvider):

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
        return "kokoro"

    @property
    def is_ready(self) -> bool:
        return self._ready

    # --------------------------------------------------
    # Lifecycle
    # --------------------------------------------------

    async def start(self) -> bool:
        self._session = aiohttp.ClientSession(
            timeout=aiohttp.ClientTimeout(total=config.TTS_TIMEOUT)
        )
        self._ready = await self._check_health()
        if self._ready:
            log.info("kokoro_ready", url=config.TTS_URL)
        else:
            log.warning("kokoro_not_ready", url=config.TTS_URL)
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
                f"{config.TTS_URL}/health"
            ) as resp:
                data = await resp.json()
                ok = resp.status == 200 and data.get("ready", False)
        except Exception as e:
            log.warning("kokoro_health_check_failed", error=str(e))
            ok = False
        self._health_cache = ok
        self._health_ts    = now
        return ok

    # --------------------------------------------------
    # Synthesis
    # --------------------------------------------------

    async def synthesize(
        self,
        text:  str,
        voice: str | None = None,
        lang:  str | None = None,
        speed: float = 1.0,
    ) -> bytes:
        """Send text to voice /synthesize. Returns WAV bytes,
        or b"" if the service is unavailable. No retry —
        TTS failure is non-fatal; caller returns text only."""
        if self._session is None:
            log.error("kokoro_synthesize_no_session")
            return b""

        payload: dict = {"text": text, "speed": speed}
        if voice:
            payload["voice"] = voice
        if lang:
            payload["lang"] = lang

        try:
            async with self._session.post(
                f"{config.TTS_URL}/synthesize",
                json=payload,
            ) as resp:
                if resp.status != 200:
                    body = await resp.text()
                    log.warning("kokoro_bad_status",
                                status=resp.status,
                                body=body[:200])
                    return b""

                audio = await resp.read()
                duration = resp.headers.get("X-Audio-Duration", "?")
                proc     = resp.headers.get("X-Processing-Time", "?")
                log.debug("kokoro_synthesized",
                           chars=len(text),
                           audio_duration=duration,
                           processing_time=proc)
                return audio

        except Exception as e:
            log.warning("kokoro_synthesize_error", error=str(e))
            return b""
