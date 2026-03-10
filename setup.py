#!/usr/bin/env python3
# setup.py
#
# Author:  Logicish
# Company: Logic-Ish Designs
# Date:    2/26/2026
#
# ==================================================
# Interactive installer and configurator for p-lanes.
# Detects if system is already installed -- offers
# configure-only mode if so. Generates config.yaml
# and creates required directories and systemd service.
#
# Run with:
#   python3 setup.py
#
# Knows about: config.yaml structure.
# ==================================================

# ==================================================
# Imports
# ==================================================
import os
import sys
import shutil
import subprocess
from pathlib import Path

import yaml

# ==================================================
# Constants
# ==================================================

INSTALL_DIR     = Path(__file__).parent.resolve()
CONFIG_PATH     = INSTALL_DIR / "src" / "config.yaml"
USERS_PATH      = INSTALL_DIR / "src" / "users.yaml"
SERVICE_NAME    = "p-lanes"
SERVICE_FILE    = Path(f"/etc/systemd/system/{SERVICE_NAME}.service")

DEFAULT_CONFIG = {
    "brain": {
        "name": "Brain",
        "description": "Brain in a box. Local, private, discreet.",
    },
    "paths": {
        "log_file": "/var/log/p-lanes/p-lanes.log",
        "user_data_root": "/var/lib/p-lanes/users",
        "model": "/var/lib/models/model.gguf",
        "projector": "/var/lib/models/mmproj.gguf",
        "llama_server": "/opt/llama.cpp/build/bin/llama-server",
    },
    "network": {
        "llm_host": "localhost",
        "llm_port": 8080,
        "brain_host": "0.0.0.0",
        "brain_port": 7860,
    },
    "llm": {
        "timeout": 60,
        "startup_timeout": 90,
        "gpu_layers": 99,
        "flash_attn": True,
        "mlock": True,
        "kv_cache_type": "q8_0",
        "recovery": {
            "max_retries": 5,
            "initial_wait": 5,
            "max_wait": 120,
        },
    },
    "slots": {
        "count": 5,
        "ctx_total": 61440,
    },
    "summarization": {
        "threshold_warn": 0.70,
        "threshold_crit": 0.80,
        "lock_wait": 6,
        "system_header_budget": 128,
        "summary_max_percent": 0.10,
        "keep_recent_percent": 0.15,
        "chars_per_token": 3,
        "scheduled": {
            "enabled": True,
            "cron": "0 2 * * *",
            "restart_llm": True,
        },
    },
    "idle": {
        "timeout": 300,
        "check_interval": 120,
    },
    "sampling": {
        "temperature": 0.7,
        "min_p": 0.05,
        "top_k": 40,
        "repeat_penalty": 1.1,
        "frequency_penalty": 0.0,
        "max_tokens": 512,
    },
    "guest": {"enabled": True},
    "utility": {"enabled": True},
    "module_permissions": {},
    "logging": {
        "level": "INFO",
        "format": "json",
    },
}

SECURITY_LABELS = {
    0: "GUEST",
    1: "USER",
    2: "POWER",
    3: "TRUSTED",
    4: "ADMIN",
}
SECURITY_REVERSE = {v: k for k, v in SECURITY_LABELS.items()}

# ==================================================
# Helpers
# ==================================================

def banner():
    print()
    print("=" * 50)
    print("  p-lanes Setup -- Logic-Ish Designs")
    print("=" * 50)
    print()


def ask(prompt: str, default: str = "") -> str:
    suffix = f" [{default}]" if default else ""
    result = input(f"  {prompt}{suffix}: ").strip()
    return result if result else default


def ask_int(prompt: str, default: int) -> int:
    while True:
        raw = ask(prompt, str(default))
        try:
            return int(raw)
        except ValueError:
            print("    -> Please enter a number.")


def ask_float(prompt: str, default: float) -> float:
    while True:
        raw = ask(prompt, str(default))
        try:
            return float(raw)
        except ValueError:
            print("    -> Please enter a number.")


def ask_bool(prompt: str, default: bool = True) -> bool:
    suffix = " [Y/n]" if default else " [y/N]"
    result = input(f"  {prompt}{suffix}: ").strip().lower()
    if not result:
        return default
    return result in ("y", "yes", "true", "1")


def ask_path(prompt: str, default: str) -> str:
    path = ask(prompt, default)
    return path


def is_installed() -> bool:
    return CONFIG_PATH.exists()


# ==================================================
# Config Wizard
# ==================================================

