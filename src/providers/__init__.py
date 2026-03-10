# providers/__init__.py
#
# Author:  Logicish
# Company: Logic-Ish Designs
# Date:    3/6/2026
#
# ==================================================
# Provider registry and lifecycle management.
# Transport-layer providers (STT, TTS) register here
# at startup. Transport.py queries the registry to
# discover available capabilities.
#
# Knows about: providers/base (type hints only).
# ==================================================

# ==================================================
# Imports
# ==================================================
from __future__ import annotations

from typing import TYPE_CHECKING

import structlog

if TYPE_CHECKING:
    from providers.base import Provider, STTProvider, TTSProvider

log = structlog.get_logger()

# ==================================================
# Registry
# ==================================================

_providers: dict[str, "Provider"] = {}


def register_provider(provider: "Provider") -> None:
    name = provider.name
    if name in _providers:
        log.warning("provider_overwrite", name=name)
    _providers[name] = provider
    log.info("provider_registered", name=name, type=type(provider).__name__)


def get_provider(name: str) -> "Provider | None":
    return _providers.get(name)


def get_stt() -> "STTProvider | None":
    """Return the first registered STT provider, or None."""
    from providers.base import STTProvider
    for p in _providers.values():
        if isinstance(p, STTProvider):
            return p
    return None


def get_tts() -> "TTSProvider | None":
    """Return the first registered TTS provider, or None."""
    from providers.base import TTSProvider
    for p in _providers.values():
        if isinstance(p, TTSProvider):
            return p
    return None


def get_all() -> dict[str, "Provider"]:
    return _providers


# ==================================================
# Lifecycle
# ==================================================

async def start_all() -> None:
    for name, provider in _providers.items():
        try:
            success = await provider.start()
            if success:
                log.info("provider_started", name=name)
            else:
                log.error("provider_start_failed", name=name)
        except Exception as e:
            log.error("provider_start_error", name=name, error=str(e))


async def stop_all() -> None:
    for name, provider in _providers.items():
        try:
            await provider.stop()
            log.info("provider_stopped", name=name)
        except Exception as e:
            log.error("provider_stop_error", name=name, error=str(e))
