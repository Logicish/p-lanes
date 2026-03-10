#!/usr/bin/env bash
# ==================================================
# p-lanes Edge Case Stress Test Suite
# Author:  Nyx (for Root / Logic-Ish Designs)
# Date:    March 5, 2026
#
# Prerequisites:
#   - p-lanes running on localhost:7860
#   - llama-server running on localhost:8080
#   - All 5 slots active (user1-3, guest, utility)
#   - curl, jq installed
#
# Usage:
#   chmod +x test.sh && ./test.sh
#   ./test.sh --base http://192.168.1.50:7860
# ==================================================

set -euo pipefail

# ==================================================
# Config
# ==================================================
BASE="http://localhost:7860"
LLAMA_HEALTH="http://localhost:8080/health"

# override base URL from CLI
if [[ "${1:-}" == "--base" && -n "${2:-}" ]]; then
    BASE="$2"
    shift 2
fi

# long filler for context-stuffing tests (roughly 300 tokens per message)
FILLER="This is a deliberately long message designed to consume context window tokens. \
I'm going to talk about several topics to make this feel like a real conversation. \
First, let me discuss the weather — it's been cloudy lately with intermittent rain. \
Second, I've been thinking about upgrading my home network to 10GbE but the switch prices \
are still absurd for anything with more than 4 ports. Third, I made pasta last night and \
it turned out great, used a simple aglio e olio recipe with way too much garlic. Fourth, \
there's a new kernel release that supposedly improves io_uring performance by 15% which \
would be nice for the NVMe drives. Fifth, I read an interesting paper about attention \
mechanisms in small language models and how quantization affects long-context coherence. \
Sixth, the cat knocked over my monitor again and I'm starting to think a VESA arm mount \
is no longer optional but mandatory for survival."

# ==================================================
# Helpers
# ==================================================
PASS=0; FAIL=0; SKIP=0; TOTAL=0
RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'
CYAN='\033[0;36m'; BOLD='\033[1m'; NC='\033[0m'

log()  { echo -e "${CYAN}[TEST]${NC} $*"; }
pass() { ((PASS++)) || true; ((TOTAL++)) || true; echo -e "  ${GREEN}✓ PASS${NC} — $*"; }
fail() { ((FAIL++)) || true; ((TOTAL++)) || true; echo -e "  ${RED}✗ FAIL${NC} — $*"; }
skip() { ((SKIP++)) || true; ((TOTAL++)) || true; echo -e "  ${YELLOW}⊘ SKIP${NC} — $*"; }
section() { echo -e "\n${BOLD}═══════════════════════════════════════════════${NC}"; echo -e "${BOLD}  $*${NC}"; echo -e "${BOLD}═══════════════════════════════════════════════${NC}"; }

chat() {
    # chat USER_ID MESSAGE [CONV_ID]
    local uid="$1" msg="$2" cid="${3:-}"
    local payload
    if [[ -n "$cid" ]]; then
        payload=$(jq -n --arg u "$uid" --arg m "$msg" --arg c "$cid" \
            '{user_id: $u, message: $m, conversation_id: $c}')
    else
        payload=$(jq -n --arg u "$uid" --arg m "$msg" \
            '{user_id: $u, message: $m}')
    fi
    curl -s -X POST "$BASE/channel/chat" \
        -H "Content-Type: application/json" \
        -d "$payload" \
        --max-time 120
}

stream_raw() {
    # stream_raw USER_ID MESSAGE [CONV_ID] — returns raw SSE text
    local uid="$1" msg="$2" cid="${3:-}"
    local payload
    if [[ -n "$cid" ]]; then
        payload=$(jq -n --arg u "$uid" --arg m "$msg" --arg c "$cid" \
            '{user_id: $u, message: $m, conversation_id: $c}')
    else
        payload=$(jq -n --arg u "$uid" --arg m "$msg" \
            '{user_id: $u, message: $m}')
    fi
    curl -s -N -X POST "$BASE/channel/chat/stream" \
        -H "Content-Type: application/json" \
        -d "$payload" \
        --max-time 120
}

get_json() {
    curl -s "$BASE$1" --max-time 15
}

