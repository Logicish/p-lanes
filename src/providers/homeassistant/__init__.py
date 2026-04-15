# providers/homeassistant/__init__.py
#
# Author:  Logicish
# Company: Logic-Ish Designs
# Date:    4/2/2026
#
# ==================================================
# Home Assistant provider entry point.
# Exposes register() — the standardized hook that
# autodiscover() calls. Loads its own config, checks
# enabled flag, and registers with core if active.
#
# Knows about: providers (registry only), providers.base,
#              providers.homeassistant.provider.
# ==================================================

from pathlib import Path

import structlog

from core.secrets import load_yaml

log = structlog.get_logger()

_CONFIG_PATH = Path(__file__).parent / "config.yaml"


def register() -> None:
    """Load config, check enabled flag, register with core."""
    import providers
    from providers.homeassistant.provider import HomeAssistantProvider

    if not _CONFIG_PATH.exists():
        log.error("ha_config_missing", path=str(_CONFIG_PATH))
        return

    cfg = load_yaml(_CONFIG_PATH)

    if not cfg.get("enabled", False):
        log.info("ha_disabled")
        return

    providers.register_provider(HomeAssistantProvider(cfg))
