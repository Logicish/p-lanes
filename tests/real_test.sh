#!/bin/bash
# p-lanes Production Test — 51200 ctx_total (10,240 per slot)
#
# Config requirements:
#   slots.ctx_total: 51200
#   slots.count: 5
#   Per slot: 10,240 tokens
#   warn at ~7,168 tokens (70%)
#   crit at ~8,192 tokens (80%)
#
# This test is designed to:
#   1. Validate isolation across all slots under real-world load
#   2. Push at least one user through a full summarization cycle
#   3. Test concurrent-ish interleaving between users
#   4. Verify post-summarization memory quality at production context
#   5. Exercise guest, unknown user, streaming, and edge cases
#   6. Confirm system stability over sustained inference
#
# Estimated runtime: 10-15 minutes
# Usage: bash test_production.sh [host]

HOST="${1:-localhost:7860}"
ENDPOINT="http://${HOST}/channel/chat"
STREAM_ENDPOINT="http://${HOST}/channel/chat/stream"

RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
CYAN='\033[0;36m'
NC='\033[0m'

divider() {
    echo ""
    echo -e "${CYAN}============================================${NC}"
    echo -e "${CYAN}  $1${NC}"
    echo -e "${CYAN}============================================${NC}"
}

send() {
    local user="$1"
    local msg="$2"
    local label="$3"
    echo ""
    echo -e "${YELLOW}--- ${label} ---${NC}"
    echo ">>> ${user}: ${msg:0:80}..."
    echo ""
    local response
    response=$(curl -s -X POST "$ENDPOINT" \
        -H "Content-Type: application/json" \
        -d "{\"user_id\": \"${user}\", \"message\": \"${msg}\"}")
    echo "$response" | python3 -m json.tool
    echo ""
    sleep 1
}

send_quiet() {
    # send without printing the full response — just status
    local user="$1"
    local msg="$2"
    local label="$3"
    echo -e "  ${label}..."
    local response
    response=$(curl -s -X POST "$ENDPOINT" \
        -H "Content-Type: application/json" \
        -d "{\"user_id\": \"${user}\", \"message\": \"${msg}\"}")
    local status
    status=$(echo "$response" | python3 -c "import sys,json; d=json.load(sys.stdin); print('OK' if 'response' in d else 'ERROR: '+str(d))" 2>/dev/null)
    if [ "$status" = "OK" ]; then
        echo -e "  ${GREEN}✓ ${label}${NC}"
    else
        echo -e "  ${RED}✗ ${label}: ${status}${NC}"
    fi
    sleep 1
}

slots() {
    echo ""
    echo -e "${YELLOW}--- Slot Status ---${NC}"
    curl -s "http://${HOST}/slots" | python3 -m json.tool
    echo ""
}

