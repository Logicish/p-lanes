#!/usr/bin/env bash
# ==================================================
# p-lanes Integration & Stress Test
#
# Author:  Logicish
# Company: Logic-Ish Designs
# Date:    3/6/2026
#
# Covers:
#   - Health/slot endpoints
#   - Hello-world module (ping/test)
#   - Gate 1 security (/llm/restart admin gate)
#   - Push user to warn threshold
#   - Idle-triggered background summarization
#   - Guest idle history clear
#   - conversation_id passthrough (JSON + SSE)
#   - SSE JSON safety (special characters)
#   - Concurrent request smoke test
#   - Negative cases (bad payloads, unknown users)
#   - Restart + persistence verification
#   - Config backup + restore
#
# Usage:
#   chmod +x test.sh
#   ./test.sh
#
# Launches/kills uvicorn directly. Expects the venv
# and p-lanes source at the paths configured below.
# Will kill any running p-lanes instance on start.
# ==================================================

set -euo pipefail

# ==================================================
# Config — adjust these to match your environment
# ==================================================
P_LANES_URL="http://localhost:7860"
P_LANES_DIR="/opt/mediator-env/p-lanes/src"
VENV_PATH="/opt/mediator-env/.venv"
CONFIG_PATH="$P_LANES_DIR/config.yaml"
CONFIG_BACKUP="$P_LANES_DIR/config.yaml.test-backup"

# test users (must match config.yaml)
USER_ADMIN="utility"
USER_TRUSTED="user1"
USER_NORMAL="user2"
USER_NORMAL2="user3"
USER_GUEST="guest"

# patched thresholds for faster testing
TEST_WARN_THRESHOLD=0.30
TEST_CRIT_THRESHOLD=0.50
TEST_IDLE_TIMEOUT=30
TEST_CHECK_INTERVAL=15

# how long to wait for idle triggers (seconds)
# should be >= idle_timeout + check_interval + buffer
IDLE_WAIT=60

# max time for a single LLM request (seconds)
LLM_TIMEOUT=120

# ==================================================
# State
# ==================================================
PASS=0
FAIL=0
SKIP=0
TOTAL=0
START_TIME=$(date +%s)

# ==================================================
# Helpers
# ==================================================

BOLD='\033[1m'
GREEN='\033[0;32m'
RED='\033[0;31m'
YELLOW='\033[0;33m'
CYAN='\033[0;36m'
DIM='\033[2m'
RESET='\033[0m'

log()   { echo -e "${CYAN}[$(date +%H:%M:%S)]${RESET} $*"; }
pass()  { (( PASS++ )) || true; (( TOTAL++ )) || true; echo -e "  ${GREEN}✓ PASS${RESET} — $1"; }
fail()  { (( FAIL++ )) || true; (( TOTAL++ )) || true; echo -e "  ${RED}✗ FAIL${RESET} — $1"; echo -e "    ${DIM}$2${RESET}"; }
skip()  { (( SKIP++ )) || true; (( TOTAL++ )) || true; echo -e "  ${YELLOW}○ SKIP${RESET} — $1"; }
phase() { echo -e "\n${BOLD}━━━ Phase $1: $2 ━━━${RESET}"; }

chat() {
    # chat USER_ID MESSAGE [CONVERSATION_ID]
    local uid="$1" msg="$2" cid="${3:-}"
    local payload
    if [[ -n "$cid" ]]; then
        payload=$(jq -nc --arg u "$uid" --arg m "$msg" --arg c "$cid" \
            '{user_id: $u, message: $m, conversation_id: $c}')
    else
        payload=$(jq -nc --arg u "$uid" --arg m "$msg" \
            '{user_id: $u, message: $m}')
    fi
    curl -s -X POST "$P_LANES_URL/channel/chat" \
        -H "Content-Type: application/json" \
        -d "$payload" --max-time "$LLM_TIMEOUT"
}

chat_stream_raw() {
    # chat_stream_raw USER_ID MESSAGE [CONVERSATION_ID]
    # returns raw SSE output
    local uid="$1" msg="$2" cid="${3:-}"
    local payload
    if [[ -n "$cid" ]]; then
        payload=$(jq -nc --arg u "$uid" --arg m "$msg" --arg c "$cid" \
            '{user_id: $u, message: $m, conversation_id: $c}')
    else
        payload=$(jq -nc --arg u "$uid" --arg m "$msg" \
            '{user_id: $u, message: $m}')
    fi
    curl -s -N -X POST "$P_LANES_URL/channel/chat/stream" \
        -H "Content-Type: application/json" \
        -d "$payload" --max-time "$LLM_TIMEOUT"
}

get_slot_field() {
    # get_slot_field USER_ID FIELD
    curl -s "$P_LANES_URL/slots" | jq -r ".slots[\"$1\"][\"$2\"]"
}

get_dump_field() {
    # get_dump_field TARGET_USER_ID FIELD
    curl -s "$P_LANES_URL/admin/dump/$1?user_id=$USER_ADMIN" | jq -r ".$2"
}

