# config.py
#
# Author:  Logicish
# Company: Logic-Ish Designs
# Date:    2/26/2026
#
# ==================================================
# Single source of truth for the entire system.
# Loads config.yaml and exposes all settings as
# module-level attributes. Read-only at runtime.
# Only setup.py writes to the YAML file.
#
# Knows about: nothing — this is a leaf dependency.
# ==================================================

# ==================================================
# Imports
# ==================================================
from pathlib import Path

import yaml

# ==================================================
# Load Config
# ==================================================
_CONFIG_PATH = Path(__file__).parent / "config.yaml"
_USERS_PATH  = Path(__file__).parent / "users.yaml"

def _load_config() -> dict:
    if not _CONFIG_PATH.exists():
        raise FileNotFoundError(
            f"Config file not found: {_CONFIG_PATH}\n"
            "Run setup.py to generate one."
        )
    with open(_CONFIG_PATH, "r") as f:
        return yaml.safe_load(f)

def _load_users() -> dict:
    if not _USERS_PATH.exists():
        raise FileNotFoundError(
            f"Users file not found: {_USERS_PATH}\n"
            "Run setup.py to generate one, or create it manually.\n"
            "See users.yaml.example for format."
        )
    with open(_USERS_PATH, "r") as f:
        data = yaml.safe_load(f) or {}
    return data.get("users", {})

_cfg        = _load_config()
_users_cfg  = _load_users()

# ==================================================
# Identity
# ==================================================
BRAIN_NAME        = _cfg["brain"]["name"]
BRAIN_DESCRIPTION = _cfg["brain"]["description"]

# ==================================================
# Paths
# ==================================================
LOG_FILE       = Path(_cfg["paths"]["log_file"])
USER_DATA_ROOT = Path(_cfg["paths"]["user_data_root"])
MODEL_PATH     = Path(_cfg["paths"]["model"])
_proj = _cfg["paths"].get("projector", "")
PROJECTOR_PATH = Path(_proj) if _proj else None
LLAMA_SERVER   = Path(_cfg["paths"]["llama_server"])

# ==================================================
# Network
# ==================================================
LLM_HOST   = _cfg["network"]["llm_host"]
LLM_PORT   = _cfg["network"]["llm_port"]
HOST_BIND  = _cfg["network"]["brain_host"]
BRAIN_PORT = _cfg["network"]["brain_port"]

LLM_URL        = f"http://{LLM_HOST}:{LLM_PORT}/v1/chat/completions"
LLM_HEALTH_URL = f"http://{LLM_HOST}:{LLM_PORT}/health"

# ==================================================
# LLM Settings
# ==================================================
LLM_TIMEOUT         = _cfg["llm"]["timeout"]
LLM_STARTUP_TIMEOUT = _cfg["llm"]["startup_timeout"]
GPU_LAYERS          = _cfg["llm"]["gpu_layers"]
FLASH_ATTN          = _cfg["llm"]["flash_attn"]
MMPROJ_OFFLOAD      = _cfg["llm"].get("mmproj_offload", False)
REASONING_OFF       = _cfg["llm"].get("reasoning_off", False)
MLOCK               = _cfg["llm"]["mlock"]
KV_CACHE_TYPE       = _cfg["llm"]["kv_cache_type"]

# ==================================================
# LLM Crash Recovery
# ==================================================
_recovery_cfg         = _cfg["llm"].get("recovery", {})
RECOVERY_MAX_RETRIES  = _recovery_cfg.get("max_retries", 5)
RECOVERY_INITIAL_WAIT = _recovery_cfg.get("initial_wait", 5)
RECOVERY_MAX_WAIT     = _recovery_cfg.get("max_wait", 120)

# ==================================================
# GPU Guard (core/gpu_guard.py)
# ==================================================
_gg_cfg                        = _cfg.get("gpu_guard", {}) or {}
GPU_GUARD_ENABLED              = _gg_cfg.get("enabled", True)
GPU_GUARD_POLL_SECONDS         = _gg_cfg.get("poll_seconds", 15)
GPU_GUARD_HOLD_C               = _gg_cfg.get("hold_c", 83)
GPU_GUARD_KILL_C               = _gg_cfg.get("kill_c", 87)
GPU_GUARD_RESUME_C             = _gg_cfg.get("resume_c", 72)
GPU_GUARD_UNREACHABLE_S        = _gg_cfg.get("unreachable_s", 45)
GPU_GUARD_MAX_KILLS_PER_HOUR   = _gg_cfg.get("max_kills_per_hour", 2)
GPU_GUARD_NOTIFY_COOLDOWN_MIN  = _gg_cfg.get("notify_cooldown_min", 15)

