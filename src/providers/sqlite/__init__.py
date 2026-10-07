# providers/sqlite/__init__.py
#
# Author:  Logicish
# Company: Logic-Ish Designs
# Date:    4/15/2026
#
# ==================================================
# SQLite provider entry point.
# Exposes register() — the standardized hook that
# autodiscover() calls. Registers the system DB.
#
# Also exposes ensure_user_db(user_id, db_path) —
# called from slots.init_all_users() to register a
# per-user DB before providers.start_all() fires.
# Lazy-safe: no-ops if the provider is already
# registered (handles restart after file deletion).
#
# Knows about: providers (registry only),
#              providers.sqlite.provider.
# ==================================================

from pathlib import Path

import structlog
import yaml

log = structlog.get_logger()

_CONFIG_PATH = Path(__file__).parent / "config.yaml"


def register() -> None:
    """Register the system and recipes DB providers."""
    import providers
    from providers.sqlite.provider import SqliteProvider, SYSTEM_MIGRATIONS, HOUSE_MIGRATIONS, GAME_MIGRATIONS, RECIPE_MIGRATIONS, FINANCE_MIGRATIONS

    if not _CONFIG_PATH.exists():
        log.error("sqlite_config_missing", path=str(_CONFIG_PATH))
        return

    with open(_CONFIG_PATH) as f:
        cfg = yaml.safe_load(f) or {}

    if not cfg.get("enabled", False):
        log.info("sqlite_disabled")
        return

    system_path  = cfg.get("system_db_path",  "/var/lib/p-lanes/db/system.db")
    house_path   = cfg.get("house_db_path",   "/var/lib/p-lanes/db/house.db")
    games_path   = cfg.get("games_db_path",   "/var/lib/p-lanes/db/games.db")
    recipes_path = cfg.get("recipes_db_path", "/var/lib/p-lanes/db/recipes.db")
    finance_path = cfg.get("finance_db_path", "/var/lib/p-lanes/db/finance.db")

    providers.register_provider(SqliteProvider("system",  system_path,  SYSTEM_MIGRATIONS))
    providers.register_provider(SqliteProvider("house",   house_path,   HOUSE_MIGRATIONS))
    providers.register_provider(SqliteProvider("games",   games_path,   GAME_MIGRATIONS))
    providers.register_provider(SqliteProvider("recipes", recipes_path, RECIPE_MIGRATIONS))
    providers.register_provider(SqliteProvider("finance", finance_path, FINANCE_MIGRATIONS))


def ensure_user_db(user_id: str, db_path: str) -> None:
    """Register a user DB provider if not already registered.

    Called from slots.init_all_users() for each user at startup.
    Safe to call multiple times — no-ops if already registered.
    """
    import providers
    from providers.sqlite.provider import SqliteProvider, USER_MIGRATIONS

    if providers.get_provider(f"sqlite:{user_id}") is not None:
        return

    providers.register_provider(
        SqliteProvider(user_id, db_path, USER_MIGRATIONS)
    )
    log.info("sqlite_user_db_registered", user_id=user_id, db_path=db_path)


def ensure_history_db(user_id: str, db_path: str) -> None:
    """Register a user's turn-log DB provider (handle "history:<uid>").

    Separate file from user.db so the turn log can be size-capped
    and trimmed without touching anything else.
    """
    import providers
    from providers.sqlite.provider import SqliteProvider, HISTORY_MIGRATIONS

    handle = f"history:{user_id}"
    if providers.get_provider(f"sqlite:{handle}") is not None:
        return

    providers.register_provider(
        SqliteProvider(handle, db_path, HISTORY_MIGRATIONS)
    )
    log.info("sqlite_history_db_registered", user_id=user_id, db_path=db_path)