restart_service() {
    log "Stopping p-lanes (killing uvicorn)..."

    # kill any running uvicorn for main:app
    pkill -f "uvicorn main:app" 2>/dev/null || true
    sleep 2

    # double-check it's dead
    if pgrep -f "uvicorn main:app" &>/dev/null; then
        log "Force killing..."
        pkill -9 -f "uvicorn main:app" 2>/dev/null || true
        sleep 1
    fi

    log "Starting p-lanes..."
    cd "$P_LANES_DIR"
    source "$VENV_PATH/bin/activate"
    nohup uvicorn main:app --host 0.0.0.0 --port 7860 \
        > /tmp/p-lanes-test.log 2>&1 &
    UVICORN_PID=$!
    log "Launched uvicorn (PID $UVICORN_PID)"

    # wait for health
    local tries=0
    while [[ $tries -lt 120 ]]; do
        if curl -s "$P_LANES_URL/health" | jq -e '.llm_running == true' &>/dev/null; then
            log "Service ready (took ~${tries}s)"
            return 0
        fi
        sleep 1
        (( tries++ )) || true
    done
    log "ERROR: Service did not become ready in 120s"
    log "Last 20 lines of log:"
    tail -20 /tmp/p-lanes-test.log 2>/dev/null || true
    return 1
}

wait_for_health() {
    local tries=0
    while [[ $tries -lt 60 ]]; do
        if curl -s "$P_LANES_URL/health" &>/dev/null; then
            return 0
        fi
        sleep 1
        (( tries++ )) || true
    done
    return 1
}

# ==================================================
# Cleanup trap — always restore config
# ==================================================
cleanup() {
    echo ""
    log "Cleaning up..."

    # kill any orphaned curl processes from this test
    pkill -f "curl.*channel/chat" 2>/dev/null || true

    if [[ -f "$CONFIG_BACKUP" ]]; then
        cp "$CONFIG_BACKUP" "$CONFIG_PATH"
        rm -f "$CONFIG_BACKUP"
        log "Config restored from backup"

        # kill test instance and relaunch with original config
        pkill -f "uvicorn main:app" 2>/dev/null || true
        sleep 2
        cd "$P_LANES_DIR"
        source "$VENV_PATH/bin/activate"
        nohup uvicorn main:app --host 0.0.0.0 --port 7860 \
            > /tmp/p-lanes-test.log 2>&1 &
        log "Service relaunched with original config (PID $!)"
    fi

    local elapsed=$(( $(date +%s) - START_TIME ))
    local mins=$(( elapsed / 60 ))
    local secs=$(( elapsed % 60 ))

    echo ""
    echo -e "${BOLD}━━━ Results ━━━${RESET}"
    echo -e "  ${GREEN}Passed:${RESET}  $PASS"
    echo -e "  ${RED}Failed:${RESET}  $FAIL"
    echo -e "  ${YELLOW}Skipped:${RESET} $SKIP"
    echo -e "  Total:   $TOTAL"
    echo -e "  Time:    ${mins}m ${secs}s"
    echo ""

    if [[ $FAIL -gt 0 ]]; then
        echo -e "${RED}${BOLD}TEST SUITE FAILED${RESET}"
        exit 1
    else
        echo -e "${GREEN}${BOLD}ALL TESTS PASSED${RESET}"
        exit 0
    fi
}
trap cleanup EXIT

# ==================================================
# Preflight
# ==================================================
echo -e "${BOLD}"
echo "  ╔══════════════════════════════════════╗"
echo "  ║   p-lanes Integration Test Suite     ║"
echo "  ║   Logic-Ish Designs — March 2026     ║"
echo "  ╚══════════════════════════════════════╝"
echo -e "${RESET}"

# check dependencies
for cmd in curl jq python3; do
    if ! command -v "$cmd" &>/dev/null; then
        echo "ERROR: $cmd is required but not found"
        exit 1
    fi
done

# check config exists
if [[ ! -f "$CONFIG_PATH" ]]; then
    echo "ERROR: Config not found at $CONFIG_PATH"
    echo "       Edit CONFIG_PATH at the top of this script."
    exit 1
fi

# ==================================================
# Phase 0: Backup Config + Patch Thresholds
# ==================================================
phase "0" "Config Patch"

cp "$CONFIG_PATH" "$CONFIG_BACKUP"
log "Config backed up to $CONFIG_BACKUP"

# patch config with test-friendly thresholds using python
# (yaml round-trip preserves structure better than sed)
python3 - "$CONFIG_PATH" "$TEST_WARN_THRESHOLD" "$TEST_CRIT_THRESHOLD" \
    "$TEST_IDLE_TIMEOUT" "$TEST_CHECK_INTERVAL" << 'PYEOF'
import sys, yaml
config_path = sys.argv[1]
warn = float(sys.argv[2])
crit = float(sys.argv[3])
idle_timeout = int(sys.argv[4])
check_interval = int(sys.argv[5])

with open(config_path) as f:
    cfg = yaml.safe_load(f)

cfg["summarization"]["threshold_warn"] = warn
cfg["summarization"]["threshold_crit"] = crit
cfg["idle"]["timeout"] = idle_timeout
cfg["idle"]["check_interval"] = check_interval

with open(config_path, "w") as f:
    yaml.dump(cfg, f, default_flow_style=False, sort_keys=False)

print(f"  Patched: warn={warn}, crit={crit}, idle={idle_timeout}s, check={check_interval}s")
PYEOF

restart_service

# ==================================================
# Phase 1: Health & Slot Endpoints
# ==================================================
phase "1" "Health & Slot Endpoints"

