# core/auth.py
#
# Author:  Logicish
# Company: Logic-Ish Designs
# Date:    4/18/2026
#
# ==================================================
# Authentication helpers.
# Password hashing (bcrypt) + session token CRUD.
# Sessions are stored in system.db and support
# a short-lived access token + long-lived refresh
# token pattern.
#
# Token TTLs:
#   Access token  — 24 hours
#   Refresh token — 30 days (sliding on use)
#
# Knows about: providers (system DB only).
# ==================================================

import secrets
from datetime import datetime, timedelta, timezone

import bcrypt
import structlog

log = structlog.get_logger()

_ACCESS_TTL  = timedelta(hours=24)
_REFRESH_TTL = timedelta(days=30)


# ==================================================
# Password
# ==================================================

def hash_password(password: str) -> str:
    return bcrypt.hashpw(password.encode(), bcrypt.gensalt(rounds=12)).decode()


def verify_password(password: str, stored_hash: str) -> bool:
    if not stored_hash:
        return False
    try:
        return bcrypt.checkpw(password.encode(), stored_hash.encode())
    except Exception:
        return False


# ==================================================
# Session CRUD
# ==================================================

async def create_session(user_id: str) -> dict:
    import providers
    db            = providers.get_db("system")
    token         = secrets.token_urlsafe(32)
    refresh_token = secrets.token_urlsafe(48)
    now           = datetime.now(timezone.utc)
    expires_at    = (now + _ACCESS_TTL).isoformat()
    refresh_exp   = (now + _REFRESH_TTL).isoformat()

    await db.execute(
        """INSERT INTO sessions
               (token, refresh_token, user_id, expires_at, refresh_expires_at, last_used)
           VALUES (?, ?, ?, ?, ?, ?)""",
        (token, refresh_token, user_id, expires_at, refresh_exp, now.isoformat()),
    )
    log.info("session_created", user_id=user_id)
    return {
        "token":              token,
        "refresh_token":      refresh_token,
        "expires_at":         expires_at,
        "refresh_expires_at": refresh_exp,
    }


async def verify_token(token: str) -> str | None:
    """Return user_id if token is valid and not expired, else None."""
    import providers
    db  = providers.get_db("system")
    now = datetime.now(timezone.utc).isoformat()

    row = await db.fetchone(
        "SELECT user_id, expires_at FROM sessions WHERE token = ?",
        (token,),
    )
    if row is None:
        return None
    if row["expires_at"] < now:
        await db.execute("DELETE FROM sessions WHERE token = ?", (token,))
        return None

    await db.execute(
        "UPDATE sessions SET last_used = ? WHERE token = ?",
        (now, token),
    )
    return row["user_id"]


async def refresh_session(refresh_token: str) -> dict | None:
    """Exchange a valid refresh token for a new session pair."""
    import providers
    db  = providers.get_db("system")
    now = datetime.now(timezone.utc).isoformat()

    row = await db.fetchone(
        "SELECT user_id, refresh_expires_at FROM sessions WHERE refresh_token = ?",
        (refresh_token,),
    )
    if row is None or row["refresh_expires_at"] < now:
        return None

    await db.execute("DELETE FROM sessions WHERE refresh_token = ?", (refresh_token,))
    return await create_session(row["user_id"])


async def revoke_session(token: str) -> None:
    import providers
    db = providers.get_db("system")
    await db.execute("DELETE FROM sessions WHERE token = ?", (token,))
    log.info("session_revoked")


async def revoke_all_sessions(user_id: str) -> None:
    import providers
    db = providers.get_db("system")
    await db.execute("DELETE FROM sessions WHERE user_id = ?", (user_id,))
    log.info("all_sessions_revoked", user_id=user_id)


async def prune_sessions() -> int:
    """Delete expired sessions. Returns number of rows pruned."""
    import providers
    db  = providers.get_db("system")
    now = datetime.now(timezone.utc).isoformat()
    await db.execute("DELETE FROM sessions WHERE refresh_expires_at < ?", (now,))
    log.debug("sessions_pruned")
    return 0