# ==================================================
# Slots / KV Cache
# ==================================================
SLOT_COUNT       = _cfg["slots"]["count"]
CONTEXT_TOTAL    = _cfg["slots"]["ctx_total"]
CONTEXT_PER_SLOT = CONTEXT_TOTAL // SLOT_COUNT

# ==================================================
# Summarization
# ==================================================
_sum_cfg = _cfg["summarization"]

THRESHOLD_WARN        = _sum_cfg["threshold_warn"]
THRESHOLD_CRIT        = _sum_cfg["threshold_crit"]
SUMMARIZE_LOCK_WAIT   = _sum_cfg["lock_wait"]
SCHEDULED_SUMMARY     = _sum_cfg["scheduled"]

# token budget settings
SYSTEM_HEADER_BUDGET  = _sum_cfg.get("system_header_budget", 128)
SUMMARY_MAX_PERCENT   = _sum_cfg.get("summary_max_percent", 0.10)
KEEP_RECENT_PERCENT   = _sum_cfg.get("keep_recent_percent", 0.15)
CHARS_PER_TOKEN       = _sum_cfg.get("chars_per_token", 3)

# derived budgets (based on slot size minus static header)
_REMAINING_CONTEXT    = CONTEXT_PER_SLOT - SYSTEM_HEADER_BUDGET
SUMMARY_MAX_TOKENS    = int(_REMAINING_CONTEXT * SUMMARY_MAX_PERCENT)
KEEP_RECENT_TOKENS    = int(_REMAINING_CONTEXT * KEEP_RECENT_PERCENT)

# ==================================================
# Idle / Background Checks
# ==================================================
USER_IDLE_TIMEOUT   = _cfg["idle"]["timeout"]
IDLE_CHECK_INTERVAL = _cfg["idle"].get("check_interval", 120)

# ==================================================
# Default Sampling
# ==================================================
DEFAULT_TEMPERATURE       = _cfg["sampling"]["temperature"]
DEFAULT_TOP_P             = _cfg["sampling"]["top_p"]
DEFAULT_TOP_K             = _cfg["sampling"]["top_k"]
DEFAULT_MIN_P             = _cfg["sampling"]["min_p"]
DEFAULT_PRESENCE_PENALTY  = _cfg["sampling"]["presence_penalty"]
DEFAULT_MAX_TOKENS        = _cfg["sampling"]["max_tokens"]

# per-turn profiles (main._pick_profile) — merged over the defaults above
SAMPLING_PROFILES: dict[str, dict] = _cfg.get("sampling_profiles", {}) or {}
THINK_MIN_HEADROOM = _cfg.get("think_min_headroom", 2200)

# house clock — server runs UTC; the model is shown this zone
from zoneinfo import ZoneInfo as _ZoneInfo
TIMEZONE = _ZoneInfo(_cfg.get("timezone", "UTC"))

# shared rule block appended after every persona (core/slots.build_messages)
HOUSE_RULES: str = (_cfg.get("house_rules") or "").strip()

# ==================================================
# Security Levels (5 levels)
# ==================================================
class SecurityLevel:
    GUEST   = 0
    USER    = 1
    POWER   = 2
    TRUSTED = 3
    ADMIN   = 4

# ==================================================
# Users — build slot map and security from users.yaml
# ==================================================
SLOT_MAP:        dict[str, int]  = {}
USER_SECURITY:   dict[str, int]  = {}
USER_PASSWORDS:  dict[str, str]  = {}
USER_SUMMARIZE:  dict[str, bool] = {}
USER_WEB_ENABLED: dict[str, bool] = {}

for uid, udata in _users_cfg.items():
    SLOT_MAP[uid]        = udata["slot"]
    USER_SECURITY[uid]   = udata["security"]
    USER_PASSWORDS[uid]  = udata.get("password_hash", "")
    USER_SUMMARIZE[uid]  = udata.get("summarize", True)
    USER_WEB_ENABLED[uid] = udata.get("web_enabled", True)

# ==================================================
# Guest
# ==================================================
GUEST_ENABLED = _cfg.get("guest", {}).get("enabled", True)

