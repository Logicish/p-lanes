# providers/sqlite/provider.py
#
# Author:  Logicish
# Company: Logic-Ish Designs
# Date:    4/15/2026
#
# ==================================================
# SQLite provider — persistent structured data store.
#
# One instance per database file. Each instance is
# registered in the provider registry under the key
# "sqlite:{handle}" (e.g. "sqlite:system", "sqlite:root").
#
# Constructor args:
#   handle     — logical name ("system", user_id, etc.)
#   path       — absolute path to the .db file
#   migrations — ordered list of SQL migration strings
#
# Schema versioning is handled via a _meta table;
# migrations are applied in order on start().
# WAL mode enabled for concurrent read access.
#
# Public API (used by modules):
#   execute(sql, params)  — INSERT/UPDATE/DELETE
#   fetchall(sql, params) — returns list[dict]
#   fetchone(sql, params) — returns dict | None
#
# Knows about: providers.base only.
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
# System DB migrations
# Tables: system_health, weather_history
# ==================================================

SYSTEM_MIGRATIONS: list[str] = [

    # 000 — initial schema
    """
    CREATE TABLE IF NOT EXISTS _meta (
        key   TEXT PRIMARY KEY,
        value TEXT NOT NULL
    );

    INSERT OR IGNORE INTO _meta (key, value) VALUES ('schema_version', '0');

    CREATE TABLE IF NOT EXISTS system_health (
        id            INTEGER PRIMARY KEY AUTOINCREMENT,
        ts            TEXT    NOT NULL,
        cpu_pct       REAL,
        ram_used_mb   INTEGER,
        ram_total_mb  INTEGER,
        swap_used_mb  INTEGER,
        disk_used_gb  REAL,
        disk_total_gb REAL,
        vram_used_mb  INTEGER,
        vram_total_mb INTEGER,
        gpu_temp_c    INTEGER,
        gpu_util_pct  INTEGER,
        llm_running   INTEGER NOT NULL DEFAULT 0,
        created_at    TEXT    NOT NULL DEFAULT (datetime('now'))
    );

    CREATE INDEX IF NOT EXISTS idx_system_health_ts ON system_health(ts);

    CREATE TABLE IF NOT EXISTS weather_history (
        id          INTEGER PRIMARY KEY AUTOINCREMENT,
        recorded_at TEXT    NOT NULL,
        source      TEXT,
        temp_c      REAL,
        humidity    REAL,
        conditions  TEXT,
        created_at  TEXT    NOT NULL DEFAULT (datetime('now'))
    );
    """,

    # 001 — sessions table for SPA auth
    """
    CREATE TABLE IF NOT EXISTS sessions (
        token              TEXT PRIMARY KEY,
        refresh_token      TEXT UNIQUE NOT NULL,
        user_id            TEXT NOT NULL,
        expires_at         TEXT NOT NULL,
        refresh_expires_at TEXT NOT NULL,
        created_at         TEXT NOT NULL DEFAULT (datetime('now')),
        last_used          TEXT
    );

    CREATE INDEX IF NOT EXISTS idx_sessions_refresh ON sessions(refresh_token);
    CREATE INDEX IF NOT EXISTS idx_sessions_user    ON sessions(user_id);
    UPDATE _meta SET value = '1' WHERE key = 'schema_version';
    """,

    # 002 — replace local-probe system_health with HA-sourced multi-container schema;
    #        add separate gpu_health table for nvidia-smi data
    """
    DROP TABLE IF EXISTS system_health;

    CREATE TABLE system_health (
        ts                   TEXT NOT NULL,
        -- brain (p-lanes server)
        brain_cpu_pct        REAL,
        brain_mem_used_gb    REAL,
        brain_mem_total_gb   REAL,
        brain_mem_pct        REAL,
        brain_disk_used_gb   REAL,
        brain_disk_total_gb  REAL,
        brain_net_in_gb      REAL,
        brain_net_out_gb     REAL,
        brain_uptime_h       REAL,
        brain_status         TEXT,
        -- ears (whisper STT LXC)
        ears_cpu_pct         REAL,
        ears_mem_used_gb     REAL,
        ears_mem_total_gb    REAL,
        ears_mem_pct         REAL,
        ears_disk_used_gb    REAL,
        ears_disk_total_gb   REAL,
        ears_net_in_gb       REAL,
        ears_net_out_gb      REAL,
        ears_uptime_h        REAL,
        ears_status          TEXT,
        -- voice (kokoro TTS LXC)
        voice_cpu_pct        REAL,
        voice_mem_used_gb    REAL,
        voice_mem_total_gb   REAL,
        voice_mem_pct        REAL,
        voice_disk_used_gb   REAL,
        voice_disk_total_gb  REAL,
        voice_net_in_gb      REAL,
        voice_net_out_gb     REAL,
        voice_uptime_h       REAL,
        voice_status         TEXT,
        -- omen (Proxmox root VM)
        omen_cpu_pct         REAL,
        omen_mem_used_gb     REAL,
        omen_mem_total_gb    REAL,
        omen_mem_pct         REAL,
        omen_disk_used_gb    REAL,
        omen_disk_total_gb   REAL,
        omen_net_in_gb       REAL,
        omen_net_out_gb      REAL,
        omen_uptime_h        REAL,
        omen_status          TEXT,
        -- artist (on-demand image generator LXC)
        artist_cpu_pct       REAL,
        artist_mem_used_gb   REAL,
        artist_mem_total_gb  REAL,
        artist_mem_pct       REAL,
        artist_disk_used_gb  REAL,
        artist_disk_total_gb REAL,
        artist_net_in_gb     REAL,
        artist_net_out_gb    REAL,
        artist_uptime_h      REAL,
        artist_status        TEXT,
        -- p-lanes process state
        llm_running          INTEGER NOT NULL DEFAULT 0,
        created_at           TEXT    NOT NULL DEFAULT (datetime('now'))
    );

    CREATE INDEX IF NOT EXISTS idx_system_health_ts ON system_health(ts);

    CREATE TABLE IF NOT EXISTS gpu_health (
        ts            TEXT    NOT NULL,
        vram_used_mb  INTEGER,
        vram_total_mb INTEGER,
        gpu_temp_c    INTEGER,
        gpu_util_pct  INTEGER,
        created_at    TEXT    NOT NULL DEFAULT (datetime('now'))
    );

    CREATE INDEX IF NOT EXISTS idx_gpu_health_ts ON gpu_health(ts);

    UPDATE _meta SET value = '2' WHERE key = 'schema_version';
    """,

    # 003 — commands registry: all available voice/chat commands with syntax,
    #        description, category, and minimum security level
    """
    CREATE TABLE IF NOT EXISTS commands (
        id           INTEGER PRIMARY KEY AUTOINCREMENT,
        name         TEXT    NOT NULL,
        syntax       TEXT    NOT NULL,
        description  TEXT    NOT NULL,
        category     TEXT    NOT NULL,
        min_security INTEGER NOT NULL DEFAULT 0
    );

    INSERT INTO commands (name, syntax, description, category, min_security) VALUES
        -- search (GUEST)
        ('Web Search',      'search for [topic]',                              'Search the web for current or live information',              'search',  0),
        ('Local Knowledge', 'what do you know about [topic]',                  'Search stored notes and local knowledge base',                'search',  0),
        ('Think Mode',      'think through this [topic]',                      'Engage extended reasoning for complex problems',              'search',  0),
        -- weather (GUEST)
        ('Weather',         'what''s the weather',                             'Get current conditions and short forecast from Home Assistant','weather', 0),
        -- memory (GUEST)
        ('Save Note',       'make a note that [text]',                         'Store a fact, detail, or technical note in your knowledge',   'memory',  0),
        ('Fact Check',      'fact check that / verify that',                   'Verify the last assistant response via web search',           'memory',  0),
        -- system (GUEST)
        ('List Commands',   'list commands / what can you do',                 'Show all commands available at your access level',            'system',  0),
        -- home (USER)
        ('Device Status',   'is the [device] on? / check the [device]',       'Read the current state of any Home Assistant entity',         'home',    1),
        ('Lights',          'turn on/off [light] / dim the [light] / set the brightness [light] to [%] / change the light color to [color]',
                                                                               'Control smart lights',                                        'home',    1),
        ('Switches & Fans', 'turn on/off [device] / toggle the [device]',     'Control switches and fans',                                   'home',    1),
        -- media (USER)
        ('Play',            'play [song/artist/playlist]',                     'Start audio playback on the nearest satellite',               'media',   1),
        ('Pause/Stop',      'pause the music / stop the music',                'Pause or stop playback',                                      'media',   1),
        ('Skip',            'skip this / next track / previous track',         'Navigate between tracks',                                     'media',   1),
        ('Volume',          'turn up/down the volume',                         'Adjust playback volume',                                      'media',   1),
        ('Mute',            'mute/unmute the music',                           'Toggle mute on active satellite',                             'media',   1),
        -- timers (USER)
        ('Timer',           'set a timer for [duration]',                      'Set a local countdown timer',                                 'timers',  1),
        ('Wake Alarm',      'wake me up at [time] / wake me up in [duration]', 'Set a wake-up alarm',                                         'timers',  1),
        ('Reminder',        'remind me in [duration] / remind me at [time]',   'Set a timed reminder',                                        'timers',  1),
        -- system (USER)
        ('Server Health',   'how is the system? / system status',              'Get a 24-hour health report with grade for all containers',   'system',  1),
        -- home (TRUSTED level 2+)
        ('Thermostat',      'set the thermostat to [temp] / set the heat to [temp]',
                                                                               'Control climate and AC (Trusted access required)',            'home',    2),
        -- home (TRUSTED level 3+)
        ('Locks',           'lock/unlock the [door]',                          'Lock or unlock smart doors (Trusted access required)',        'home',    3);

    UPDATE _meta SET value = '3' WHERE key = 'schema_version';
    """,

    # 004 — 5-min health poller tables, daily rollup, notifications, alert state
    """
    CREATE TABLE IF NOT EXISTS system_health_5min (
        ts                   TEXT NOT NULL,
        brain_cpu_pct        REAL, brain_mem_used_gb    REAL, brain_mem_total_gb   REAL, brain_mem_pct        REAL,
        brain_disk_used_gb   REAL, brain_disk_total_gb  REAL, brain_net_in_gb      REAL, brain_net_out_gb     REAL,
        brain_uptime_h       REAL, brain_status         TEXT,
        ears_cpu_pct         REAL, ears_mem_used_gb     REAL, ears_mem_total_gb    REAL, ears_mem_pct         REAL,
        ears_disk_used_gb    REAL, ears_disk_total_gb   REAL, ears_net_in_gb       REAL, ears_net_out_gb      REAL,
        ears_uptime_h        REAL, ears_status          TEXT,
        voice_cpu_pct        REAL, voice_mem_used_gb    REAL, voice_mem_total_gb   REAL, voice_mem_pct        REAL,
        voice_disk_used_gb   REAL, voice_disk_total_gb  REAL, voice_net_in_gb      REAL, voice_net_out_gb     REAL,
        voice_uptime_h       REAL, voice_status         TEXT,
        omen_cpu_pct         REAL, omen_mem_used_gb     REAL, omen_mem_total_gb    REAL, omen_mem_pct         REAL,
        omen_disk_used_gb    REAL, omen_disk_total_gb   REAL, omen_net_in_gb       REAL, omen_net_out_gb      REAL,
        omen_uptime_h        REAL, omen_status          TEXT,
        artist_cpu_pct       REAL, artist_mem_used_gb   REAL, artist_mem_total_gb  REAL, artist_mem_pct       REAL,
        artist_disk_used_gb  REAL, artist_disk_total_gb REAL, artist_net_in_gb     REAL, artist_net_out_gb    REAL,
        artist_uptime_h      REAL, artist_status        TEXT,
        llm_running          INTEGER NOT NULL DEFAULT 0,
        created_at           TEXT    NOT NULL DEFAULT (datetime('now'))
    );
    CREATE INDEX IF NOT EXISTS idx_sh5_ts ON system_health_5min(ts);

    CREATE TABLE IF NOT EXISTS gpu_health_5min (
        ts            TEXT    NOT NULL,
        vram_used_mb  INTEGER,
        vram_total_mb INTEGER,
        gpu_temp_c    INTEGER,
        gpu_util_pct  INTEGER,
        created_at    TEXT    NOT NULL DEFAULT (datetime('now'))
    );
    CREATE INDEX IF NOT EXISTS idx_gh5_ts ON gpu_health_5min(ts);

    CREATE TABLE IF NOT EXISTS health_daily (
        date                  TEXT NOT NULL UNIQUE,
        brain_cpu_avg         REAL, brain_cpu_peak        REAL,
        brain_mem_avg         REAL, brain_mem_peak        REAL,
        brain_disk_eod        REAL, brain_net_in_delta    REAL, brain_net_out_delta  REAL,
        brain_uptime_min      REAL, brain_status_eod      TEXT,
        ears_cpu_avg          REAL, ears_cpu_peak         REAL,
        ears_mem_avg          REAL, ears_mem_peak         REAL,
        ears_disk_eod         REAL, ears_net_in_delta     REAL, ears_net_out_delta   REAL,
        ears_uptime_min       REAL, ears_status_eod       TEXT,
        voice_cpu_avg         REAL, voice_cpu_peak        REAL,
        voice_mem_avg         REAL, voice_mem_peak        REAL,
        voice_disk_eod        REAL, voice_net_in_delta    REAL, voice_net_out_delta  REAL,
        voice_uptime_min      REAL, voice_status_eod      TEXT,
        omen_cpu_avg          REAL, omen_cpu_peak         REAL,
        omen_mem_avg          REAL, omen_mem_peak         REAL,
        omen_disk_eod         REAL, omen_net_in_delta     REAL, omen_net_out_delta   REAL,
        omen_uptime_min       REAL, omen_status_eod       TEXT,
        artist_cpu_avg        REAL, artist_cpu_peak       REAL,
        artist_mem_avg        REAL, artist_mem_peak       REAL,
        artist_disk_eod       REAL, artist_net_in_delta   REAL, artist_net_out_delta REAL,
        artist_uptime_min     REAL, artist_status_eod     TEXT,
        gpu_vram_avg          REAL, gpu_vram_peak         INTEGER,
        gpu_temp_avg          REAL, gpu_temp_peak         INTEGER,
        gpu_util_avg          REAL, gpu_util_peak         INTEGER,
        llm_uptime_pct        REAL,
        sample_count          INTEGER,
        notes                 TEXT,
        created_at            TEXT NOT NULL DEFAULT (datetime('now'))
    );

    CREATE TABLE IF NOT EXISTS notifications (
        id         INTEGER PRIMARY KEY AUTOINCREMENT,
        type       TEXT NOT NULL,
        severity   TEXT NOT NULL,
        host       TEXT,
        message    TEXT NOT NULL,
        read_at    TEXT,
        created_at TEXT NOT NULL DEFAULT (datetime('now'))
    );
    CREATE INDEX IF NOT EXISTS idx_notif_created ON notifications(created_at);

    CREATE TABLE IF NOT EXISTS alert_state (
        rule_id       TEXT PRIMARY KEY,
        active_since  TEXT,
        last_alerted  TEXT,
        snoozed_until TEXT
    );

    UPDATE _meta SET value = '4' WHERE key = 'schema_version';
    """,

    # 005 — finance processing command in commands registry
    """
    INSERT OR IGNORE INTO commands (name, syntax, description, category, min_security) VALUES
        ('Process Finance Files', 'process finance files / process my finance files',
         'Ingest PDF, TXT, and CSV bank/credit statements from the finance dump folder',
         'finance', 4);

    UPDATE _meta SET value = '5' WHERE key = 'schema_version';
    """,

]