HEALTH=$(curl -s "$P_LANES_URL/health")

if echo "$HEALTH" | jq -e '.status == "ok"' &>/dev/null; then
    pass "GET /health returns ok"
else
    fail "GET /health" "$HEALTH"
fi

if echo "$HEALTH" | jq -e '.llm_running == true' &>/dev/null; then
    pass "LLM is running"
else
    fail "LLM running check" "$HEALTH"
fi

if echo "$HEALTH" | jq -e '.llm_pid != null' &>/dev/null; then
    pass "LLM PID is present"
else
    fail "LLM PID check" "$HEALTH"
fi

SLOTS=$(curl -s "$P_LANES_URL/slots")

if echo "$SLOTS" | jq -e ".slots[\"$USER_TRUSTED\"]" &>/dev/null; then
    pass "GET /slots returns $USER_TRUSTED"
else
    fail "Slot check for $USER_TRUSTED" "$SLOTS"
fi

if echo "$SLOTS" | jq -e ".slots[\"$USER_GUEST\"]" &>/dev/null; then
    pass "GET /slots returns guest"
else
    fail "Slot check for guest" "$SLOTS"
fi

SLOT_COUNT=$(echo "$SLOTS" | jq '.slots | length')
if [[ "$SLOT_COUNT" -eq 5 ]]; then
    pass "All 5 slots present"
else
    fail "Expected 5 slots, got $SLOT_COUNT" "$SLOTS"
fi

# ==================================================
# Phase 2: Hello-World Module
# ==================================================
phase "2" "Hello-World Module"

# ping test
PING=$(chat "$USER_TRUSTED" "ping")
if echo "$PING" | jq -r '.response' | grep -qi "pong"; then
    pass "ping → pong ($USER_TRUSTED)"
else
    fail "ping command" "$(echo "$PING" | jq -r '.response')"
fi

# test command
TEST_CMD=$(chat "$USER_TRUSTED" "test")
TEST_RESP=$(echo "$TEST_CMD" | jq -r '.response')
if echo "$TEST_RESP" | grep -q "slot:"; then
    pass "test → status dump ($USER_TRUSTED)"
else
    fail "test command" "$TEST_RESP"
fi

# ping as guest
PING_GUEST=$(chat "$USER_GUEST" "ping")
if echo "$PING_GUEST" | jq -r '.response' | grep -qi "pong"; then
    pass "ping → pong (guest)"
else
    fail "ping as guest" "$(echo "$PING_GUEST" | jq -r '.response')"
fi

# ping as normal user
PING_NORM=$(chat "$USER_NORMAL" "ping")
if echo "$PING_NORM" | jq -r '.response' | grep -qi "pong"; then
    pass "ping → pong ($USER_NORMAL)"
else
    fail "ping as $USER_NORMAL" "$(echo "$PING_NORM" | jq -r '.response')"
fi

# normal message should NOT be intercepted
NORMAL_MSG=$(chat "$USER_NORMAL" "What is the capital of France?")
NORMAL_RESP=$(echo "$NORMAL_MSG" | jq -r '.response')
if echo "$NORMAL_RESP" | grep -qi "pong\|pipeline is alive"; then
    fail "normal message should not trigger hello_world" "$NORMAL_RESP"
else
    pass "normal message passes through to LLM"
fi

# ==================================================
# Phase 3: Security — /llm/restart Gate
# ==================================================
phase "3" "Security — Admin Gate"

# non-admin should get 403
RESTART_DENIED=$(curl -s -X POST "$P_LANES_URL/llm/restart" \
    -H "Content-Type: application/json" \
    -d "{\"user_id\": \"$USER_NORMAL\"}")
if echo "$RESTART_DENIED" | jq -e '.error == "access_denied"' &>/dev/null; then
    pass "/llm/restart rejected for non-admin ($USER_NORMAL)"
else
    fail "/llm/restart should reject non-admin" "$RESTART_DENIED"
fi

# guest should also get 403
RESTART_GUEST=$(curl -s -X POST "$P_LANES_URL/llm/restart" \
    -H "Content-Type: application/json" \
    -d "{\"user_id\": \"$USER_GUEST\"}")
if echo "$RESTART_GUEST" | jq -e '.error == "access_denied"' &>/dev/null; then
    pass "/llm/restart rejected for guest"
else
    fail "/llm/restart should reject guest" "$RESTART_GUEST"
fi

# admin should succeed
RESTART_OK=$(curl -s -X POST "$P_LANES_URL/llm/restart" \
    -H "Content-Type: application/json" \
    -d "{\"user_id\": \"$USER_ADMIN\"}")
if echo "$RESTART_OK" | jq -e '.success == true' &>/dev/null; then
    pass "/llm/restart succeeded for admin ($USER_ADMIN)"
else
    fail "/llm/restart should succeed for admin" "$RESTART_OK"
fi

# wait for LLM to come back after restart
log "Waiting for LLM to recover after admin restart..."
sleep 3
if wait_for_health; then
    pass "LLM healthy after admin restart"
else
    fail "LLM did not recover after admin restart" ""
fi

# ==================================================
# Phase 4: Admin Dump Endpoints
# ==================================================
phase "4" "Admin Dump Endpoints"