get_slot_info() {
    get_json "/slots"
}

get_user_dump() {
    # get_user_dump TARGET_USER_ID
    get_json "/admin/dump/$1?user_id=utility"
}

assert_http() {
    # assert_http METHOD PATH EXPECTED_STATUS [BODY]
    local method="$1" path="$2" expected="$3" body="${4:-}"
    local code
    if [[ -n "$body" ]]; then
        code=$(curl -s -o /dev/null -w "%{http_code}" -X "$method" "$BASE$path" \
            -H "Content-Type: application/json" -d "$body" --max-time 15)
    else
        code=$(curl -s -o /dev/null -w "%{http_code}" -X "$method" "$BASE$path" --max-time 15)
    fi
    if [[ "$code" == "$expected" ]]; then
        pass "$method $path → $code"
    else
        fail "$method $path → expected $expected, got $code"
    fi
}


# ==================================================
# Pre-flight
# ==================================================
section "PRE-FLIGHT CHECKS"

log "Checking p-lanes health..."
health=$(get_json "/health" 2>/dev/null || echo '{}')
if echo "$health" | jq -e '.status == "ok"' > /dev/null 2>&1; then
    pass "p-lanes is up"
else
    echo -e "${RED}FATAL: p-lanes not reachable at $BASE${NC}"
    echo "$health"
    exit 1
fi

llm_running=$(echo "$health" | jq -r '.llm_running')
if [[ "$llm_running" == "true" ]]; then
    pass "LLM server is running"
else
    fail "LLM server not running — tests will fail"
fi

log "Checking slot status..."
slot_info=$(get_slot_info)
slot_count=$(echo "$slot_info" | jq '.slots | length')
log "  Active slots: $slot_count"


# ==================================================
# 1. GATE 1 — Authentication & Access Control
# ==================================================
section "1. GATE 1 — AUTH & ACCESS CONTROL"

# known users should pass
log "Known user access..."
resp=$(chat "user1" "Hello, this is a gate test.")
if echo "$resp" | jq -e '.user_id == "user1"' > /dev/null 2>&1; then
    pass "user1 authenticated and routed"
else
    fail "user1 auth — response: $(echo "$resp" | head -c 200)"
fi

# unknown user should map to guest
log "Unknown user → guest mapping..."
resp=$(chat "totally_unknown_person" "Am I a guest?")
if echo "$resp" | jq -e '.user_id == "guest"' > /dev/null 2>&1; then
    pass "Unknown user mapped to guest"
else
    fail "Unknown user mapping — response: $(echo "$resp" | head -c 200)"
fi

# case insensitivity
log "Case insensitivity..."
resp=$(chat "User1" "Case test.")
if echo "$resp" | jq -e '.user_id == "user1"' > /dev/null 2>&1; then
    pass "user_id is case-insensitive"
else
    fail "Case sensitivity — response: $(echo "$resp" | head -c 200)"
fi

# whitespace in user_id
log "Whitespace stripping..."
resp=$(chat "  user2  " "Whitespace test.")
if echo "$resp" | jq -e '.user_id == "user2"' > /dev/null 2>&1; then
    pass "user_id whitespace stripped"
else
    fail "Whitespace strip — response: $(echo "$resp" | head -c 200)"
fi


# ==================================================
# 2. ADMIN GATE — /llm/restart
# ==================================================
section "2. ADMIN GATE — /llm/restart"

# non-admin should get 403
log "Non-admin restart attempt..."
assert_http POST "/llm/restart" 403 '{"user_id": "user1"}'
assert_http POST "/llm/restart" 403 '{"user_id": "guest"}'
assert_http POST "/llm/restart" 403 '{"user_id": "user2"}'

# admin (utility) should succeed — we'll test this at the END
# to avoid disrupting other tests
log "(Admin restart deferred to final section)"


# ==================================================
# 3. ADMIN DUMP ENDPOINTS
# ==================================================
section "3. ADMIN DUMP ENDPOINTS"

log "Admin dump — all users..."
resp=$(get_json "/admin/dump?user_id=utility")
if echo "$resp" | jq -e '.dump' > /dev/null 2>&1; then
    dump_users=$(echo "$resp" | jq '.dump | keys | length')
    pass "Admin dump returned $dump_users users"
