# providers/__init__.py
#
# Author:  Logicish
# Company: Logic-Ish Designs
# Date:    3/13/2026
#
# ==================================================
# Provider registry, lifecycle management, and
# autodiscovery. Providers are fully isolated — each
# lives in its own subdirectory with its own config.
#
# autodiscover() scans providers/ for subpackages that
# expose a register() hook. Core never imports a
# specific provider directly.
#
# Knows about: providers/base (type hints only).
# ==================================================

# ==================================================
# Imports
# ==================================================
from __future__ import annotations

import importlib
import pkgutil
from pathlib import Path
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
# Autodiscovery
# ==================================================

def autodiscover() -> None:
    """Scan providers/ subdirectories for register() hooks.
    Each provider subpackage that exposes register() is
    imported and its hook is called. Providers that are
    disabled in their own config.yaml are silently skipped.
    A broken provider logs an error but does not crash startup.
    """
    providers_path = Path(__file__).parent

    for _importer, pkg_name, ispkg in pkgutil.iter_modules([str(providers_path)]):
        if not ispkg:
            continue   # skip flat modules (base.py, etc.)

        full_name = f"providers.{pkg_name}"
        try:
            mod = importlib.import_module(full_name)
            if hasattr(mod, "register"):
                mod.register()
                log.info("provider_discovered", provider=pkg_name)
            else:
                log.debug("provider_no_register_hook", provider=pkg_name)
        except Exception as e:
            log.error("provider_discovery_failed",
                       provider=pkg_name, error=str(e))


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