def configure() -> dict:
    cfg = DEFAULT_CONFIG.copy()

    # load existing config if reconfiguring
    if CONFIG_PATH.exists():
        print("  Existing config found. Loading as defaults.\n")
        with open(CONFIG_PATH) as f:
            existing = yaml.safe_load(f) or {}
        _deep_update(cfg, existing)
    # load existing users if reconfiguring
    if USERS_PATH.exists():
        with open(USERS_PATH) as f:
            existing_users = yaml.safe_load(f) or {}
        cfg["_users"] = existing_users.get("users", {})

    # --- identity ---
    print("-- Identity --")
    cfg["brain"]["name"] = ask("Assistant name", cfg["brain"]["name"])
    cfg["brain"]["description"] = ask("Description", cfg["brain"]["description"])
    print()

    # --- paths ---
    print("-- Paths --")
    cfg["paths"]["model"] = ask_path("Model path", cfg["paths"]["model"])
    cfg["paths"]["projector"] = ask_path("Projector path", cfg["paths"]["projector"])
    cfg["paths"]["llama_server"] = ask_path("llama-server path", cfg["paths"]["llama_server"])
    cfg["paths"]["log_file"] = ask_path("Log file", cfg["paths"]["log_file"])
    cfg["paths"]["user_data_root"] = ask_path("User data dir", cfg["paths"]["user_data_root"])
    print()

    # --- network ---
    print("-- Network --")
    cfg["network"]["brain_port"] = ask_int("p-lanes port", cfg["network"]["brain_port"])
    cfg["network"]["llm_port"] = ask_int("LLM port", cfg["network"]["llm_port"])
    print()

    # --- utility lane ---
    print("-- Utility Lane --")
    utility_enabled = ask_bool(
        "Enable dedicated utility lane? (async summarization, no user blocking)",
        cfg["utility"]["enabled"],
    )
    cfg["utility"]["enabled"] = utility_enabled
    if not utility_enabled:
        print("    -> Utility lane disabled. Summarization will run on user slots.")
    print()

    # --- slots ---
    print("-- Slots / KV Cache --")
    if utility_enabled:
        cfg["slots"]["count"] = ask_int("Total slots (users + utility)", cfg["slots"]["count"])
    else:
        # no utility slot -- all slots are user slots
        cfg["slots"]["count"] = ask_int("Total slots (users only, no utility)", cfg["slots"]["count"])
    cfg["slots"]["ctx_total"] = ask_int("Total context tokens", cfg["slots"]["ctx_total"])
    print()

    # --- users ---
    print("-- Users --")
    print("  Define users and their slot assignments.")
    print("  Security levels: GUEST=0, USER=1, POWER=2, TRUSTED=3, ADMIN=4")
    print()

    users = {}
    slot_idx = 0
    # reserve last slot for utility if enabled
    max_user_slots = cfg["slots"]["count"] - 1 if utility_enabled else cfg["slots"]["count"]
    # seed from existing users.yaml when reconfiguring (exclude system accounts)
    _existing_users = {k: v for k, v in cfg.get("_users", {}).items()
                       if k not in ("guest", "utility")}
    if _existing_users:
        print("  Existing users found (from users.yaml):")
        for uid, udata in sorted(_existing_users.items(), key=lambda x: x[1]["slot"]):
            level = SECURITY_LABELS.get(udata["security"], "USER")
            print(f"    {uid}: slot {udata['slot']}, {level}")
        print()
        if ask_bool("  Keep existing users?", True):
            users = dict(_existing_users)
            slot_idx = max((v["slot"] for v in users.values()), default=-1) + 1

    while slot_idx < max_user_slots:
        name = ask(f"  User {slot_idx} name (blank to stop)", "")
        if not name:
            break
        name = name.lower().strip()
        sec_str = ask(f"    Security level for '{name}'", "USER").upper()
        sec_level = SECURITY_REVERSE.get(sec_str, 1)
        users[name] = {"slot": slot_idx, "security": sec_level}
        print(f"    -> {name}: slot {slot_idx}, {SECURITY_LABELS[sec_level]}")
        slot_idx += 1

    # guest account
    print()
    guest_enabled = ask_bool("Enable guest account?", cfg["guest"]["enabled"])
    cfg["guest"]["enabled"] = guest_enabled

    if guest_enabled and "guest" not in users:
        if slot_idx < max_user_slots:
            users["guest"] = {"slot": slot_idx, "security": 0}
            print(f"    -> guest: slot {slot_idx}, GUEST")
            slot_idx += 1
        else:
            print("    WARNING: No slots available for guest -- skipping")
            cfg["guest"]["enabled"] = False

    # utility slot -- last slot, only if enabled
    if utility_enabled:
        utility_slot = cfg["slots"]["count"] - 1
        users["utility"] = {"slot": utility_slot, "security": 4}
        print(f"    -> utility: slot {utility_slot}, ADMIN")

    cfg["_users"] = users   # carried separately; written to users.yaml, not config.yaml
    print()

    # --- summarization ---
    print("-- Summarization --")
    cfg["summarization"]["threshold_warn"] = ask_float(
        "Warning threshold (0-1)", cfg["summarization"]["threshold_warn"])
    cfg["summarization"]["threshold_crit"] = ask_float(
        "Critical threshold (0-1)", cfg["summarization"]["threshold_crit"])
    cfg["summarization"]["system_header_budget"] = ask_int(
        "System header token budget (static)",
        cfg["summarization"]["system_header_budget"])
    cfg["summarization"]["summary_max_percent"] = ask_float(
        "Summary max (% of remaining context)",
        cfg["summarization"]["summary_max_percent"])
    cfg["summarization"]["keep_recent_percent"] = ask_float(
        "Recent messages (% of remaining context)",
        cfg["summarization"]["keep_recent_percent"])

    # show the user what this means for their slot size
    slot_ctx = cfg["slots"]["ctx_total"] // cfg["slots"]["count"]
    remaining = slot_ctx - cfg["summarization"]["system_header_budget"]
    summary_cap = int(remaining * cfg["summarization"]["summary_max_percent"])
    recent_cap = int(remaining * cfg["summarization"]["keep_recent_percent"])
    headroom = int(slot_ctx * cfg["summarization"]["threshold_crit"]) - (
        cfg["summarization"]["system_header_budget"] + summary_cap + recent_cap)
    print()
    print(f"    At {slot_ctx} tokens/slot:")
    print(f"      Summary cap:  ~{summary_cap} tokens")
    print(f"      Recent keep:  ~{recent_cap} tokens")
    print(f"      Headroom:     ~{headroom} tokens until next summarize")
    if not utility_enabled:
        print(f"      Mode:         In-place (user slot locked during summarization)")
    else:
        print(f"      Mode:         Async (utility lane, no user blocking)")
    print()

    sched_enabled = ask_bool("Enable scheduled daily summary?",
                              cfg["summarization"]["scheduled"]["enabled"])
    cfg["summarization"]["scheduled"]["enabled"] = sched_enabled
    if sched_enabled:
        cfg["summarization"]["scheduled"]["cron"] = ask(
            "Cron expression (M H * * *)",
            cfg["summarization"]["scheduled"]["cron"])
        cfg["summarization"]["scheduled"]["restart_llm"] = ask_bool(
            "Restart LLM after scheduled summary?",
            cfg["summarization"]["scheduled"]["restart_llm"])
    print()

    # --- idle / background ---
    print("-- Idle / Background Checks --")
    cfg["idle"]["timeout"] = ask_int(
        "Idle timeout (seconds)", cfg["idle"]["timeout"])
    cfg["idle"]["check_interval"] = ask_int(
        "Background check interval (seconds)", cfg["idle"]["check_interval"])
    print()

    # --- logging ---
    print("-- Logging --")
    cfg["logging"]["level"] = ask("Log level (DEBUG/INFO/WARNING/ERROR)",
                                   cfg["logging"]["level"]).upper()
    fmt = ask("Log format (json/console)", cfg["logging"]["format"]).lower()
    cfg["logging"]["format"] = fmt if fmt in ("json", "console") else "json"
    print()

    return cfg