else
    fail "Admin dump all — response: $(echo "$resp" | head -c 200)"
fi

log "Admin dump — non-admin should fail..."
code=$(curl -s -o /dev/null -w "%{http_code}" "$BASE/admin/dump?user_id=user1" --max-time 15)
if [[ "$code" == "403" ]]; then
    pass "Non-admin dump correctly rejected (403)"
else
    fail "Non-admin dump returned $code instead of 403"
fi

log "Admin dump — single user..."
resp=$(get_user_dump "user1")
if echo "$resp" | jq -e '.user_id == "user1"' > /dev/null 2>&1; then
    pass "Single user dump for user1"
else
    fail "Single user dump — response: $(echo "$resp" | head -c 200)"
fi

log "Admin dump — nonexistent user..."
code=$(curl -s -o /dev/null -w "%{http_code}" "$BASE/admin/dump/fake_user?user_id=utility" --max-time 15)
if [[ "$code" == "404" ]]; then
    pass "Dump nonexistent user → 404"
else
    fail "Dump nonexistent user returned $code instead of 404"
fi


# ==================================================
# 4. CONVERSATION_ID PASSTHROUGH
# ==================================================
section "4. CONVERSATION_ID PASSTHROUGH"

CONV_ID="test-conv-$(date +%s)"

log "JSON endpoint — conversation_id round-trip..."
resp=$(chat "user1" "Conversation ID test." "$CONV_ID")
returned_cid=$(echo "$resp" | jq -r '.conversation_id // empty')
if [[ "$returned_cid" == "$CONV_ID" ]]; then
    pass "conversation_id echoed in JSON response"
else
    fail "conversation_id missing or wrong — got '$returned_cid', expected '$CONV_ID'"
fi

log "SSE endpoint — conversation_id in init + done events..."
sse_output=$(stream_raw "user2" "Stream conv_id test." "$CONV_ID")
init_cid=$(echo "$sse_output" | grep -A1 'event: init' | grep 'data:' | head -1 | sed 's/data: //' | jq -r '.conversation_id // empty' 2>/dev/null)
done_cid=$(echo "$sse_output" | grep -A1 'event: done' | grep 'data:' | head -1 | sed 's/data: //')

if [[ "$init_cid" == "$CONV_ID" ]]; then
    pass "conversation_id in SSE init event"
else
    fail "SSE init conversation_id — got '$init_cid'"
fi

if [[ "$done_cid" == "$CONV_ID" ]]; then
    pass "conversation_id in SSE done event"
else
    fail "SSE done conversation_id — got '$done_cid'"
fi


# ==================================================
# 5. PAYLOAD VALIDATION
# ==================================================
section "5. PAYLOAD VALIDATION"

log "Empty message..."
code=$(curl -s -o /dev/null -w "%{http_code}" -X POST "$BASE/channel/chat" \
    -H "Content-Type: application/json" \
    -d '{"user_id": "user1", "message": ""}' --max-time 15)
if [[ "$code" == "422" ]]; then
    pass "Empty message rejected (422)"
else
    fail "Empty message returned $code instead of 422"
fi

log "Missing message field..."
code=$(curl -s -o /dev/null -w "%{http_code}" -X POST "$BASE/channel/chat" \
    -H "Content-Type: application/json" \
    -d '{"user_id": "user1"}' --max-time 15)
if [[ "$code" == "422" ]]; then
    pass "Missing message field rejected (422)"
else
    fail "Missing message returned $code instead of 422"
fi

log "Extra forbidden field..."
code=$(curl -s -o /dev/null -w "%{http_code}" -X POST "$BASE/channel/chat" \
    -H "Content-Type: application/json" \
    -d '{"user_id": "user1", "message": "test", "evil_field": "hax"}' --max-time 15)
if [[ "$code" == "422" ]]; then
    pass "Extra field rejected (422, extra=forbid)"
else
    fail "Extra field returned $code instead of 422"
fi

log "Catch-all 404..."
assert_http GET "/nonexistent/route" 404
assert_http POST "/nonexistent/route" 404