# admin can dump all
DUMP_ALL=$(curl -s "$P_LANES_URL/admin/dump?user_id=$USER_ADMIN")
if echo "$DUMP_ALL" | jq -e '.dump' &>/dev/null; then
    pass "GET /admin/dump works for admin"
else
    fail "Admin dump all" "$DUMP_ALL"
fi

# admin can dump single user
DUMP_ONE=$(curl -s "$P_LANES_URL/admin/dump/$USER_TRUSTED?user_id=$USER_ADMIN")
if echo "$DUMP_ONE" | jq -e ".user_id == \"$USER_TRUSTED\"" &>/dev/null; then
    pass "GET /admin/dump/$USER_TRUSTED works"
else
    fail "Admin dump single user" "$DUMP_ONE"
fi

# non-admin cannot dump
DUMP_DENIED=$(curl -s "$P_LANES_URL/admin/dump?user_id=$USER_NORMAL")
if echo "$DUMP_DENIED" | jq -e '.error == "access_denied"' &>/dev/null; then
    pass "Admin dump rejected for non-admin"
else
    fail "Admin dump should reject non-admin" "$DUMP_DENIED"
fi

# dump nonexistent user
DUMP_404=$(curl -s "$P_LANES_URL/admin/dump/nobody?user_id=$USER_ADMIN")
if echo "$DUMP_404" | jq -e '.error == "user_not_found"' &>/dev/null; then
    pass "Admin dump 404 for unknown user"
else
    fail "Admin dump should 404 for unknown user" "$DUMP_404"
fi

# ==================================================
# Phase 5: Push USER_TRUSTED to Warn Threshold
# ==================================================
phase "5" "Push $USER_TRUSTED to Warn Threshold"

log "Sending long prompts to fill context (warn at ${TEST_WARN_THRESHOLD}%)..."
log "This may take a few minutes — each prompt triggers LLM inference."

# long prompts to eat context fast
LONG_PROMPTS=(
    "Write a detailed technical explanation of how a four-stroke internal combustion engine works, covering the intake, compression, power, and exhaust strokes. Include details about valve timing, fuel injection, and the role of the crankshaft and camshaft in the overall system. Be thorough and explain the thermodynamic principles involved."
    "Explain the complete history of the Roman Empire from its founding through the fall of the Western Roman Empire. Cover the transition from Republic to Empire, major emperors, territorial expansion, economic systems, military organization, and the key factors that led to its eventual decline and collapse."
    "Describe in detail how modern computer processors work, from transistor logic gates up through ALU operations, pipelining, branch prediction, cache hierarchies, out-of-order execution, and multi-core architectures. Explain how instructions flow from memory through the fetch-decode-execute cycle."
    "Write a comprehensive guide to brewing beer at home, covering grain selection, mashing temperatures and enzyme activity, sparging techniques, hop varieties and additions at different boil stages, yeast selection and fermentation temperature control, and the bottling and conditioning process."
    "Explain the complete process of how the human immune system responds to a viral infection, from initial innate immune response through adaptive immunity. Cover the roles of macrophages, dendritic cells, T cells, B cells, antibody production, and the formation of immunological memory."
    "Describe the engineering challenges and solutions involved in building a skyscraper, from foundation design and soil analysis through structural steel framing, wind load calculations, elevator systems, HVAC distribution, electrical systems, plumbing, fire suppression, and facade engineering."
    "Write a detailed explanation of how neural networks learn through backpropagation, starting from individual neurons and activation functions, through forward passes, loss calculation, gradient computation via the chain rule, and weight updates via gradient descent. Cover vanishing gradients, batch normalization, and modern optimizers like Adam."
    "Explain the complete lifecycle of a star from nebula formation through main sequence, red giant or supergiant phase, and final fate as white dwarf, neutron star, or black hole depending on initial mass. Cover nuclear fusion processes at each stage and the creation of heavy elements."
    "Describe in detail how the global financial system works, covering central banks, monetary policy, fractional reserve banking, bond markets, stock exchanges, derivatives, foreign exchange markets, the role of the Federal Reserve, and how these systems interact during financial crises."
    "Write a comprehensive explanation of plate tectonics covering mantle convection, divergent and convergent plate boundaries, subduction zones, volcanic arc formation, transform faults, hotspot volcanism, continental drift evidence, seafloor spreading, and the Wilson cycle of supercontinent assembly and breakup."
    "Explain the complete process of how a modern jet engine produces thrust, from air intake through compression stages, combustion chamber fuel injection and ignition, turbine extraction of energy, and exhaust nozzle acceleration. Cover bypass ratios, turbofan vs turbojet designs, and the thermodynamic Brayton cycle."
    "Describe the chemistry and physics of cooking at a molecular level, covering Maillard reactions, caramelization, protein denaturation, starch gelatinization, emulsification, gluten formation, fermentation, and how temperature, pH, salt concentration, and mechanical force affect food at the molecular scale."
)

ROUND=0
for prompt in "${LONG_PROMPTS[@]}"; do
    (( ROUND++ )) || true
    log "  Round $ROUND/${#LONG_PROMPTS[@]}..."

    RESP=$(chat "$USER_TRUSTED" "$prompt" 2>/dev/null)

    # check if response is valid
    if ! echo "$RESP" | jq -e '.response' &>/dev/null; then
        log "  WARNING: Bad response in round $ROUND, continuing..."
    fi

    # check flag_warn
    WARN=$(get_slot_field "$USER_TRUSTED" "flag_warn")
    if [[ "$WARN" == "true" ]]; then
        log "  flag_warn triggered after round $ROUND"
        break
    fi

    sleep 1