# ==================================================
# Install
# ==================================================

def install(cfg: dict):
    print("-- Installing --")

    # pull users out — they go to users.yaml, not config.yaml
    users = cfg.pop("_users", {})

    # create directories
    dirs = [
        Path(cfg["paths"]["log_file"]).parent,
        Path(cfg["paths"]["user_data_root"]),
    ]
    for d in dirs:
        d.mkdir(parents=True, exist_ok=True)
        print(f"  OK Created {d}")

    # create user data directories
    for uid in users:
        user_dir = Path(cfg["paths"]["user_data_root"]) / uid
        user_dir.mkdir(parents=True, exist_ok=True)
        print(f"  OK Created user dir: {user_dir}")

    # write config.yaml (no users block)
    with open(CONFIG_PATH, "w") as f:
        yaml.dump(cfg, f, default_flow_style=False, sort_keys=False)
    print(f"  OK Config written to {CONFIG_PATH}")

    # write users.yaml (gitignored)
    users_doc = (
        "# users.yaml\n"
        "#\n"
        "# THIS FILE IS GITIGNORED — do not commit.\n"
        "# Contains real user names and slot assignments.\n"
        "# Generated by setup.py — edit manually or re-run setup.py to reconfigure.\n"
        "#\n"
        "# Security levels: GUEST=0, USER=1, POWER=2, TRUSTED=3, ADMIN=4\n\n"
    )
    with open(USERS_PATH, "w") as f:
        f.write(users_doc)
        yaml.dump({"users": users}, f, default_flow_style=False, sort_keys=False)
    print(f"  OK Users written to {USERS_PATH} (gitignored)")

    # systemd service
    if ask_bool("Create systemd service?", True):
        _create_service(cfg)

    print()
    print("  OK Installation complete!")
    print(f"    Start with: systemctl start {SERVICE_NAME}")
    print(f"    Or manually: cd src && uvicorn main:app --host 0.0.0.0 --port {cfg['network']['brain_port']}")
    print()