slots_compact() {
    # one-line-per-user summary
    echo ""
    curl -s "http://${HOST}/slots" | python3 -c "
import sys, json
data = json.load(sys.stdin)
for uid, info in data['slots'].items():
    flags = []
    if info['flag_warn']: flags.append('WARN')
    if info['flag_crit']: flags.append('CRIT')
    if info['has_summary']: flags.append('HAS_SUMMARY')
    flag_str = ' [' + ', '.join(flags) + ']' if flags else ''
    print(f\"  {uid:10s} slot={info['slot']}  hist={info['history_len']:2d}  sec={info['security']}{flag_str}\")
"
    echo ""
}

stream_test() {
    local user="$1"
    local msg="$2"
    local label="$3"
    echo ""
    echo -e "${YELLOW}--- ${label} (SSE) ---${NC}"
    echo ">>> ${user}: ${msg:0:80}..."
    echo ""
    echo -n "  Stream: "
    curl -s -N -X POST "$STREAM_ENDPOINT" \
        -H "Content-Type: application/json" \
        -d "{\"user_id\": \"${user}\", \"message\": \"${msg}\"}" | \
        while IFS= read -r line; do
            if [[ "$line" == data:* ]]; then
                data="${line#data: }"
                if [ "$data" != "" ] && [ "$data" != "[DONE]" ]; then
                    echo -n "$data"
                fi
            fi
            if [[ "$line" == *"event: done"* ]]; then
                break
            fi
        done
    echo ""
    echo -e "  ${GREEN}✓ Stream complete${NC}"
    echo ""
    sleep 1
}

# ==================================================
divider "PHASE 0: Preflight"
# ==================================================
echo ""
echo "Target: ${HOST}"
echo "Expected: 51,200 total context / 5 slots = 10,240 per slot"
echo ""

curl -s "http://${HOST}/health" | python3 -m json.tool
slots

# ==================================================
divider "PHASE 1: Secrets & Anchors"
# ==================================================
# Plant verifiable facts in multiple users that we'll
# recall at different points throughout the test.

send "user1" \
    "Remember these two things exactly. Secret code: alpha-03387-gamma-4b. Favorite color: cerulean blue. Confirm both." \
    "user1 plants secret + color"

send "user2" \
    "Remember this precisely. My dog's name is Captain Barksworth and he weighs exactly 47 pounds. Confirm." \
    "user2 plants anchor"

send "user3" \
    "Remember this: the password is 'correct horse battery staple' and the backup code is 9921. Confirm both." \
    "user3 plants anchor"

echo ""
echo -e "${GREEN}Anchors planted in 3 slots.${NC}"
slots_compact

# ==================================================
divider "PHASE 2: Sustained Conversation — User1"
# ==================================================
# Build a genuine multi-turn conversation that
# accumulates naturally. Not trying to fill the
# slot — testing normal conversational flow.

send "user1" \
    "I'm designing a home automation system. What protocol should I use for lighting — Zigbee, Z-Wave, or WiFi? Give me the tradeoffs." \
    "user1 home automation Q1"

send "user1" \
    "Good points. I have about 40 devices planned. Would Zigbee mesh handle that without a dedicated coordinator for each room?" \
    "user1 home automation Q2"

send "user1" \
    "What about latency? I want lights to respond in under 200ms from button press. Is that realistic with Zigbee mesh through 3 hops?" \
    "user1 home automation Q3"

send "user1" \
    "Let's switch topics. Explain how KV caching works in transformer models. I know the basics of attention — go deeper on the cache mechanics." \
    "user1 KV cache Q1"

send "user1" \
    "How does grouped-query attention change the KV cache memory footprint compared to standard multi-head attention?" \
    "user1 KV cache Q2"

echo ""
echo "User1 midpoint check:"
slots_compact

# ==================================================
divider "PHASE 3: Parallel Buildup — User2 & User3"
# ==================================================
# While user1 rests, build up user2 and user3 with
# their own sustained conversations.

send "user2" \
    "Explain the differences between RISC and CISC processor architectures. Cover instruction sets, pipeline design, power efficiency, and modern convergence." \
    "user2 CPU architecture"

send "user3" \
    "Walk me through how a compiler turns source code into machine code. Cover lexing, parsing, AST generation, optimization passes, and code generation." \
    "user3 compiler pipeline"

send "user2" \
    "How does branch prediction work in modern CPUs? Cover static prediction, dynamic prediction with BTB, and speculative execution." \
    "user2 branch prediction"

send "user3" \
    "Explain register allocation in compilers. Cover graph coloring, linear scan, and how spilling works when you run out of registers." \
    "user3 register allocation"

send "user2" \
    "What's the actual difference between L1, L2, and L3 cache in terms of latency, size tradeoffs, and how cache coherency works in multi-core systems?" \
    "user2 cache hierarchy"

send "user3" \
    "How do modern compilers handle loop optimization? Cover unrolling, vectorization, tiling, and how LLVM's optimization passes work in practice." \
    "user3 loop optimization"

echo ""
echo "All users midpoint check:"
slots_compact

# ==================================================
divider "PHASE 4: Cross-Check — Anchor Recall #1"
# ==================================================
# Verify all anchors still intact mid-conversation.

send "user1" \
    "What was my secret code? Just the code, nothing else." \
    "user1 mid-test secret recall"

send "user2" \
    "What's my dog's name and weight? Just those facts." \
    "user2 mid-test anchor recall"

send "user3" \
    "What was the password and backup code I gave you? Just those, nothing else." \
    "user3 mid-test anchor recall"

# ==================================================
divider "PHASE 5: Fill User2 — Push to Summarization"
# ==================================================
# Now we hammer user2 specifically to push toward
# the 70% warn and 80% crit thresholds.
# At 10,240 per slot, we need roughly 7,000+ tokens
# of accumulated history. Each heavy Q&A pair adds
# ~600-800 tokens, so we need ~8-10 more rounds.

echo "Hammering user2 toward summarization threshold..."
echo "(This will take a few minutes)"
echo ""

send_quiet "user2" \
    "Explain out-of-order execution in detail. Cover the reorder buffer, reservation stations, and how the CPU maintains program order while executing out of order." \
    "user2 OoO execution"

send_quiet "user2" \
    "How does simultaneous multithreading (SMT/Hyperthreading) share physical CPU resources between logical threads? Cover the register file, execution units, and cache partitioning." \
    "user2 SMT/Hyperthreading"

send_quiet "user2" \
    "Explain memory-mapped I/O vs port-mapped I/O. How does the CPU communicate with peripherals, and what role does DMA play in high-bandwidth transfers?" \
    "user2 MMIO and DMA"

send_quiet "user2" \
    "What are memory barriers and why do they matter in multi-core systems? Cover store buffers, memory ordering models, and the difference between acquire and release semantics." \
    "user2 memory barriers"

send_quiet "user2" \
    "Describe the full boot sequence of an x86 system from power-on to OS handoff. Cover POST, UEFI, bootloader stages, and kernel initialization." \
    "user2 boot sequence"

send_quiet "user2" \
    "How does virtual memory work at the hardware level? Cover page tables, TLB, page faults, and how the MMU translates addresses. Include multi-level page tables." \
    "user2 virtual memory"

send_quiet "user2" \
    "Explain PCIe architecture. Cover lanes, generations, the transaction layer, and how enumeration works during boot. How does a GPU communicate over PCIe with the CPU?" \
    "user2 PCIe architecture"

send_quiet "user2" \
    "Describe how NUMA architecture affects memory access patterns in multi-socket systems. Cover local vs remote memory latency, OS scheduling implications, and how applications should be NUMA-aware." \
    "user2 NUMA architecture"

send_quiet "user2" \
    "Explain how modern CPUs handle floating-point operations. Cover the x87 FPU legacy, SSE, AVX, and AVX-512. How do these SIMD extensions map to physical execution units?" \
    "user2 floating point SIMD"

send_quiet "user2" \
    "Describe the Intel and AMD microarchitecture differences in the 2024-2025 generation. Cover core layouts, cache hierarchies, chiplet designs, and IPC improvements." \
    "user2 modern microarchitectures"

echo ""
echo "User2 post-hammer status:"
slots_compact

# If user2 hasn't hit crit yet, push harder
send_quiet "user2" \
    "Now explain how GPU compute architectures differ from CPU architectures. Cover SIMT execution, warp scheduling, shared memory, and how CUDA maps to hardware." \
    "user2 GPU vs CPU compute"

send_quiet "user2" \
    "How does tensor core execution work on NVIDIA GPUs? Cover the matrix multiply-accumulate operations, mixed precision, and how frameworks like cuBLAS use them for LLM inference." \
    "user2 tensor cores"

echo ""
echo "User2 final push status:"
slots_compact

# ==================================================
divider "PHASE 6: Interleaved Rapid Fire"
# ==================================================
# Alternate between users rapidly to test slot
# switching under load.

send "user1" \
    "Quick question — in our home automation discussion, did you recommend Zigbee or Z-Wave for my 40-device setup?" \
    "user1 interleave recall"

send "user3" \
    "In our compiler discussion, what were the optimization passes you mentioned for loops?" \
    "user3 interleave recall"

send "user1" \
    "What about my favorite color? What did I tell you it was?" \
    "user1 color recall"

send "user2" \
    "Summarize everything we've discussed about CPU architecture in 3 sentences or less." \
    "user2 comprehensive recall"

send "user3" \
    "And what was my backup code number?" \
    "user3 backup code recall"

# ==================================================
divider "PHASE 7: Guest & Unknown Users"
# ==================================================

send "guest" \
    "Explain recursion to me like I'm 10 years old." \
    "guest basic question"

send "guest" \
    "Can you give me an example with something fun, like counting down for a rocket launch?" \
    "guest follow-up (tests guest history)"

send "stranger1" \
    "Hey are you there?" \
    "unknown user 1 → guest"

send "nobody_special" \
    "What did the last person say to you?" \
    "unknown user 2 → guest (tests shared guest slot)"

# ==================================================
divider "PHASE 8: Streaming Endpoint"
# ==================================================

stream_test "user1" \
    "Give me a haiku about a GPU running inference at 3 AM." \
    "user1 streaming test"

stream_test "guest" \
    "Tell me a one-paragraph story about a sentient toaster." \
    "guest streaming test"

# ==================================================
divider "PHASE 9: The Final Recall"
# ==================================================
# All anchors, all users, after everything.

echo -e "${YELLOW}Every anchor planted in Phase 1 should survive.${NC}"
echo ""

send "user1" \
    "Reply with ONLY my secret code and my favorite color, separated by a comma. Nothing else." \
    "USER1 FINAL RECALL"

send "user2" \
    "Reply with ONLY my dog's name and his weight. Nothing else." \
    "USER2 FINAL RECALL"

send "user3" \
    "Reply with ONLY the password and the backup code I told you. Nothing else." \
    "USER3 FINAL RECALL"

# ==================================================
divider "PHASE 10: Final Status & Edge Cases"
# ==================================================

slots

# Edge cases
echo -e "${YELLOW}--- Edge Cases ---${NC}"
echo ""

echo "  Testing empty body..."
curl -s -X POST "$ENDPOINT" \
    -H "Content-Type: application/json" \
    -d '{}' | python3 -c "import sys,json; d=json.load(sys.stdin); print('  ✓ Empty body:', d.get('detail',[{}])[0].get('msg','?'))" 2>/dev/null
echo ""

echo "  Testing no content-type..."
curl -s -X POST "$ENDPOINT" \
    -d 'hello' | python3 -c "import sys,json; d=json.load(sys.stdin); print('  ✓ No content-type:', d.get('detail',[{}])[0].get('msg','?'))" 2>/dev/null
echo ""

echo "  Testing unicode message..."
curl -s -X POST "$ENDPOINT" \
    -H "Content-Type: application/json" \
    -d '{"user_id": "user1", "message": "こんにちは、元気ですか？🤖"}' | python3 -c "import sys,json; d=json.load(sys.stdin); print('  ✓ Unicode:', 'OK' if 'response' in d else d)" 2>/dev/null
echo ""

echo "  Testing max_length boundary (4096 chars)..."
LONG_MSG=$(python3 -c "print('test ' * 819)")
curl -s -X POST "$ENDPOINT" \
    -H "Content-Type: application/json" \
    -d "{\"user_id\": \"user1\", \"message\": \"${LONG_MSG}\"}" | python3 -c "import sys,json; d=json.load(sys.stdin); print('  ✓ 4095 chars:', 'OK' if 'response' in d else d)" 2>/dev/null
echo ""

echo "  Testing over max_length (4097+ chars)..."
OVER_MSG=$(python3 -c "print('test ' * 820)")
curl -s -X POST "$ENDPOINT" \
    -H "Content-Type: application/json" \
    -d "{\"user_id\": \"user1\", \"message\": \"${OVER_MSG}\"}" | python3 -c "import sys,json; d=json.load(sys.stdin); print('  ✓ Over limit:', d.get('detail',[{}])[0].get('msg','accepted (no server-side limit hit)'))" 2>/dev/null
echo ""

echo "  Testing XSS in message..."
curl -s -X POST "$ENDPOINT" \
    -H "Content-Type: application/json" \
    -d '{"user_id": "user1", "message": "<script>alert(1)</script>"}' | python3 -c "import sys,json; d=json.load(sys.stdin); print('  ✓ XSS:', 'OK - response received' if 'response' in d else d)" 2>/dev/null
echo ""

echo "  Testing SQL-ish injection in user_id..."
curl -s -X POST "$ENDPOINT" \
    -H "Content-Type: application/json" \
    -d "{\"user_id\": \"admin'; DROP TABLE users;--\", \"message\": \"hello\"}" | python3 -c "import sys,json; d=json.load(sys.stdin); print('  ✓ SQLi user_id:', 'mapped to guest' if d.get('user_id')=='guest' else d)" 2>/dev/null
echo ""

# ==================================================
divider "TESTS COMPLETE"
# ==================================================
echo ""
echo "Checklist:"
echo "  1. Phase 4: All three anchors recalled mid-conversation?"
echo "  2. Phase 5: Did user2 hit flag_warn or flag_crit? Check server logs."
echo "  3. Phase 5: Did summarization trigger? Look for 'summarizing' events."
echo "  4. Phase 6: Can users recall topics from interleaved conversations?"
echo "  5. Phase 7: Unknown users mapped to guest? Guest history shared?"
echo "  6. Phase 8: Streaming endpoint delivered tokens?"
echo "  7. Phase 9: ALL three final recalls returned correct anchors?"
echo "  8. Phase 10: Edge cases returned proper errors, no 500s?"
echo "  9. Server logs: Zero unhandled exceptions?"
echo " 10. nvidia-smi: VRAM stable around ~11.5 GB?"
echo ""
echo "If user2 hit summarization, also check:"
echo "  - Can user2 still recall 'Captain Barksworth, 47 pounds' after summary?"
echo "  - Does the summary preserve CPU architecture topics?"
echo "  - How many recent messages were kept? (should be 4-10 at 10k context)"
echo ""