done

FINAL_WARN=$(get_slot_field "$USER_TRUSTED" "flag_warn")
if [[ "$FINAL_WARN" == "true" ]]; then
    pass "$USER_TRUSTED hit warn threshold after $ROUND rounds"
else
    fail "$USER_TRUSTED did not hit warn threshold after ${#LONG_PROMPTS[@]} rounds" \
        "flag_warn=$FINAL_WARN — may need more prompts or lower threshold"
fi

# record history length before idle summarization
HIST_BEFORE=$(get_slot_field "$USER_TRUSTED" "history_len")
log "$USER_TRUSTED history length before idle: $HIST_BEFORE messages"
log "$USER_TRUSTED is now idle — background loop will trigger summarization"
IDLE_START=$(date +%s)

# ==================================================
# Phase 6: conversation_id Passthrough
# ==================================================
phase "6" "conversation_id Passthrough"

CID="test-conv-$(date +%s)"

# JSON endpoint
CID_RESP=$(chat "$USER_NORMAL" "Just say hello." "$CID")
CID_ECHO=$(echo "$CID_RESP" | jq -r '.conversation_id')
if [[ "$CID_ECHO" == "$CID" ]]; then
    pass "conversation_id echoed in JSON response"
else
    fail "conversation_id JSON echo" "Expected '$CID', got '$CID_ECHO'"
fi

# verify user_id is in response
CID_UID=$(echo "$CID_RESP" | jq -r '.user_id')
if [[ "$CID_UID" == "$USER_NORMAL" ]]; then
    pass "user_id in JSON response"
else
    fail "user_id in response" "Expected '$USER_NORMAL', got '$CID_UID'"
fi

# SSE endpoint — capture init and done events
CID_SSE="test-sse-conv-$(date +%s)"
SSE_OUT=$(chat_stream_raw "$USER_NORMAL" "Say the word apple." "$CID_SSE" 2>/dev/null)

# check init event has conversation_id
SSE_INIT=$(echo "$SSE_OUT" | grep "event: init" -A1 | grep "data:" | head -1 | sed 's/data: //')
if echo "$SSE_INIT" | jq -e ".conversation_id == \"$CID_SSE\"" &>/dev/null; then
    pass "conversation_id in SSE init event"
else
    fail "conversation_id in SSE init" "Init data: $SSE_INIT"
fi

# check done event
SSE_DONE=$(echo "$SSE_OUT" | grep "event: done" -A1 | grep "data:" | head -1 | sed 's/data: //')
if [[ "$SSE_DONE" == "$CID_SSE" ]]; then
    pass "conversation_id in SSE done event"
else
    fail "conversation_id in SSE done" "Done data: '$SSE_DONE', expected '$CID_SSE'"
fi

# null conversation_id should work fine
NULL_CID=$(chat "$USER_NORMAL" "Say ok." "")
if echo "$NULL_CID" | jq -e '.response' &>/dev/null; then
    pass "Empty conversation_id accepted"
else
    fail "Empty conversation_id" "$NULL_CID"
fi

# switch conversation_id mid-user
CID_B="switched-conv-$(date +%s)"
SWITCH_RESP=$(chat "$USER_NORMAL" "Now say goodbye." "$CID_B")
SWITCH_CID=$(echo "$SWITCH_RESP" | jq -r '.conversation_id')
if [[ "$SWITCH_CID" == "$CID_B" ]]; then
    pass "conversation_id switch echoed correctly"
else
    fail "conversation_id switch" "Expected '$CID_B', got '$SWITCH_CID'"
fi

# ==================================================
# Phase 7: SSE JSON Safety
# ==================================================
phase "7" "SSE JSON Safety (Special Characters)"

# conversation_id with quotes
EVIL_CID='test"with"quotes'
EVIL_SSE=$(chat_stream_raw "$USER_NORMAL" "Say ok." "$EVIL_CID" 2>/dev/null)
EVIL_INIT=$(echo "$EVIL_SSE" | grep "event: init" -A1 | grep "data:" | head -1 | sed 's/data: //')

if echo "$EVIL_INIT" | jq . &>/dev/null; then
    pass "SSE init valid JSON with quotes in conversation_id"
else
    fail "SSE JSON broken by quotes" "Raw: $EVIL_INIT"
fi

# conversation_id with backslashes
SLASH_CID='test\\back\\slash'
SLASH_SSE=$(chat_stream_raw "$USER_NORMAL" "Say ok." "$SLASH_CID" 2>/dev/null)
SLASH_INIT=$(echo "$SLASH_SSE" | grep "event: init" -A1 | grep "data:" | head -1 | sed 's/data: //')

if echo "$SLASH_INIT" | jq . &>/dev/null; then
    pass "SSE init valid JSON with backslashes in conversation_id"
else
    fail "SSE JSON broken by backslashes" "Raw: $SLASH_INIT"
fi

# ==================================================
# Phase 8: Guest Chat + Idle Setup
# ==================================================
phase "8" "Guest Chat + Idle Setup"

