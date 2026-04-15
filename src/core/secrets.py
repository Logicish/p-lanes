# core/secrets.py
#
# Author:  Logicish
# Company: Logic-Ish Designs
# Date:    4/2/2026
#
# ==================================================
# Secrets-aware YAML loader.
# Supports the !secret tag (ESPHome-style):
#
#   token: !secret ha_token
#
# Values are resolved from secrets.yaml which lives
# next to config.yaml in src/. That file is gitignored
# and never committed.
#
# Usage:
#   from core.secrets import load_yaml
#   cfg = load_yaml(Path(__file__).parent / "config.yaml")
#
# Knows about: nothing — this is a leaf dependency.
# ==================================================

from pathlib import Path

import yaml

_SECRETS_PATH = Path(__file__).parent.parent / "secrets.yaml"
_secrets: dict | None = None


def _load_secrets() -> dict:
    global _secrets
    if _secrets is not None:
        return _secrets
    if not _SECRETS_PATH.exists():
        _secrets = {}
        return _secrets
    with open(_SECRETS_PATH) as f:
        _secrets = yaml.safe_load(f) or {}
    return _secrets


def _secret_constructor(loader: yaml.Loader, node: yaml.Node) -> str:
    key = loader.construct_scalar(node)
    secrets = _load_secrets()
    if key not in secrets:
        raise KeyError(
            f"!secret '{key}' not found in {_SECRETS_PATH}. "
            f"Add it or check secrets.yaml.example."
        )
    return secrets[key]


def _make_loader() -> type:
    """Return a fresh Loader subclass with the !secret constructor registered.
    A subclass is used so we don't pollute the global SafeLoader."""
    loader_cls = type("SecretsLoader", (yaml.SafeLoader,), {})
    loader_cls.add_constructor("!secret", _secret_constructor)
    return loader_cls


def get_secret(key: str, default: str | None = None) -> str | None:
    """Return a single secret value by key, or default if not found."""
    return _load_secrets().get(key, default)


def load_yaml(path: Path) -> dict:
    """Load a YAML file with !secret tag support.
    Raises FileNotFoundError if path does not exist."""
    with open(path) as f:
        return yaml.load(f, Loader=_make_loader()) or {}  # noqa: S506