# ==================================================
# User DB migrations
# Tables: garmin_metrics, weight_log, plants,
#         plant_checkins, transactions,
#         component_inventory, workouts
# ==================================================

USER_MIGRATIONS: list[str] = [

    # 000 — initial schema
    """
    CREATE TABLE IF NOT EXISTS _meta (
        key   TEXT PRIMARY KEY,
        value TEXT NOT NULL
    );

    INSERT OR IGNORE INTO _meta (key, value) VALUES ('schema_version', '0');

    CREATE TABLE IF NOT EXISTS garmin_metrics (
        id                 INTEGER PRIMARY KEY AUTOINCREMENT,
        date               TEXT    NOT NULL UNIQUE,
        hrv                REAL,
        sleep_score        INTEGER,
        sleep_min          INTEGER,
        steps              INTEGER,
        stress             INTEGER,
        calories           INTEGER,
        resting_hr         INTEGER,
        spo2               REAL,
        body_battery_high  INTEGER,
        body_battery_low   INTEGER,
        active_min         INTEGER,
        created_at         TEXT    NOT NULL DEFAULT (datetime('now'))
    );

    CREATE TABLE IF NOT EXISTS weight_log (
        id          INTEGER PRIMARY KEY AUTOINCREMENT,
        recorded_at TEXT    NOT NULL,
        weight_kg   REAL    NOT NULL,
        notes       TEXT,
        created_at  TEXT    NOT NULL DEFAULT (datetime('now'))
    );

    CREATE TABLE IF NOT EXISTS transactions (
        id           INTEGER PRIMARY KEY AUTOINCREMENT,
        date         TEXT    NOT NULL,
        amount_cents INTEGER NOT NULL,
        category     TEXT,
        merchant     TEXT,
        description  TEXT,
        source_file  TEXT,
        created_at   TEXT    NOT NULL DEFAULT (datetime('now'))
    );


    CREATE TABLE IF NOT EXISTS workouts (
        id           INTEGER PRIMARY KEY AUTOINCREMENT,
        date         TEXT    NOT NULL,
        type         TEXT,
        duration_min INTEGER,
        distance_m   REAL,
        notes        TEXT,
        source       TEXT,
        created_at   TEXT    NOT NULL DEFAULT (datetime('now'))
    );
    """,

    # 001 — workout journal: exercise library + sessions + sets
    """
    CREATE TABLE IF NOT EXISTS exercises (
        id           INTEGER PRIMARY KEY AUTOINCREMENT,
        name         TEXT    NOT NULL UNIQUE,
        category     TEXT    NOT NULL,
        muscle_group TEXT,
        equipment    TEXT,
        is_custom    INTEGER NOT NULL DEFAULT 0,
        created_at   TEXT    NOT NULL DEFAULT (datetime('now'))
    );

    CREATE TABLE IF NOT EXISTS workout_sessions (
        id           INTEGER PRIMARY KEY AUTOINCREMENT,
        date         TEXT    NOT NULL,
        name         TEXT,
        notes        TEXT,
        duration_min INTEGER,
        created_at   TEXT    NOT NULL DEFAULT (datetime('now'))
    );

    CREATE TABLE IF NOT EXISTS workout_sets (
        id           INTEGER PRIMARY KEY AUTOINCREMENT,
        session_id   INTEGER NOT NULL REFERENCES workout_sessions(id) ON DELETE CASCADE,
        exercise_id  INTEGER NOT NULL REFERENCES exercises(id),
        set_order    INTEGER NOT NULL,
        reps         INTEGER,
        weight       REAL,
        distance_m   REAL,
        duration_sec INTEGER,
        created_at   TEXT    NOT NULL DEFAULT (datetime('now'))
    );

    INSERT OR IGNORE INTO exercises (name, category, muscle_group, equipment) VALUES
        ('Bench Press',            'strength',   'chest',     'barbell'),
        ('Incline Bench Press',    'strength',   'chest',     'barbell'),
        ('Dumbbell Chest Press',   'strength',   'chest',     'dumbbell'),
        ('Incline Dumbbell Press', 'strength',   'chest',     'dumbbell'),
        ('Chest Fly',              'strength',   'chest',     'dumbbell'),
        ('Cable Chest Fly',        'strength',   'chest',     'cable'),
        ('Push-up',                'bodyweight', 'chest',     'bodyweight'),
        ('Barbell Row',            'strength',   'back',      'barbell'),
        ('Lat Pulldown',           'strength',   'back',      'machine'),
        ('Cable Row',              'strength',   'back',      'cable'),
        ('Pull-up',                'bodyweight', 'back',      'bodyweight'),
        ('Deadlift',               'strength',   'back',      'barbell'),
        ('Romanian Deadlift',      'strength',   'back',      'barbell'),
        ('Dumbbell Row',           'strength',   'back',      'dumbbell'),
        ('Squat',                  'strength',   'legs',      'barbell'),
        ('Leg Press',              'strength',   'legs',      'machine'),
        ('Leg Curl',               'strength',   'legs',      'machine'),
        ('Leg Extension',          'strength',   'legs',      'machine'),
        ('Hip Thrust',             'strength',   'legs',      'barbell'),
        ('Bulgarian Split Squat',  'strength',   'legs',      'dumbbell'),
        ('Lunges',                 'strength',   'legs',      'dumbbell'),
        ('Calf Raise',             'strength',   'legs',      'machine'),
        ('Overhead Press',         'strength',   'shoulders', 'barbell'),
        ('Dumbbell Shoulder Press','strength',   'shoulders', 'dumbbell'),
        ('Lateral Raise',          'strength',   'shoulders', 'dumbbell'),
        ('Face Pull',              'strength',   'shoulders', 'cable'),
        ('Rear Delt Fly',          'strength',   'shoulders', 'dumbbell'),
        ('Barbell Curl',           'strength',   'arms',      'barbell'),
        ('Dumbbell Curl',          'strength',   'arms',      'dumbbell'),
        ('Hammer Curl',            'strength',   'arms',      'dumbbell'),
        ('Tricep Pushdown',        'strength',   'arms',      'cable'),
        ('Skull Crusher',          'strength',   'arms',      'barbell'),
        ('Dip',                    'bodyweight', 'arms',      'bodyweight'),
        ('Plank',                  'bodyweight', 'core',      'bodyweight'),
        ('Running',                'cardio',     NULL,        'other'),
        ('Cycling',                'cardio',     NULL,        'cardio_machine'),
        ('Rowing Machine',         'cardio',     NULL,        'cardio_machine'),
        ('Elliptical',             'cardio',     NULL,        'cardio_machine'),
        ('Stair Climber',          'cardio',     NULL,        'cardio_machine'),
        ('Jump Rope',              'cardio',     NULL,        'other'),
        ('Treadmill',              'cardio',     NULL,        'cardio_machine'),
        ('Walking',                'cardio',     NULL,        'other');

    UPDATE _meta SET value = '1' WHERE key = 'schema_version';
    """,

]


