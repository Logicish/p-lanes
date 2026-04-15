# providers/sqlite/provider.py
#
# Author:  Logicish
# Company: Logic-Ish Designs
# Date:    4/15/2026
#
# ==================================================
# SQLite provider — persistent structured data store.
#
# Manages a single aiosqlite connection with WAL mode
# enabled for concurrent read access. Schema versioning
# is handled via a _meta table; migrations are applied
# in order on start().
#
# Public API (used by modules):
#   execute(sql, params)  — INSERT/UPDATE/DELETE
#   fetchall(sql, params) — returns list[dict]
#   fetchone(sql, params) — returns dict | None
#
# Self-contained: reads providers/sqlite/config.yaml.
# Knows about: providers (registry), providers.base only.
# ==================================================

# ==================================================
# Imports
# ==================================================
import asyncio
from pathlib import Path
from typing import Any

import aiosqlite
import structlog

from providers.base import Provider

log = structlog.get_logger()

# ==================================================
# Schema migrations
# Each entry is applied in order. Never edit existing
# entries — add new ones to evolve the schema.
# ==================================================

_MIGRATIONS: list[str] = [

    # 001 — initial schema
    """
    CREATE TABLE IF NOT EXISTS _meta (
        key   TEXT PRIMARY KEY,
        value TEXT NOT NULL
    );

    INSERT OR IGNORE INTO _meta (key, value) VALUES ('schema_version', '0');

    CREATE TABLE IF NOT EXISTS garmin_metrics (
        id          INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id     TEXT    NOT NULL,
        date        TEXT    NOT NULL,
        hrv         REAL,
        sleep_score INTEGER,
        sleep_min   INTEGER,
        steps       INTEGER,
        stress      INTEGER,
        calories    INTEGER,
        created_at  TEXT    NOT NULL DEFAULT (datetime('now')),
        UNIQUE(user_id, date)
    );

    CREATE TABLE IF NOT EXISTS weight_log (
        id          INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id     TEXT    NOT NULL,
        recorded_at TEXT    NOT NULL,
        weight_kg   REAL    NOT NULL,
        notes       TEXT,
        created_at  TEXT    NOT NULL DEFAULT (datetime('now'))
    );

    CREATE TABLE IF NOT EXISTS plants (
        id          INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id     TEXT    NOT NULL,
        name        TEXT    NOT NULL,
        species     TEXT,
        location    TEXT,
        notes       TEXT,
        created_at  TEXT    NOT NULL DEFAULT (datetime('now'))
    );

    CREATE TABLE IF NOT EXISTS plant_checkins (
        id          INTEGER PRIMARY KEY AUTOINCREMENT,
        plant_id    INTEGER NOT NULL REFERENCES plants(id),
        checked_at  TEXT    NOT NULL,
        condition   TEXT,
        notes       TEXT,
        photo_path  TEXT,
        created_at  TEXT    NOT NULL DEFAULT (datetime('now'))
    );

    CREATE TABLE IF NOT EXISTS transactions (
        id           INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id      TEXT    NOT NULL,
        date         TEXT    NOT NULL,
        amount_cents INTEGER NOT NULL,
        category     TEXT,
        merchant     TEXT,
        description  TEXT,
        source_file  TEXT,
        created_at   TEXT    NOT NULL DEFAULT (datetime('now'))
    );

    CREATE TABLE IF NOT EXISTS component_inventory (
        id            INTEGER PRIMARY KEY AUTOINCREMENT,
        name          TEXT    NOT NULL,
        category      TEXT,
        qty           INTEGER NOT NULL DEFAULT 0,
        location      TEXT,
        notes         TEXT,
        datasheet_ref TEXT,
        added_at      TEXT    NOT NULL DEFAULT (datetime('now')),
        updated_at    TEXT    NOT NULL DEFAULT (datetime('now'))
    );

    CREATE TABLE IF NOT EXISTS weather_history (
        id          INTEGER PRIMARY KEY AUTOINCREMENT,
        recorded_at TEXT    NOT NULL,
        source      TEXT,
        temp_c      REAL,
        humidity    REAL,
        conditions  TEXT,
        created_at  TEXT    NOT NULL DEFAULT (datetime('now'))
    );

    CREATE TABLE IF NOT EXISTS workouts (
        id           INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id      TEXT    NOT NULL,
        date         TEXT    NOT NULL,
        type         TEXT,
        duration_min INTEGER,
        distance_m   REAL,
        notes        TEXT,
        source       TEXT,
        created_at   TEXT    NOT NULL DEFAULT (datetime('now'))
    );
    """,

]