def _create_service(cfg: dict):
    python_path = shutil.which("python3") or sys.executable
    uvicorn_path = shutil.which("uvicorn")

    if not uvicorn_path:
        print("  WARNING uvicorn not found in PATH -- skipping service creation")
        return

    port = cfg["network"]["brain_port"]
    host = cfg["network"]["brain_host"]

    service_content = f"""[Unit]
Description=p-lanes -- Local AI Assistant
After=network.target

[Service]
Type=simple
WorkingDirectory={INSTALL_DIR / "src"}
ExecStart={uvicorn_path} main:app --host {host} --port {port}
Restart=on-failure
RestartSec=5
StandardOutput=journal
StandardError=journal

[Install]
WantedBy=multi-user.target
"""
    try:
        SERVICE_FILE.write_text(service_content)
        subprocess.run(["systemctl", "daemon-reload"], check=True, capture_output=True)
        print(f"  OK Systemd service created: {SERVICE_FILE}")
    except PermissionError:
        print(f"  WARNING Permission denied writing {SERVICE_FILE} -- run as root or use sudo")
    except Exception as e:
        print(f"  WARNING Failed to create service: {e}")


# ==================================================
# Deep Update Helper
# ==================================================

def _deep_update(base: dict, override: dict):
    for key, val in override.items():
        if key in base and isinstance(base[key], dict) and isinstance(val, dict):
            _deep_update(base[key], val)
        else:
            base[key] = val


# ==================================================
# Main
# ==================================================

def main():
    banner()

    if is_installed():
        print("  Existing installation detected.\n")
        choice = ask("(R)econfigure or (F)resh install?", "R").upper()
        if choice == "F":
            print("  Starting fresh install...\n")
        else:
            print("  Reconfiguring...\n")
    else:
        print("  No existing installation found. Starting fresh.\n")

    cfg = configure()

    print()
    print("  Configuration complete. Review:")
    print(f"    Brain name:   {cfg['brain']['name']}")
    print(f"    Users:        {', '.join(cfg['_users'].keys())}")
    print(f"    Slots:        {cfg['slots']['count']}")
    print(f"    Context:      {cfg['slots']['ctx_total']} tokens")
    print(f"    Port:         {cfg['network']['brain_port']}")
    print(f"    Utility lane: {'enabled' if cfg['utility']['enabled'] else 'disabled (user-slot fallback)'}")
    print(f"    Guest:        {'enabled' if cfg['guest']['enabled'] else 'disabled'}")
    print()

    if ask_bool("Proceed with installation?", True):
        install(cfg)
    else:
        users = cfg.pop("_users", {})
        with open(CONFIG_PATH, "w") as f:
            yaml.dump(cfg, f, default_flow_style=False, sort_keys=False)
        users_doc = (
            "# users.yaml\n#\n# THIS FILE IS GITIGNORED — do not commit.\n\n"
        )
        with open(USERS_PATH, "w") as f:
            f.write(users_doc)
            yaml.dump({"users": users}, f, default_flow_style=False, sort_keys=False)
        print(f"  Config saved to {CONFIG_PATH} (no install performed)")
        print(f"  Users saved to {USERS_PATH} (gitignored)")
        print()


if __name__ == "__main__":
    main()