# ==================================================
# House DB migrations
# Tables: plants, plant_checkins, component_inventory
# ==================================================

HOUSE_MIGRATIONS: list[str] = [

    # 000 — initial schema
    """
    CREATE TABLE IF NOT EXISTS _meta (
        key   TEXT PRIMARY KEY,
        value TEXT NOT NULL
    );

    INSERT OR IGNORE INTO _meta (key, value) VALUES ('schema_version', '0');

    CREATE TABLE IF NOT EXISTS plants (
        id          INTEGER PRIMARY KEY AUTOINCREMENT,
        name        TEXT    NOT NULL UNIQUE,
        species     TEXT,
        location    TEXT,
        notes       TEXT,
        created_at  TEXT    NOT NULL DEFAULT (datetime('now'))
    );

    CREATE TABLE IF NOT EXISTS plant_checkins (
        id                INTEGER PRIMARY KEY AUTOINCREMENT,
        plant_id          INTEGER NOT NULL REFERENCES plants(id),
        checked_at        TEXT    NOT NULL,
        leaf_color_health INTEGER,
        wilting_score     INTEGER,
        disease_spots     INTEGER,
        new_growth        INTEGER,
        overall_vigor     INTEGER,
        condition         TEXT,
        notes             TEXT,
        created_at        TEXT    NOT NULL DEFAULT (datetime('now'))
    );

    CREATE INDEX IF NOT EXISTS idx_plant_checkins_plant_id
        ON plant_checkins(plant_id);

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
    """,

]


