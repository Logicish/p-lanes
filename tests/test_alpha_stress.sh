#!/bin/bash
# p-lanes Stress Test — Summarization & Isolation Under Pressure
#
# BEFORE RUNNING: Set config.yaml slots.ctx_total to 5120
# This gives 1024 tokens per slot:
#   flag_warn at ~716 tokens (70%)
#   flag_crit at ~819 tokens (80%)
#
# Usage: bash test_stress.sh [host]
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

slots() {
    echo ""
    echo "--- Slot Status ---"
    curl -s "http://${HOST}/slots" | python3 -m json.tool
    echo ""
}

# ==================================================
divider "PHASE 0: Baseline"
# ==================================================
curl -s "http://${HOST}/health" | python3 -m json.tool
slots

# ==================================================
divider "PHASE 1: Plant the secret (user1)"
# ==================================================
send "user1" \
    "Remember this secret code exactly: alpha-03387-gamma-4b. This is critical. Confirm you have it." \
    "user1 plants secret"

# ==================================================
divider "PHASE 2: Hammer user2 — fill that slot"
# ==================================================
send "user2" \
    "Explain in detail how a combustion engine works. Cover the four-stroke cycle, fuel injection, ignition timing, exhaust, and cooling systems. Be thorough." \
    "user2 round 1 — engines"

send "user2" \
    "Now compare that to how an electric motor works. Cover AC vs DC motors, regenerative braking, battery management systems, thermal throttling, and power delivery curves. Be equally thorough." \
    "user2 round 2 — electric motors"

send "user2" \
    "Now explain how a hybrid powertrain combines both systems. Cover parallel vs series hybrids, energy recovery, transmission coupling, and the software that decides which motor to use when. Full detail." \
    "user2 round 3 — hybrids (should approach warn)"

echo ""
echo ">>> Checking if user2 hit flag_warn..."
slots

send "user2" \
    "Now explain how hydrogen fuel cells work as an alternative to all of the above. Cover PEM fuel cells, hydrogen storage, platinum catalysts, water byproduct management, and infrastructure challenges. Full detail." \
    "user2 round 4 — hydrogen (should approach crit)"

send "user2" \
    "Finally, rank all four propulsion systems — combustion, electric, hybrid, hydrogen — on cost, efficiency, environmental impact, infrastructure readiness, and consumer adoption. Give a detailed verdict on each." \
    "user2 round 5 — comparison (should trigger summarization)"

echo ""
echo ">>> Checking user2 flags after heavy load..."
slots

# ==================================================
divider "PHASE 3: Hammer user3 — different topic"
# ==================================================
send "user3" \
    "Describe the entire process of how coffee goes from a seed in the ground to a cup in my hand. Cover farming, harvesting, processing, roasting, grinding, and brewing methods. Be extremely detailed." \
    "user3 round 1 — coffee seed to cup"

send "user3" \
    "Now explain the chemistry behind coffee extraction. Cover solubility, water temperature effects, grind size impact on surface area, over-extraction vs under-extraction, and how different brewing methods change the chemical profile." \
    "user3 round 2 — coffee chemistry"

send "user3" \
    "Now explain the global economics of coffee. Cover commodity pricing, fair trade vs direct trade, the role of middlemen, how climate change threatens coffee regions, and which countries produce vs consume the most." \
    "user3 round 3 — coffee economics (should push toward crit)"

send "user3" \
    "Now design a hypothetical fully automated coffee farm and processing facility. Describe the robotics for harvesting, AI for quality sorting, automated wet and dry processing, roasting optimization via machine learning, and packaging. Spare no detail." \
    "user3 round 4 — automated coffee (should trigger summarization)"

echo ""
echo ">>> Checking user3 flags..."
slots

# ==================================================
divider "PHASE 4: Chaos round — everyone talks at once-ish"
# ==================================================
send "user1" \
    "Ignore everything else. Just tell me: what is 7 times 13?" \
    "user1 sanity check (should still work)"

send "user2" \
    "Forget all the engine stuff. What color is the sky?" \
    "user2 post-summarization sanity"