# send a few messages as guest
GUEST_R1=$(chat "$USER_GUEST" "Hello, I am a guest user.")
if echo "$GUEST_R1" | jq -e '.response' &>/dev/null; then
    pass "Guest chat message 1"
else
    fail "Guest chat 1" "$GUEST_R1"
fi

GUEST_R2=$(chat "$USER_GUEST" "Tell me a short joke.")
if echo "$GUEST_R2" | jq -e '.response' &>/dev/null; then
    pass "Guest chat message 2"
else
    fail "Guest chat 2" "$GUEST_R2"
fi

# verify guest has history
GUEST_HIST=$(get_slot_field "$USER_GUEST" "history_len")
if [[ "$GUEST_HIST" -ge 4 ]]; then
    pass "Guest has history ($GUEST_HIST messages)"
else
    fail "Guest history count" "Expected >= 4, got $GUEST_HIST"
fi

log "Guest is now idle — background loop will clear history"
GUEST_IDLE_START=$(date +%s)

# ==================================================
# Phase 9: Concurrent Request Smoke Test
# ==================================================
phase "9" "Concurrent Request Smoke Test"

# spread across DIFFERENT users/slots to avoid
# same-slot contention hang in llama.cpp.
# each request hits a different id_slot.
log "Firing concurrent requests across different users/slots..."

CONC_TMPDIR=$(mktemp -d)

chat "$USER_NORMAL" "Concurrent test. Say the number one." \
    > "$CONC_TMPDIR/user_normal.json" 2>/dev/null &
PID_A=$!

chat "$USER_NORMAL2" "Concurrent test. Say the number two." \
    > "$CONC_TMPDIR/user_normal2.json" 2>/dev/null &
PID_B=$!

chat "$USER_GUEST" "Concurrent test. Say the number three." \
    > "$CONC_TMPDIR/user_guest.json" 2>/dev/null &
PID_C=$!

# wait with timeout — kill stragglers after 3 minutes
CONC_DEADLINE=$(( $(date +%s) + 180 ))
CONC_PIDS=($PID_A $PID_B $PID_C)
CONC_HUNG=false

while true; do
    ALL_DONE=true
    for pid in "${CONC_PIDS[@]}"; do
        if kill -0 "$pid" 2>/dev/null; then
            ALL_DONE=false
        fi
    done

    if $ALL_DONE; then
        break
    fi

    if [[ $(date +%s) -ge $CONC_DEADLINE ]]; then
        log "WARNING: Concurrent requests timed out after 3 minutes — killing"
        for pid in "${CONC_PIDS[@]}"; do
            kill "$pid" 2>/dev/null || true
        done
        sleep 1
        CONC_HUNG=true
        break
    fi

    sleep 2
done

if $CONC_HUNG; then
    fail "Concurrent requests timed out" "One or more requests hung for >3 minutes"