# ==================================================
# Game DB migrations
# Tables: game_crawl_queue, game_crawl_visited
# ==================================================

GAME_MIGRATIONS: list[str] = [

    # 000 — initial schema
    """
    CREATE TABLE IF NOT EXISTS _meta (
        key   TEXT PRIMARY KEY,
        value TEXT NOT NULL
    );

    INSERT OR IGNORE INTO _meta (key, value) VALUES ('schema_version', '0');

    CREATE TABLE IF NOT EXISTS game_crawl_queue (
        id            INTEGER PRIMARY KEY AUTOINCREMENT,
        game_name     TEXT    NOT NULL UNIQUE,
        seed_url      TEXT,
        status        TEXT    NOT NULL DEFAULT 'pending',
        force_delta   INTEGER NOT NULL DEFAULT 0,
        requested_at  TEXT    NOT NULL DEFAULT (datetime('now')),
        started_at    TEXT,
        completed_at  TEXT,
        pages_fetched INTEGER NOT NULL DEFAULT 0,
        error         TEXT
    );

    CREATE INDEX IF NOT EXISTS idx_game_crawl_queue_status
        ON game_crawl_queue(status);

    CREATE TABLE IF NOT EXISTS game_crawl_visited (
        id           INTEGER PRIMARY KEY AUTOINCREMENT,
        game_name    TEXT    NOT NULL,
        url          TEXT    NOT NULL,
        content_hash TEXT    NOT NULL,
        page_title   TEXT,
        fetched_at   TEXT    NOT NULL DEFAULT (datetime('now')),
        UNIQUE(game_name, url)
    );

    CREATE INDEX IF NOT EXISTS idx_game_crawl_visited_game
        ON game_crawl_visited(game_name);
    """,

    # 001 — crawler status flags (disk_full, folder_full)
    """
    CREATE TABLE IF NOT EXISTS crawler_status (
        key        TEXT PRIMARY KEY,
        value      TEXT NOT NULL,
        updated_at TEXT NOT NULL DEFAULT (datetime('now'))
    );
    """,

]