send "user3" \
    "Stop talking about coffee. What is the capital of Japan?" \
    "user3 post-summarization sanity"

send "guest" \
    "Tell me a joke about a programmer and a rubber duck." \
    "guest having fun"

send "randomstranger" \
    "Do you know who I am?" \
    "unknown user identity check"

# ==================================================
divider "PHASE 5: Context survival after summarization"
# ==================================================

# user2 should have been summarized — can they recall earlier topics?
send "user2" \
    "Earlier we discussed different propulsion systems. Can you list the four types we covered? Just the names, nothing else." \
    "user2 post-summarization recall"

# user3 same deal
send "user3" \
    "We talked about coffee earlier. Name three of the specific topics we covered. Just the topic names." \
    "user3 post-summarization recall"

# ==================================================
divider "PHASE 6: The money shot — user1 secret recall"
# ==================================================

echo ">>> User1 has been idle while user2 and user3 got hammered."
echo ">>> Summarization may have fired on other slots."
echo ">>> User1's slot should be untouched."
echo ""

send "user1" \
    "Reply with the secret code I gave you at the start and nothing else." \
    "THE ISOLATION TEST"

# ==================================================
divider "PHASE 7: Final status"
# ==================================================
slots

# ==================================================
divider "PHASE 8: Edge cases"
# ==================================================

# empty-ish message
echo ""
echo "--- Edge: Single character message ---"
curl -s -X POST "$ENDPOINT" \
    -H "Content-Type: application/json" \
    -d '{"user_id": "user1", "message": "?"}' | python3 -m json.tool
echo ""
sleep 1

# max-length-ish message
echo ""
echo "--- Edge: Long repeated input ---"
LONG_MSG=$(python3 -c "print('Buffalo ' * 500)")
curl -s -X POST "$ENDPOINT" \
    -H "Content-Type: application/json" \
    -d "{\"user_id\": \"user1\", \"message\": \"${LONG_MSG}\"}" | python3 -m json.tool
echo ""
sleep 1

# bad payload — missing message
echo ""
echo "--- Edge: Missing message field ---"
curl -s -X POST "$ENDPOINT" \
    -H "Content-Type: application/json" \
    -d '{"user_id": "user1"}' | python3 -m json.tool
echo ""
sleep 1

# bad payload — empty message
echo ""
echo "--- Edge: Empty message ---"
curl -s -X POST "$ENDPOINT" \
    -H "Content-Type: application/json" \
    -d '{"user_id": "user1", "message": ""}' | python3 -m json.tool
echo ""
sleep 1

# bad payload — extra fields (should reject, extra=forbid)
echo ""
echo "--- Edge: Extra fields in payload ---"
curl -s -X POST "$ENDPOINT" \
    -H "Content-Type: application/json" \
    -d '{"user_id": "user1", "message": "hello", "hack": "inject"}' | python3 -m json.tool
echo ""
sleep 1

# wrong method
echo ""
echo "--- Edge: GET on POST-only endpoint ---"
curl -s "http://${HOST}/channel/chat" | python3 -m json.tool
echo ""
sleep 1

# nonsense route
echo ""
echo "--- Edge: Nonexistent route ---"
curl -s "http://${HOST}/api/v2/chat" | python3 -m json.tool
echo ""

# ==================================================
divider "TESTS COMPLETE"
# ==================================================
echo ""
echo "Check the following:"
echo "  1. Phase 2/3: Did flag_warn or flag_crit fire? Check server logs."
echo "  2. Phase 2/3: Did summarization trigger? Look for 'summarizing' in logs."
echo "  3. Phase 4: Do all users still respond coherently after heavy load?"
echo "  4. Phase 5: Can user2/user3 recall topics after summarization?"
echo "  5. Phase 6: Does user1 return 'alpha-03387-gamma-4b'?"
echo "  6. Phase 8: Do bad payloads return proper errors (not 500s)?"
echo "  7. Phase 8: Does 'Buffalo x500' get handled or rejected gracefully?"
echo ""