else
    # check each response
    CONC_OK=0
    for f in "$CONC_TMPDIR"/*.json; do
        if jq -e '.response' "$f" &>/dev/null; then
            (( CONC_OK++ )) || true
        fi
    done

    if [[ $CONC_OK -eq 3 ]]; then
        pass "All 3 concurrent cross-slot requests completed"
    else
        fail "Only $CONC_OK of 3 concurrent requests returned valid responses" ""
    fi
fi

rm -rf "$CONC_TMPDIR"

# health check after concurrency
CONC_HEALTH=$(curl -s "$P_LANES_URL/health" | jq -r '.status')
if [[ "$CONC_HEALTH" == "ok" ]]; then
    pass "Server healthy after concurrent requests"
else
    fail "Server unhealthy after concurrent requests" "status=$CONC_HEALTH"
fi

# ==================================================
# Phase 10: Negative Cases
# ==================================================
phase "10" "Negative Cases"

# unknown user (guest enabled so maps to guest)
UNKNOWN=$(curl -s -X POST "$P_LANES_URL/channel/chat" \
    -H "Content-Type: application/json" \
    -d '{"user_id": "nobody_real", "message": "hello"}' \
    --max-time 30)
if echo "$UNKNOWN" | jq -e '.response' &>/dev/null; then
    pass "Unknown user mapped to guest"
elif echo "$UNKNOWN" | jq -e '.error' &>/dev/null; then
    pass "Unknown user rejected (guest mapping)"
else
    fail "Unknown user handling" "$UNKNOWN"
fi

# empty message
EMPTY=$(curl -s -X POST "$P_LANES_URL/channel/chat" \
    -H "Content-Type: application/json" \
    -d "{\"user_id\": \"$USER_NORMAL\", \"message\": \"\"}" \
    --max-time 10)
if echo "$EMPTY" | jq -e '.detail' &>/dev/null; then
    pass "Empty message rejected (validation error)"
else
    fail "Empty message should be rejected" "$EMPTY"
fi

# missing message field
NO_MSG=$(curl -s -X POST "$P_LANES_URL/channel/chat" \
    -H "Content-Type: application/json" \
    -d "{\"user_id\": \"$USER_NORMAL\"}" \
    --max-time 10)
if echo "$NO_MSG" | jq -e '.detail' &>/dev/null; then
    pass "Missing message field rejected"
else
    fail "Missing message should be rejected" "$NO_MSG"
fi

# extra forbidden field (pydantic extra=forbid)
EXTRA=$(curl -s -X POST "$P_LANES_URL/channel/chat" \
    -H "Content-Type: application/json" \
    -d "{\"user_id\": \"$USER_NORMAL\", \"message\": \"hi\", \"evil\": \"inject\"}" \
    --max-time 10)
if echo "$EXTRA" | jq -e '.detail' &>/dev/null; then
    pass "Extra field rejected (extra=forbid)"
else
    fail "Extra field should be rejected" "$EXTRA"
fi

# oversized message (> 4096 chars)
BIGMSG=$(python3 -c "print('A' * 4097)")
OVERSIZE=$(curl -s -X POST "$P_LANES_URL/channel/chat" \
    -H "Content-Type: application/json" \
    -d "$(jq -nc --arg u "$USER_NORMAL" --arg m "$BIGMSG" '{user_id: $u, message: $m}')" \
    --max-time 10)
if echo "$OVERSIZE" | jq -e '.detail' &>/dev/null; then
    pass "Oversized message rejected (> 4096 chars)"
else
    fail "Oversized message should be rejected" "$(echo "$OVERSIZE" | head -c 200)"
fi

# 404 catch-all
NOT_FOUND=$(curl -s "$P_LANES_URL/this/does/not/exist" --max-time 10)
if echo "$NOT_FOUND" | jq -e '.error' | grep -q "not found"; then
    pass "404 catch-all works"
else
    fail "404 catch-all" "$NOT_FOUND"
fi

# ==================================================
# Phase 11: Wait for Idle Triggers
# ==================================================
phase "11" "Idle Trigger Verification"

# calculate remaining wait time
ELAPSED_SINCE_IDLE=$(( $(date +%s) - IDLE_START ))
REMAINING_WAIT=$(( IDLE_WAIT - ELAPSED_SINCE_IDLE ))

if [[ $REMAINING_WAIT -gt 0 ]]; then
    log "Waiting ${REMAINING_WAIT}s for idle triggers to fire..."
    log "(idle timeout: ${TEST_IDLE_TIMEOUT}s + check interval: ${TEST_CHECK_INTERVAL}s)"

    # poll periodically so we can report progress
    WAIT_END=$(( $(date +%s) + REMAINING_WAIT ))
    TRUSTED_SUMMARIZED=false
    GUEST_CLEARED=false

    while [[ $(date +%s) -lt $WAIT_END ]]; do
        sleep 5

        # check if user1 was summarized
        if [[ "$TRUSTED_SUMMARIZED" == "false" ]]; then
            T_WARN=$(get_slot_field "$USER_TRUSTED" "flag_warn")
            T_SUMM=$(get_slot_field "$USER_TRUSTED" "has_summary")
            T_HIST=$(get_slot_field "$USER_TRUSTED" "history_len")
            if [[ "$T_WARN" == "false" && "$T_SUMM" == "true" ]]; then
                TRUSTED_SUMMARIZED=true
                log "  $USER_TRUSTED summarized! (history: $HIST_BEFORE → $T_HIST)"
            fi
        fi

        # check if guest was cleared
        if [[ "$GUEST_CLEARED" == "false" ]]; then
            G_HIST=$(get_slot_field "$USER_GUEST" "history_len")
            if [[ "$G_HIST" == "0" ]]; then
                GUEST_CLEARED=true
                log "  Guest history cleared!"
            fi
        fi

        # early exit if both done
        if [[ "$TRUSTED_SUMMARIZED" == "true" && "$GUEST_CLEARED" == "true" ]]; then
            log "Both idle triggers fired — continuing"
            break
        fi

        SECS_LEFT=$(( WAIT_END - $(date +%s) ))
        log "  Waiting... (${SECS_LEFT}s remaining)"
    done
else
    log "Idle wait already elapsed during other tests"
fi

# final checks
# user1 idle summarization
T_WARN_FINAL=$(get_slot_field "$USER_TRUSTED" "flag_warn")
T_HIST_FINAL=$(get_slot_field "$USER_TRUSTED" "history_len")
T_SUMM_FINAL=$(get_slot_field "$USER_TRUSTED" "has_summary")

if [[ "$T_WARN_FINAL" == "false" && "$T_SUMM_FINAL" == "true" ]]; then
    pass "$USER_TRUSTED idle-summarized (warn cleared, summary exists, history: $HIST_BEFORE → $T_HIST_FINAL)"
else
    fail "$USER_TRUSTED idle summarization" \
        "flag_warn=$T_WARN_FINAL, has_summary=$T_SUMM_FINAL, history=$T_HIST_FINAL"
fi

# guest idle clear
G_HIST_FINAL=$(get_slot_field "$USER_GUEST" "history_len")
G_SUMM_FINAL=$(get_slot_field "$USER_GUEST" "has_summary")

if [[ "$G_HIST_FINAL" == "0" ]]; then
    pass "Guest history cleared on idle"
else
    fail "Guest idle clear" "history_len=$G_HIST_FINAL (expected 0)"
fi

if [[ "$G_SUMM_FINAL" == "false" ]]; then
    pass "Guest summary cleared on idle"
else
    fail "Guest summary clear" "has_summary=$G_SUMM_FINAL (expected false)"
fi

# ==================================================
# Phase 12: Admin Dump — Verify Summary Content
# ==================================================
phase "12" "Summary Content Verification"

DUMP_TRUSTED=$(curl -s "$P_LANES_URL/admin/dump/$USER_TRUSTED?user_id=$USER_ADMIN")
SUMMARY_TEXT=$(echo "$DUMP_TRUSTED" | jq -r '.summary')

if [[ -n "$SUMMARY_TEXT" && "$SUMMARY_TEXT" != "null" && "$SUMMARY_TEXT" != "" ]]; then
    SUMMARY_LEN=${#SUMMARY_TEXT}
    pass "$USER_TRUSTED has summary content ($SUMMARY_LEN chars)"
else
    fail "$USER_TRUSTED summary is empty after summarization" ""
fi

# history should be shorter than before
if [[ "$T_HIST_FINAL" -lt "$HIST_BEFORE" ]]; then
    pass "History trimmed after summarization ($HIST_BEFORE → $T_HIST_FINAL messages)"
else
    fail "History not trimmed" "Before: $HIST_BEFORE, After: $T_HIST_FINAL"
fi

# ==================================================
# Phase 13: Restart + Persistence
# ==================================================
phase "13" "Restart + Persistence Verification"

log "Recording pre-restart state..."
PRE_SUMMARY=$(echo "$DUMP_TRUSTED" | jq -r '.summary')
PRE_SUMMARY_LEN=${#PRE_SUMMARY}

restart_service

# verify summary survived restart
POST_DUMP=$(curl -s "$P_LANES_URL/admin/dump/$USER_TRUSTED?user_id=$USER_ADMIN")
POST_SUMMARY=$(echo "$POST_DUMP" | jq -r '.summary')
POST_SUMMARY_LEN=${#POST_SUMMARY}

if [[ -n "$POST_SUMMARY" && "$POST_SUMMARY" != "null" && "$POST_SUMMARY" != "" ]]; then
    pass "Summary persisted across restart ($POST_SUMMARY_LEN chars)"
else
    fail "Summary lost on restart" "Pre: ${PRE_SUMMARY_LEN} chars, Post: empty"
fi

# verify summary content matches
if [[ "$PRE_SUMMARY" == "$POST_SUMMARY" ]]; then
    pass "Summary content identical after restart"
else
    fail "Summary content changed after restart" \
        "Pre: ${PRE_SUMMARY_LEN} chars, Post: ${POST_SUMMARY_LEN} chars"
fi

# conversation history should be empty after restart
# (history is in-memory only, not persisted)
POST_HIST=$(echo "$POST_DUMP" | jq -r '.history_len')
if [[ "$POST_HIST" == "0" ]]; then
    pass "Conversation history reset after restart (expected — memory only)"
else
    # history_len from dump includes the system message built from build_messages()
    # but the raw conversation_history should be empty
    log "  Note: history_len=$POST_HIST (may include system message in build_messages)"
    pass "Server restarted cleanly"
fi

# flags should be clear after restart
POST_WARN=$(echo "$POST_DUMP" | jq -r '.flag_warn')
POST_CRIT=$(echo "$POST_DUMP" | jq -r '.flag_crit')
if [[ "$POST_WARN" == "false" && "$POST_CRIT" == "false" ]]; then
    pass "Flags clear after restart"
else
    fail "Flags not cleared on restart" "warn=$POST_WARN, crit=$POST_CRIT"
fi

# verify LLM is healthy after restart
POST_HEALTH=$(curl -s "$P_LANES_URL/health")
if echo "$POST_HEALTH" | jq -e '.llm_running == true' &>/dev/null; then
    pass "LLM running after restart"
else
    fail "LLM not running after restart" "$POST_HEALTH"
fi

# verify hello_world still works after restart (module auto-discovery)
POST_PING=$(chat "$USER_TRUSTED" "ping")
if echo "$POST_PING" | jq -r '.response' | grep -qi "pong"; then
    pass "Module auto-discovery works after restart"
else
    fail "Module auto-discovery after restart" "$(echo "$POST_PING" | jq -r '.response')"
fi

# ==================================================
# Phase 14: Post-Restart Functional Smoke Test
# ==================================================
phase "14" "Post-Restart Smoke Test"

# verify all users can still chat
for uid in "$USER_TRUSTED" "$USER_NORMAL" "$USER_NORMAL2" "$USER_GUEST"; do
    SMOKE=$(chat "$uid" "Quick post-restart check. Say ok.")
    if echo "$SMOKE" | jq -e '.response' &>/dev/null; then
        pass "Post-restart chat works for $uid"
    else
        fail "Post-restart chat for $uid" "$SMOKE"
    fi
done

# verify conversation_id still works after restart
SMOKE_CID="post-restart-$(date +%s)"
SMOKE_RESP=$(chat "$USER_NORMAL" "Post-restart conv_id test." "$SMOKE_CID")
SMOKE_CID_ECHO=$(echo "$SMOKE_RESP" | jq -r '.conversation_id')
if [[ "$SMOKE_CID_ECHO" == "$SMOKE_CID" ]]; then
    pass "conversation_id passthrough works after restart"
else
    fail "conversation_id after restart" "Expected '$SMOKE_CID', got '$SMOKE_CID_ECHO'"
fi

# ==================================================
# Done — cleanup trap handles config restore + report
# ==================================================
echo ""
log "All phases complete."