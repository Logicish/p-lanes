# providers/kokoro/__init__.py
#
# Author:  Logicish
# Company: Logic-Ish Designs
# Date:    3/13/2026
#
# ==================================================
# Kokoro TTS provider entry point.
# Exposes register() — the standardized hook that
# autodiscover() calls. Loads its own config, checks
# enabled flag, and registers with core if active.
#
# Knows about: providers (registry only), providers.base,
#              providers.kokoro.provider.
# ==================================================

from pathlib import Path

import structlog
import yaml

log = structlog.get_logger()

_CONFIG_PATH = Path(__file__).parent / "config.yaml"


def register() -> None:
    """Load config, check enabled flag, register with core."""
    import providers
    from providers.kokoro.provider import KokoroProvider

    if not _CONFIG_PATH.exists():
        log.error("kokoro_config_missing", path=str(_CONFIG_PATH))
        return

    with open(_CONFIG_PATH) as f:
        cfg = yaml.safe_load(f) or {}

    if not cfg.get("enabled", False):
        log.info("kokoro_disabled")
        return

    providers.register_provider(KokoroProvider(cfg))