# ==================================================
# Recipe DB migrations
# Tables: recipes
# ==================================================

RECIPE_MIGRATIONS: list[str] = [

    # 000 — initial schema
    """
    CREATE TABLE IF NOT EXISTS _meta (
        key   TEXT PRIMARY KEY,
        value TEXT NOT NULL
    );

    INSERT OR IGNORE INTO _meta (key, value) VALUES ('schema_version', '0');

    CREATE TABLE IF NOT EXISTS recipes (
        id                   INTEGER PRIMARY KEY AUTOINCREMENT,
        title                TEXT    NOT NULL,
        ingredients          TEXT    NOT NULL,
        instructions         TEXT    NOT NULL,
        servings             INTEGER,
        calories_per_serving INTEGER,
        prep_min             INTEGER,
        cook_min             INTEGER,
        needs_llm            INTEGER NOT NULL DEFAULT 1,
        created_at           TEXT    NOT NULL DEFAULT (datetime('now'))
    );
    """,

]


# ==================================================
# Finance DB migrations
# Tables: accounts, transactions
# ==================================================

FINANCE_MIGRATIONS: list[str] = [

    # 000 — initial schema
    """
    CREATE TABLE IF NOT EXISTS _meta (
        key   TEXT PRIMARY KEY,
        value TEXT NOT NULL
    );

    INSERT OR IGNORE INTO _meta (key, value) VALUES ('schema_version', '0');

    CREATE TABLE IF NOT EXISTS accounts (
        id              INTEGER PRIMARY KEY AUTOINCREMENT,
        friendly_name   TEXT    NOT NULL,
        account_name    TEXT    NOT NULL,
        account_type    TEXT,
        last_four       TEXT,
        apr             REAL,
        ending_balance  REAL,
        statement_month TEXT    NOT NULL,
        source_file     TEXT,
        created_at      TEXT    NOT NULL DEFAULT (datetime('now')),
        UNIQUE(account_name, statement_month)
    );

    CREATE TABLE IF NOT EXISTS transactions (
        id            INTEGER PRIMARY KEY AUTOINCREMENT,
        account_id    INTEGER NOT NULL REFERENCES accounts(id),
        friendly_name TEXT,
        date          TEXT    NOT NULL,
        amount        REAL    NOT NULL,
        issuer        TEXT,
        description   TEXT,
        bucket1       TEXT,
        bucket2       TEXT,
        created_at    TEXT    NOT NULL DEFAULT (datetime('now')),
        UNIQUE(account_id, date, amount, issuer)
    );

    CREATE INDEX IF NOT EXISTS idx_transactions_account ON transactions(account_id);
    CREATE INDEX IF NOT EXISTS idx_transactions_date    ON transactions(date);
    CREATE INDEX IF NOT EXISTS idx_transactions_bucket1 ON transactions(bucket1);
    """,

]