# ==================================================
# SqliteProvider
# ==================================================

class SqliteProvider(Provider):

    def __init__(self, cfg: dict):
        self.db_path:  str            = cfg.get("db_path", "/var/lib/p-lanes/db/p-lanes.db")
        self._conn:    aiosqlite.Connection | None = None
        self._ready:   bool           = False
        self._lock:    asyncio.Lock   = asyncio.Lock()

    # --------------------------------------------------
    # Provider identity / state
    # --------------------------------------------------

    @property
    def name(self) -> str:
        return "sqlite"

    @property
    def is_ready(self) -> bool:
        return self._ready

    # --------------------------------------------------
    # Lifecycle
    # --------------------------------------------------

    async def start(self) -> bool:
        try:
            Path(self.db_path).parent.mkdir(parents=True, exist_ok=True)

            self._conn = await aiosqlite.connect(self.db_path)
            self._conn.row_factory = aiosqlite.Row

            await self._conn.execute("PRAGMA journal_mode=WAL")
            await self._conn.execute("PRAGMA foreign_keys=ON")
            await self._conn.commit()

            await self._migrate()

            self._ready = True
            log.info("sqlite_ready", db_path=self.db_path)
            return True

        except Exception as e:
            log.error("sqlite_start_failed", error=str(e))
            return False

    async def stop(self) -> None:
        self._ready = False
        if self._conn:
            await self._conn.close()
            self._conn = None
        log.info("sqlite_stopped")

    # --------------------------------------------------
    # Migrations
    # --------------------------------------------------

    async def _migrate(self) -> None:
        """Apply pending migrations in order."""
        # On a fresh DB _meta doesn't exist yet — that's version -1
        try:
            async with self._conn.execute(
                "SELECT value FROM _meta WHERE key = 'schema_version'"
            ) as cur:
                row = await cur.fetchone()
            current = int(row["value"]) if row else -1
        except aiosqlite.OperationalError:
            current = -1

        pending = _MIGRATIONS[current + 1:]
        if not pending:
            log.debug("sqlite_schema_current", version=current)
            return

        for i, sql in enumerate(pending):
            version = current + 1 + i
            log.info("sqlite_migration_applying", version=version)
            await self._conn.executescript(sql)
            await self._conn.execute(
                "UPDATE _meta SET value = ? WHERE key = 'schema_version'",
                (str(version),),
            )
            await self._conn.commit()
            log.info("sqlite_migration_done", version=version)

    # --------------------------------------------------
    # Public API
    # --------------------------------------------------

    async def execute(
        self,
        sql: str,
        params: tuple[Any, ...] = (),
    ) -> int:
        """Run an INSERT, UPDATE, or DELETE.

        Returns:
            lastrowid for INSERT, rowcount otherwise.
        """
        async with self._lock:
            async with self._conn.execute(sql, params) as cur:
                await self._conn.commit()
                return cur.lastrowid

    async def fetchall(
        self,
        sql: str,
        params: tuple[Any, ...] = (),
    ) -> list[dict]:
        """Run a SELECT and return all rows as dicts."""
        async with self._conn.execute(sql, params) as cur:
            rows = await cur.fetchall()
            return [dict(row) for row in rows]

    async def fetchone(
        self,
        sql: str,
        params: tuple[Any, ...] = (),
    ) -> dict | None:
        """Run a SELECT and return the first row as a dict, or None."""
        async with self._conn.execute(sql, params) as cur:
            row = await cur.fetchone()
            return dict(row) if row else None
