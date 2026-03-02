# core/slots.py
#
# Author:  Logicish
# Company: Logic-Ish Designs
# Date:    2/26/2026
#
# ==================================================
# User state management.
# Loads profiles, assigns slots, manages locks,
# enforces security level checks.
#
# Knows about: config (slot map, security, paths,
#              sampling defaults, idle timeout).
# ==================================================

# ==================================================
# Imports
# ==================================================
import asyncio
import json
import time
from dataclasses import dataclass, field
from pathlib import Path

import structlog

from config import (
    SLOT_MAP,
    USER_SECURITY,
    USER_DATA_ROOT,
    USER_IDLE_TIMEOUT,
    DEFAULT_TEMPERATURE,
    DEFAULT_MIN_P,
    DEFAULT_TOP_K,
    DEFAULT_REPEAT_PENALTY,
    DEFAULT_FREQUENCY_PENALTY,
    DEFAULT_MAX_TOKENS,
    BRAIN_NAME,
    BRAIN_DESCRIPTION,
    GUEST_ENABLED,
    SecurityLevel,
)

log = structlog.get_logger()

# ==================================================
# User Object
# ==================================================

@dataclass
class User:
    user_id:        str
    slot:           int
    security_level: int
    persona:        str
    voice_id:       str            = ""
    rag_scope:      list[str]      = field(default_factory=list)

    # runtime state
    conversation_history: list[dict] = field(default_factory=list)
    summary:        str            = ""
    flag_warn:      bool           = False
    flag_crit:      bool           = False
    is_idle_flag:   bool           = False
    last_active:    float          = field(default_factory=time.time)
    slot_lock:      asyncio.Lock   = field(default_factory=asyncio.Lock)

    # sampling params (loaded from profile or defaults)
    temperature:       float = DEFAULT_TEMPERATURE
    min_p:             float = DEFAULT_MIN_P
    top_k:             int   = DEFAULT_TOP_K
    repeat_penalty:    float = DEFAULT_REPEAT_PENALTY
    frequency_penalty: float = DEFAULT_FREQUENCY_PENALTY
    max_tokens:        int   = DEFAULT_MAX_TOKENS

    # --- paths ---

    @property
    def base_path(self) -> Path:
        return USER_DATA_ROOT / self.user_id

    @property
    def profile_path(self) -> Path:
        return self.base_path / "profile.json"

    @property
    def summary_path(self) -> Path:
        return self.base_path / "summary.txt"

    @property
    def history_db_path(self) -> Path:
        return self.base_path / "history.db"

    # --- activity tracking ---

    def touch(self):
        self.last_active = time.time()
        self.is_idle_flag = False

    def is_idle(self) -> bool:
        idle = (time.time() - self.last_active) > USER_IDLE_TIMEOUT
        self.is_idle_flag = idle
        return idle

    # --- message helpers ---

    def add_message(self, role: str, content: str):
        self.conversation_history.append({"role": role, "content": content})
        self.touch()

    def build_messages(self) -> list[dict]:
        system_content = self.persona
        if self.summary:
            system_content += f"\n\n[Conversation summary so far]:\n{self.summary}"
        return [
            {"role": "system", "content": system_content},
            *self.conversation_history,
        ]

    def clear_history(self):
        self.conversation_history = []
        self.flag_warn = False
        self.flag_crit = False


# ==================================================
# Profile I/O
# ==================================================

def _default_persona(user_id: str) -> str:
    return (
        f"You are {BRAIN_NAME}, a private home AI assistant. "
        f"{BRAIN_DESCRIPTION} "
        f"You are speaking with {user_id}. "
        "Be concise and direct. No filler."
    )


def _load_profile(user_id: str) -> User:
    if user_id not in SLOT_MAP:
        raise ValueError(f"Unknown user_id: '{user_id}'")

    slot           = SLOT_MAP[user_id]
    security_level = USER_SECURITY.get(user_id, SecurityLevel.GUEST)
    base_path      = USER_DATA_ROOT / user_id
    profile_path   = base_path / "profile.json"
    summary_path   = base_path / "summary.txt"

    persona   = _default_persona(user_id)
    voice_id  = ""
    rag_scope = []

    if profile_path.exists():
        try:
            data = json.loads(profile_path.read_text())
            persona        = data.get("persona", persona)
            voice_id       = data.get("voice_id", voice_id)
            rag_scope      = data.get("rag_scope", rag_scope)
            security_level = data.get("security_level", security_level)
            log.info("profile_loaded", user_id=user_id)
        except Exception as e:
            log.error("profile_load_error", user_id=user_id, error=str(e))
    else:
        log.info("profile_not_found_using_defaults", user_id=user_id)

    user = User(
        user_id=user_id,
        slot=slot,
        security_level=security_level,
        persona=persona,
        voice_id=voice_id,
        rag_scope=rag_scope,
    )

    if summary_path.exists():
        try:
            user.summary = summary_path.read_text().strip()
        except Exception as e:
            log.error("summary_load_error", user_id=user_id, error=str(e))

    return user


def save_profile(user: User):
    user.base_path.mkdir(parents=True, exist_ok=True)
    try:
        profile_data = {
            "user_id":        user.user_id,
            "slot":           user.slot,
            "security_level": user.security_level,
            "persona":        user.persona,
            "voice_id":       user.voice_id,
            "rag_scope":      user.rag_scope,
        }
        user.profile_path.write_text(json.dumps(profile_data, indent=2))
        user.summary_path.write_text(user.summary)
        log.info("profile_saved", user_id=user.user_id)
    except Exception as e:
        log.error("profile_save_error", user_id=user.user_id, error=str(e))


# ==================================================
# User Registry
# ==================================================

_active_users: dict[str, User] = {}


def resolve_user(user_id: str) -> str | None:
    # resolve username — known users pass through,
    # unknown users map to guest if enabled, else None
    user_id = user_id.lower().strip()
    if user_id in SLOT_MAP:
        return user_id
    if GUEST_ENABLED and "guest" in SLOT_MAP:
        log.info("unknown_user_mapped_to_guest", original=user_id)
        return "guest"
    return None


def get_user(user_id: str) -> User | None:
    resolved = resolve_user(user_id)
    if resolved is None:
        return None
    if resolved not in _active_users:
        _active_users[resolved] = _load_profile(resolved)
    return _active_users[resolved]


def init_all_users():
    # initialize all slots at startup
    for user_id in SLOT_MAP:
        if user_id not in _active_users:
            _active_users[user_id] = _load_profile(user_id)
            log.info("slot_initialized", user_id=user_id, slot=SLOT_MAP[user_id])


def get_all_users() -> dict[str, User]:
    return _active_users


def remove_user(user_id: str):
    if user_id in _active_users:
        save_profile(_active_users[user_id])
        del _active_users[user_id]
        log.info("user_removed", user_id=user_id)


def check_permission(user: User, required_level: int) -> bool:
    return user.security_level >= required_level


def shutdown_all():
    for uid, user in _active_users.items():
        save_profile(user)
        log.info("profile_saved_on_shutdown", user_id=uid)
    _active_users.clear()