# ==================================================
# History DB migrations (per user: users/<uid>/history.db)
# Tables: turns, prompts
# Written by core/turn_log.py; trimmed nightly by
# modules/turn_log_retention.py.
# ==================================================

HISTORY_MIGRATIONS: list[str] = [

    # 000 — initial schema
    # auto_vacuum lets the retention job hand freed pages back
    # to the OS. The provider switches to WAL before migrating,
    # which pins the default, so VACUUM at the end applies it.
    """
    PRAGMA auto_vacuum = INCREMENTAL;

    CREATE TABLE IF NOT EXISTS _meta (
        key   TEXT PRIMARY KEY,
        value TEXT NOT NULL
    );

    INSERT OR IGNORE INTO _meta (key, value) VALUES ('schema_version', '0');

    CREATE TABLE IF NOT EXISTS turns (
        turn_id          TEXT    PRIMARY KEY,          -- envelope.message_id
        ts               TEXT    NOT NULL,             -- UTC ISO, turn start
        user_id          TEXT    NOT NULL,             -- resolved user
        requested_user   TEXT,                         -- claimed user on denied requests
        source           TEXT,                         -- text | voice | api | ha
        device_id        TEXT,
        conversation_id  TEXT,
        stt_confidence   REAL,
        voice_confidence REAL,
        status           TEXT    NOT NULL,             -- ok | denied | aborted | busy | overflow | error | disconnected
        error            TEXT,
        user_text        TEXT,
        response         TEXT,
        intent           TEXT,
        tags             TEXT,                         -- JSON list (router score lives here)
        tool             TEXT,
        tool_args        TEXT,                         -- JSON
        tool_result      TEXT,
        directive        TEXT,                         -- system text injected this turn
        thinking         INTEGER NOT NULL DEFAULT 0,
        temperature      REAL,
        prompt_hash      TEXT,                         -- prompts.hash (persona version)
        total_tokens     INTEGER,
        llm_elapsed      REAL,
        total_ms         INTEGER,
        truncated        INTEGER NOT NULL DEFAULT 0,
        payload          BLOB,                         -- zlib(JSON {messages, sampling, think}); NULL once trimmed
        payload_bytes    INTEGER NOT NULL DEFAULT 0,
        flag             INTEGER NOT NULL DEFAULT 0,   -- -1 bad, 0 none, 1 good
        flag_source      TEXT,                         -- voice | text | web
        flag_note        TEXT,
        flag_ts          TEXT
    );

    CREATE INDEX IF NOT EXISTS idx_turns_ts   ON turns(ts);
    CREATE INDEX IF NOT EXISTS idx_turns_flag ON turns(flag) WHERE flag != 0;

    CREATE TABLE IF NOT EXISTS prompts (
        hash       TEXT PRIMARY KEY,
        first_seen TEXT NOT NULL,
        persona    TEXT NOT NULL
    );

    VACUUM;
    """,

]


