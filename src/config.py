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
PROJECTOR_PATH = Path(_cfg["paths"]["projector"])
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
DEFAULT_MIN_P             = _cfg["sampling"]["min_p"]
DEFAULT_TOP_K             = _cfg["sampling"]["top_k"]
DEFAULT_REPEAT_PENALTY    = _cfg["sampling"]["repeat_penalty"]
DEFAULT_FREQUENCY_PENALTY = _cfg["sampling"]["frequency_penalty"]
DEFAULT_MAX_TOKENS        = _cfg["sampling"]["max_tokens"]

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
SLOT_MAP: dict[str, int] = {}
USER_SECURITY: dict[str, int] = {}

for uid, udata in _users_cfg.items():
    SLOT_MAP[uid] = udata["slot"]
    USER_SECURITY[uid] = udata["security"]

# ==================================================
# Guest
# ==================================================
GUEST_ENABLED = _cfg.get("guest", {}).get("enabled", True)

# ==================================================
# Utility Lane
# ==================================================
# When enabled, background tasks (summarization, etc.)
# run on the dedicated utility slot without blocking
# the user. When disabled, tasks fall back to the
# requesting user's own slot with a brief lock.
# ==================================================
UTILITY_ENABLED = _cfg.get("utility", {}).get("enabled", True)

# safety check: utility toggled on but not in slot map
if UTILITY_ENABLED and "utility" not in SLOT_MAP:
    import warnings
    warnings.warn(
        "utility.enabled is true but 'utility' is not in users config. "
        "Falling back to user-slot summarization."
    )
    UTILITY_ENABLED = False

# ==================================================
# Module Permissions
# ==================================================
MODULE_PERMISSIONS: dict[str, int] = _cfg.get("module_permissions", {}) or {}

# ==================================================
# Logging Config
# ==================================================
LOG_LEVEL  = _cfg.get("logging", {}).get("level", "INFO")
LOG_FORMAT = _cfg.get("logging", {}).get("format", "json")

# ==================================================
# Providers (STT / TTS)
# ==================================================
_providers_cfg = _cfg.get("providers", {})

_stt_cfg = _providers_cfg.get("stt", {})
STT_ENABLED  = _stt_cfg.get("enabled", False)
STT_URL      = _stt_cfg.get("url", "http://localhost:8100")
STT_TIMEOUT  = _stt_cfg.get("timeout", 30)
STT_RETRIES  = _stt_cfg.get("retries", 1)

_tts_cfg = _providers_cfg.get("tts", {})
TTS_ENABLED  = _tts_cfg.get("enabled", False)
TTS_URL      = _tts_cfg.get("url", "http://localhost:8200")
TTS_TIMEOUT  = _tts_cfg.get("timeout", 30)
TTS_RETRIES  = _tts_cfg.get("retries", 0)

# ==================================================
# LLM Launch Command
# ==================================================
def build_llm_cmd() -> list[str]:
    cmd = [
        str(LLAMA_SERVER),
        "--model",          str(MODEL_PATH),
        "--mmproj",         str(PROJECTOR_PATH),
        "--host",           LLM_HOST,
        "--port",           str(LLM_PORT),
        "--n-gpu-layers",   str(GPU_LAYERS),
        "--parallel",       str(SLOT_COUNT),
        "--ctx-size",       str(CONTEXT_TOTAL),
        "--cache-type-k",   KV_CACHE_TYPE,
        "--cache-type-v",   KV_CACHE_TYPE,
    ]
    if FLASH_ATTN:
        cmd.append("--flash-attn")
    if MLOCK:
        cmd.append("--mlock")
    return cmd

LLM_CMD = build_llm_cmd()