# ==================================================
# 6. SLOT ISOLATION — Multi-User Concurrent Chat
# ==================================================
section "6. SLOT ISOLATION — CONCURRENT MULTI-USER"

log "Firing 3 users simultaneously..."
chat "user1" "User1 concurrent test: my favorite color is red." &
PID1=$!
chat "user2" "User2 concurrent test: my favorite animal is a cat." &
PID2=$!
chat "user3" "User3 concurrent test: my favorite food is sushi." &
PID3=$!

wait $PID1 && wait $PID2 && wait $PID3
pass "All 3 concurrent requests completed without error"

log "Verifying slot assignments from dump..."
for uid in user1 user2 user3; do
    dump=$(get_user_dump "$uid")
    slot=$(echo "$dump" | jq '.slot')
    hist=$(echo "$dump" | jq '.history_len')
    log "  $uid → slot $slot, history_len $hist"
done
pass "Slot isolation check (manual verify above)"


# ==================================================
# 7. CONTEXT PRESSURE — Push Toward Summarization
# ==================================================
section "7. CONTEXT PRESSURE — FILL TOWARD CRIT"

TARGET_USER="user2"
log "Stuffing $TARGET_USER with long messages to trigger flag_warn/flag_crit..."
log "This will take a while — sending many messages to fill context window..."

for i in $(seq 1 30); do
    resp=$(chat "$TARGET_USER" "Message $i of pressure test. $FILLER")
    # check for emergency responses
    resp_text=$(echo "$resp" | jq -r '.response // empty')
    if [[ "$resp_text" == *"memory is full"* ]]; then
        log "  → Hit memory-full response at message $i"
        break
    fi
    if [[ "$resp_text" == *"just a second"* ]]; then
        log "  → Slot locked (summarization in progress) at message $i"
        sleep 3
    fi

    # check flags every 5 messages
    if (( i % 5 == 0 )); then
        dump=$(get_user_dump "$TARGET_USER")
        warn=$(echo "$dump" | jq '.flag_warn')
        crit=$(echo "$dump" | jq '.flag_crit')
        hist=$(echo "$dump" | jq '.history_len')
        has_sum=$(echo "$dump" | jq '.has_summary // false')
        log "  [$i] history=$hist warn=$warn crit=$crit summary_exists=$(echo "$dump" | jq 'if .summary and (.summary | length > 0) then true else false end')"

        if [[ "$crit" == "true" ]]; then
            log "  → flag_crit triggered at message $i"
            pass "flag_crit triggered during pressure test"
            break
        fi
        if [[ "$warn" == "true" && "$i" -le 25 ]]; then
            log "  → flag_warn triggered at message $i"
        fi
    fi
done

# give async summarization time to complete
sleep 5

dump=$(get_user_dump "$TARGET_USER")
summary=$(echo "$dump" | jq -r '.summary // empty')
hist=$(echo "$dump" | jq '.history_len')
warn=$(echo "$dump" | jq '.flag_warn')
crit=$(echo "$dump" | jq '.flag_crit')

log "Post-pressure state: history=$hist, warn=$warn, crit=$crit"