# ==================================================
# SqliteProvider
# ==================================================

class SqliteProvider(Provider):

    def __init__(self, handle: str, path: str, migrations: list[str]):
        self._handle:     str            = handle
        self.db_path:     str            = path
        self._migrations: list[str]      = migrations
        self._conn:       aiosqlite.Connection | None = None
        self._ready:      bool           = False
        self._lock:       asyncio.Lock   = asyncio.Lock()

    # --------------------------------------------------
    # Provider identity / state
    # --------------------------------------------------

    @property
    def name(self) -> str:
        return f"sqlite:{self._handle}"

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
            log.info("sqlite_ready", handle=self._handle, db_path=self.db_path)
            return True

        except Exception as e:
            log.error("sqlite_start_failed", handle=self._handle, error=str(e))
            return False

    async def stop(self) -> None:
        self._ready = False
        if self._conn:
            await self._conn.close()
            self._conn = None
        log.info("sqlite_stopped", handle=self._handle)

    # --------------------------------------------------
    # Migrations
    # --------------------------------------------------

    async def _migrate(self) -> None:
        try:
            async with self._conn.execute(
                "SELECT value FROM _meta WHERE key = 'schema_version'"
            ) as cur:
                row = await cur.fetchone()
            current = int(row["value"]) if row else -1
        except aiosqlite.OperationalError:
            current = -1

        pending = self._migrations[current + 1:]
        if not pending:
            log.debug("sqlite_schema_current", handle=self._handle, version=current)
            return

        for i, sql in enumerate(pending):
            version = current + 1 + i
            log.info("sqlite_migration_applying", handle=self._handle, version=version)
            await self._conn.executescript(sql)
            await self._conn.execute(
                "UPDATE _meta SET value = ? WHERE key = 'schema_version'",
                (str(version),),
            )
            await self._conn.commit()
            log.info("sqlite_migration_done", handle=self._handle, version=version)

    # --------------------------------------------------
    # Public API
    # --------------------------------------------------

    async def execute(
        self,
        sql: str,
        params: tuple[Any, ...] = (),
    ) -> int:
        """Run an INSERT, UPDATE, or DELETE. Returns lastrowid."""
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

    async def pragma(self, sql: str) -> list[tuple]:
        """Run a PRAGMA and return raw tuples. Use for PRAGMAs that return
        zero-column rows (incremental_vacuum) — dict(Row) crashes on those."""
        async with self._lock:
            async with self._conn.execute(sql) as cur:
                return [tuple(r) for r in await cur.fetchall()]
