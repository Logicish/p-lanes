#!/bin/bash
# p-lanes Alpha Test — Slot Isolation & Memory
# Run from any machine on the LAN
# Usage: bash test_alpha.sh [host]
# Default host: localhost:7860

HOST="${1:-localhost:7860}"
ENDPOINT="http://${HOST}/channel/chat"

divider() {
    echo ""
    echo "============================================"
    echo "  $1"
    echo "============================================"
}

send() {
    local user="$1"
    local msg="$2"
    local label="$3"
    echo ""
    echo "--- ${label} ---"
    echo ">>> ${user}: ${msg:0:80}..."
    echo ""
    curl -s -X POST "$ENDPOINT" \
        -H "Content-Type: application/json" \
        -d "{\"user_id\": \"${user}\", \"message\": \"${msg}\"}" | python3 -m json.tool
    echo ""
    sleep 1
}

# --------------------------------------------------
divider "TEST 1: Health Check"
# --------------------------------------------------
echo ""
curl -s "http://${HOST}/health" | python3 -m json.tool
echo ""

# --------------------------------------------------
divider "TEST 2: Slot Status (baseline)"
# --------------------------------------------------
echo ""
curl -s "http://${HOST}/slots" | python3 -m json.tool
echo ""

# --------------------------------------------------
divider "TEST 3: User1 — Store a secret"
# --------------------------------------------------
send "user1" \
    "Remember this secret code exactly: alpha-03387-gamma-4b. This is important. Confirm you have it." \
    "user1 stores secret"

# --------------------------------------------------
divider "TEST 4: User1 — Casual conversation (different topic)"
# --------------------------------------------------
send "user1" \
    "Tell me a short story about a blue dragon who adopted a lost rabbit. Keep it under 200 words." \
    "user1 asks for a story"

# --------------------------------------------------
divider "TEST 5: User2 — Token hog (long output)"
# --------------------------------------------------
send "user2" \
    "Write a detailed technical guide on building a home automation system from scratch. Cover hardware selection, network architecture, protocol choices (Zigbee, Z-Wave, Matter, WiFi), server setup, security hardening, backup strategies, and integration with voice assistants. Be thorough and use at least 500 words per section." \
    "user2 token hog"

# --------------------------------------------------
divider "TEST 6: User3 — Token hog (long output)"
# --------------------------------------------------
send "user3" \
    "Write an extremely detailed history of the development of artificial intelligence from 1950 to 2025. Cover the major breakthroughs, key researchers, important papers, funding cycles, AI winters, the rise of neural networks, transformers, and large language models. Include specific dates, names, and technical details. Be as comprehensive as possible." \
    "user3 token hog"

# --------------------------------------------------
divider "TEST 7: User1 — Recall the secret (isolation test)"
# --------------------------------------------------
send "user1" \
    "Reply with the secret code I gave you earlier and nothing else." \
    "user1 recall secret"

# --------------------------------------------------
divider "TEST 8: Slot Status (post-test)"
# --------------------------------------------------
echo ""
curl -s "http://${HOST}/slots" | python3 -m json.tool
echo ""

# --------------------------------------------------
divider "TEST 9: Guest — Verify guest access"
# --------------------------------------------------
send "guest" \
    "Hello, who am I talking to?" \
    "guest access test"

# --------------------------------------------------
divider "TEST 10: Unknown user — Should map to guest"
# --------------------------------------------------
send "randomstranger" \
    "Can you hear me?" \
    "unknown user -> guest fallback"

# --------------------------------------------------
divider "TESTS COMPLETE"
# --------------------------------------------------
echo ""
echo "Check:"
echo "  1. Test 7 should return 'alpha-03387-gamma-4b' despite user2/user3 activity"
echo "  2. Test 8 slots should show user2 + user3 with higher history_len"
echo "  3. Test 10 should succeed (mapped to guest slot)"
echo "  4. All responses should have valid timestamps"
echo ""