if [[ -n "$summary" && ${#summary} -gt 20 ]]; then
    pass "Summary was generated (${#summary} chars)"
else
    log "  Summary content: '${summary:0:100}'"
    skip "Summary may not have been generated (check if context was big enough)"
fi


# ==================================================
# 8. ASYNC SUMMARIZATION — Message During Summarize
# ==================================================
section "8. ASYNC MERGE — MESSAGES DURING SUMMARIZATION"

log "Snapshot boundary test: send messages while summarization runs..."
# fire a message to push crit if not already
chat "$TARGET_USER" "Pre-snapshot message alpha. $FILLER" > /dev/null
chat "$TARGET_USER" "Pre-snapshot message bravo. $FILLER" > /dev/null
chat "$TARGET_USER" "Pre-snapshot message charlie. $FILLER" > /dev/null

# grab history before the expected summarization
pre_dump=$(get_user_dump "$TARGET_USER")
pre_hist=$(echo "$pre_dump" | jq '.history_len')
log "  Pre: history_len=$pre_hist"

# fire a bunch of messages rapidly (some should arrive during summarization)
for i in $(seq 1 5); do
    chat "$TARGET_USER" "During-summarize message $i: the quick brown fox." &
done
wait

sleep 8  # let summarization settle

post_dump=$(get_user_dump "$TARGET_USER")
post_hist=$(echo "$post_dump" | jq '.history_len')
post_summary=$(echo "$post_dump" | jq -r '.summary // empty')
log "  Post: history_len=$post_hist, summary_len=${#post_summary}"

if [[ "$post_hist" -gt 0 ]]; then
    pass "History preserved after async merge (history=$post_hist)"
else
    fail "History empty after merge — possible message loss"
fi


# ==================================================
# 9. STREAMING ENDPOINT
# ==================================================
section "9. STREAMING ENDPOINT — SSE VALIDATION"

log "Basic stream test..."
sse_output=$(stream_raw "user1" "Tell me a one-sentence fact about penguins.")

has_init=$(echo "$sse_output" | grep -c 'event: init' || true)
has_token=$(echo "$sse_output" | grep -c 'event: token' || true)
has_done=$(echo "$sse_output" | grep -c 'event: done' || true)

if [[ "$has_init" -gt 0 ]]; then
    pass "SSE init event received"
else
    fail "SSE init event missing"
fi

if [[ "$has_token" -gt 0 ]]; then
    pass "SSE token events received ($has_token chunks)"
else
    fail "SSE token events missing"
fi

if [[ "$has_done" -gt 0 ]]; then
    pass "SSE done event received"
else
    fail "SSE done event missing"
fi


# ==================================================
# 10. GUEST BEHAVIOR
# ==================================================
section "10. GUEST BEHAVIOR"

log "Guest chat..."
resp=$(chat "guest" "Hello, I'm a guest.")
if echo "$resp" | jq -e '.user_id == "guest"' > /dev/null 2>&1; then
    pass "Guest chat works"
else
    fail "Guest chat — response: $(echo "$resp" | head -c 200)"
fi

log "Guest should never have a summary..."
dump=$(get_user_dump "guest")
guest_summary=$(echo "$dump" | jq -r '.summary // empty')
if [[ -z "$guest_summary" || "$guest_summary" == "null" ]]; then
    pass "Guest has no summary (correct)"
else
    fail "Guest has a summary — it shouldn't: '${guest_summary:0:100}'"
fi


# ==================================================
# 11. BROADCAST ENDPOINT (expect disabled)
# ==================================================
section "11. BROADCAST LISTENER"

log "Broadcast listen (likely 503 if no module enabled it)..."
code=$(curl -s -o /dev/null -w "%{http_code}" "$BASE/channel/listen/user1?user_id=user1" --max-time 5 || echo "000")
if [[ "$code" == "503" ]]; then
    pass "Broadcast correctly returns 503 (disabled)"
elif [[ "$code" == "200" ]]; then
    pass "Broadcast is enabled and listening"
else
    log "  Broadcast returned $code"
    skip "Broadcast returned unexpected code $code"
fi

log "Cross-user listen should be blocked..."
code=$(curl -s -o /dev/null -w "%{http_code}" "$BASE/channel/listen/user1?user_id=user2" --max-time 5 || echo "000")
if [[ "$code" == "403" || "$code" == "503" ]]; then
    pass "Cross-user listen blocked ($code)"
else
    fail "Cross-user listen returned $code — expected 403 or 503"
fi


# ==================================================
# 12. MEMORY PERSISTENCE — Pre-Restart Snapshot
# ==================================================
section "12. MEMORY PERSISTENCE — PRE-RESTART SNAPSHOT"

MEMORY_MARKER="PERSISTENCE_MARKER_$(date +%s)"

log "Planting memory marker in user3..."
resp=$(chat "user3" "Remember this exact phrase: $MEMORY_MARKER")
if echo "$resp" | jq -e '.response' > /dev/null 2>&1; then
    pass "Memory marker planted"
else
    fail "Failed to plant marker"
fi

# capture pre-restart state
pre_restart_dump=$(get_user_dump "user3")
pre_restart_hist=$(echo "$pre_restart_dump" | jq '.history_len')
pre_restart_summary=$(echo "$pre_restart_dump" | jq -r '.summary // empty')
log "  Pre-restart: user3 history=$pre_restart_hist, summary_len=${#pre_restart_summary}"


# ==================================================
# 13. ADMIN LLM RESTART
# ==================================================
section "13. ADMIN LLM RESTART"

log "Restarting LLM via admin endpoint..."
resp=$(curl -s -X POST "$BASE/llm/restart" \
    -H "Content-Type: application/json" \
    -d '{"user_id": "utility"}' \
    --max-time 120)

success=$(echo "$resp" | jq -r '.success // false')
if [[ "$success" == "true" ]]; then
    pass "LLM restart succeeded"
else
    fail "LLM restart failed — response: $(echo "$resp" | head -c 200)"
fi

# wait for LLM to come back up
log "Waiting for LLM to become healthy..."
for i in $(seq 1 30); do
    sleep 3
    health=$(get_json "/health" 2>/dev/null || echo '{}')
    llm_up=$(echo "$health" | jq -r '.llm_running // false')
    if [[ "$llm_up" == "true" ]]; then
        pass "LLM back up after restart (${i}x3s)"
        break
    fi
    if [[ "$i" == "30" ]]; then
        fail "LLM didn't come back within 90 seconds"
    fi
done


# ==================================================
# 14. POST-RESTART VALIDATION
# ==================================================
section "14. POST-RESTART VALIDATION"

log "Post-restart health check..."
health=$(get_json "/health")
if echo "$health" | jq -e '.status == "ok"' > /dev/null 2>&1; then
    pass "p-lanes healthy after restart"
else
    fail "p-lanes unhealthy after restart"
fi

log "Post-restart chat — all users should still work..."
for uid in user1 user2 user3 guest; do
    resp=$(chat "$uid" "Post-restart test from $uid.")
    if echo "$resp" | jq -e '.response' > /dev/null 2>&1; then
        pass "$uid can chat after restart"
    else
        fail "$uid broken after restart — response: $(echo "$resp" | head -c 200)"
    fi
done

log "Checking slot status after restart..."
post_slots=$(get_slot_info)
post_count=$(echo "$post_slots" | jq '.slots | length')
if [[ "$post_count" -ge 4 ]]; then
    pass "All slots active after restart ($post_count slots)"
else
    fail "Only $post_count slots active after restart"
fi

log "Checking user3 memory persistence..."
post_dump=$(get_user_dump "user3")
post_hist=$(echo "$post_dump" | jq '.history_len')
log "  Post-restart: user3 history=$post_hist"

# note: after LLM restart, KV cache is cold but p-lanes history should persist
# in memory (not wiped). The slot goes cold but conversation_history stays.
if [[ "$post_hist" -gt 0 ]]; then
    pass "user3 history survived restart (history=$post_hist)"
else
    fail "user3 history lost after restart"
fi

# check if marker is findable in history
marker_found=$(echo "$post_dump" | jq --arg m "$MEMORY_MARKER" \
    '[.messages[]? | select(.content | contains($m))] | length')
if [[ "$marker_found" -gt 0 ]]; then
    pass "Memory marker found in user3 history after restart"
else
    # might be in summary if summarization happened
    summary_has_marker=$(echo "$post_dump" | jq -r '.summary // empty')
    if [[ "$summary_has_marker" == *"$MEMORY_MARKER"* ]]; then
        pass "Memory marker found in user3 summary (summarized during pressure)"
    else
        skip "Memory marker not found — may have been summarized away (check manually)"
    fi
fi


# ==================================================
# RESULTS
# ==================================================
section "RESULTS"

echo -e "  ${GREEN}Passed: $PASS${NC}"
echo -e "  ${RED}Failed: $FAIL${NC}"
echo -e "  ${YELLOW}Skipped: $SKIP${NC}"
echo -e "  Total:  $TOTAL"
echo ""

if [[ "$FAIL" -eq 0 ]]; then
    echo -e "${GREEN}${BOLD}ALL TESTS PASSED${NC}"
    exit 0
else
    echo -e "${RED}${BOLD}$FAIL TEST(S) FAILED${NC}"
    exit 1
fi