# ==================================================
# Utility Lane
# ==================================================
# When enabled, background tasks (summarization, tool
# LLM calls, PII checks) run on the guest slot without
# blocking the requesting user's own slot.
# When disabled, tasks fall back to the requesting
# user's own slot with a brief lock.
# ==================================================
UTILITY_ENABLED = _cfg.get("utility", {}).get("enabled", True)

# safety check: utility enabled but guest slot missing
if UTILITY_ENABLED and "guest" not in SLOT_MAP:
    import warnings
    warnings.warn(
        "utility.enabled is true but 'guest' is not in users config. "
        "Falling back to user-slot summarization."
    )
    UTILITY_ENABLED = False

# ==================================================
# Module Permissions
# ==================================================
MODULE_PERMISSIONS: dict[str, int] = _cfg.get("module_permissions", {}) or {}

# ==================================================
# Module Priorities
# ==================================================
# Lower number = runs first within a phase. Default 50 if not listed.
MODULE_PRIORITIES: dict[str, int] = _cfg.get("module_priorities", {}) or {}

# ==================================================
# Disabled Features
# ==================================================
# Names matched against @register module names, @tool names,
# and scheduled-job source files (modules/<name>.py).
DISABLED: frozenset[str] = frozenset(_cfg.get("disabled", []) or [])

def is_disabled(name: str | None) -> bool:
    return bool(name) and name in DISABLED

# ==================================================
# Turn Log
# ==================================================
_turn_cfg = _cfg.get("turn_log", {}) or {}
TURN_LOG_ENABLED        = _turn_cfg.get("enabled", True)
TURN_LOG_PAYLOAD_DAYS   = _turn_cfg.get("payload_retention_days", 180)
TURN_LOG_MAX_DB_MB      = _turn_cfg.get("max_db_mb", 500)
TURN_LOG_RETENTION_CRON = _turn_cfg.get("retention_cron", "30 9 * * *")

# ==================================================
# Device Domain Permissions
# ==================================================
# Cumulative: security level N grants all domains listed at levels <= N.
DEVICE_EXCLUDE: list[str] = [p.lower() for p in (_cfg.get("device_exclude", []) or [])]

def device_excluded(state: dict) -> bool:
    eid  = state.get("entity_id", "").lower()
    name = state.get("attributes", {}).get("friendly_name", "").lower()
    return any(p in eid or p in name for p in DEVICE_EXCLUDE)

DEVICE_DOMAIN_PERMISSIONS: dict[int, list[str]] = {
    int(k): v
    for k, v in (_cfg.get("device_domain_permissions", {}) or {}).items()
}

# ==================================================
# Logging Config
# ==================================================
LOG_LEVEL  = _cfg.get("logging", {}).get("level", "INFO")
LOG_FORMAT = _cfg.get("logging", {}).get("format", "json")

# ==================================================
# Providers
# ==================================================
# Provider config is isolated — each provider reads
# its own config.yaml from its subdirectory under
# providers/. Nothing provider-specific lives here.
# ==================================================

# ==================================================
# LLM Launch Command
# ==================================================
def build_llm_cmd() -> list[str]:
    cmd = [
        str(LLAMA_SERVER),
        "--model",          str(MODEL_PATH),
        *(["--mmproj", str(PROJECTOR_PATH)] if PROJECTOR_PATH else []),
        "--host",           LLM_HOST,
        "--port",           str(LLM_PORT),
        "--n-gpu-layers",   str(GPU_LAYERS),
        "--parallel",       str(SLOT_COUNT),
        "--ctx-size",       str(CONTEXT_TOTAL),
        "--cache-type-k",   KV_CACHE_TYPE,
        "--cache-type-v",   KV_CACHE_TYPE,
    ]
    cmd += ["--jinja"]           # required for Qwen3.5 chat template + chat_template_kwargs
    cmd += ["-ub", "2048"]       # larger physical batch for faster prefill
    if FLASH_ATTN:
        cmd += ["--flash-attn", "on"]
    if MMPROJ_OFFLOAD and PROJECTOR_PATH:
        cmd.append("--mmproj-offload")
    if REASONING_OFF:
        cmd += ["--reasoning", "off"]
    if MLOCK:
        cmd.append("--mlock")
    return cmd

LLM_CMD = build_llm_cmd()