#!/usr/bin/env bash
# ==================================================
# p-lanes Summarization Stress Test
# Hammers user2 with verbose-forcing prompts,
# monitors flag_warn/flag_crit/summary state,
# validates post-summarization behavior.
#
# Usage: ./test_summarize.sh [--base http://host:port]
# ==================================================

set -uo pipefail

BASE="http://localhost:7860"
TARGET="user2"
MAX_ROUNDS=200
DELAY=0.5

if [[ "${1:-}" == "--base" && -n "${2:-}" ]]; then
    BASE="$2"
    shift 2
fi

# ==================================================
# Helpers
# ==================================================
RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'
CYAN='\033[0;36m'; BOLD='\033[1m'; NC='\033[0m'

log()     { echo -e "${CYAN}[$(date +%H:%M:%S)]${NC} $*"; }
success() { echo -e "${GREEN}[$(date +%H:%M:%S)] ✓${NC} $*"; }
warn()    { echo -e "${YELLOW}[$(date +%H:%M:%S)] ⚠${NC} $*"; }
err()     { echo -e "${RED}[$(date +%H:%M:%S)] ✗${NC} $*"; }
divider() { echo -e "${BOLD}──────────────────────────────────────────${NC}"; }

chat() {
    local msg="$1"
    curl -s -X POST "$BASE/channel/chat" \
        -H "Content-Type: application/json" \
        -d "$(jq -n --arg u "$TARGET" --arg m "$msg" '{user_id:$u, message:$m}')" \
        --max-time 120
}

dump() {
    curl -s "$BASE/admin/dump/$TARGET?user_id=utility" --max-time 15
}

# verbose-forcing prompts that generate long responses
PROMPTS=(
    "Explain in detail how a CPU pipeline works, including fetch, decode, execute, memory, and writeback stages. Use at least 200 words and include examples."
    "Describe the complete lifecycle of an HTTP request from the browser to the server and back. Be thorough and explain every layer of the network stack."
    "Write a detailed comparison of TCP vs UDP, covering reliability, ordering, flow control, congestion control, and real-world use cases. Be verbose."
    "Explain how garbage collection works in modern programming languages. Cover mark-and-sweep, generational GC, reference counting, and tradeoffs. Use at least 200 words."
    "Describe in detail how TLS 1.3 handshake works step by step. Include the cryptographic primitives involved and explain why each step matters."
    "Explain virtual memory in depth — page tables, TLB, page faults, demand paging, copy-on-write, and memory-mapped files. Be thorough."
    "Write a detailed explanation of how B-trees work and why databases use them. Cover insertion, deletion, splitting, and performance characteristics."
    "Explain the CAP theorem in distributed systems with real-world examples of systems that choose CP vs AP. Discuss PACELC as well."
    "Describe how a modern GPU executes shader programs. Cover SIMD/SIMT, warps, occupancy, memory coalescing, and shared memory. Be detailed."
    "Explain the internals of how Git stores data — blobs, trees, commits, refs, packfiles, and delta compression. Use at least 200 words."
    "Describe how Linux process scheduling works — CFS, nice values, real-time priorities, cgroups, and CPU affinity. Be verbose and detailed."
    "Explain how DNS resolution works end to end — recursive resolvers, root servers, TLD servers, authoritative servers, caching, and TTL."
    "Write a detailed explanation of how RAFT consensus protocol works — leader election, log replication, safety, and membership changes."
    "Explain how SSDs work at the hardware level — NAND cells, pages, blocks, wear leveling, garbage collection, TRIM, and write amplification."
    "Describe the internals of a JIT compiler — parsing, IR generation, optimization passes, register allocation, and code emission. Be thorough."
    "Explain container isolation in Linux — namespaces, cgroups, seccomp, capabilities, overlay filesystems, and how Docker uses them."
    "Write a detailed explanation of how WiFi works at the physical layer — OFDM, channel bonding, MIMO, beamforming, and the 802.11ax improvements."
    "Explain how modern CPUs do branch prediction — static vs dynamic, BTB, two-level adaptive, TAGE, and speculative execution side effects."
    "Describe how a key-value store like RocksDB works internally — LSM trees, memtable, SSTables, compaction strategies, bloom filters."
    "Explain the complete boot process of a Linux system from BIOS/UEFI through GRUB to the init system and userspace. Be verbose."
)

PROMPT_COUNT=${#PROMPTS[@]}

# ==================================================
# Pre-flight
# ==================================================
divider
log "${BOLD}SUMMARIZATION STRESS TEST${NC}"
log "Target: $TARGET | Max rounds: $MAX_ROUNDS"
divider

health=$(curl -s "$BASE/health" --max-time 10 2>/dev/null || echo '{}')
if ! echo "$health" | jq -e '.status == "ok"' > /dev/null 2>&1; then
    err "p-lanes not reachable at $BASE"
    exit 1
fi
success "p-lanes is up"

# grab initial state
initial=$(dump)
init_hist=$(echo "$initial" | jq '.history_len')
init_warn=$(echo "$initial" | jq '.flag_warn')
init_crit=$(echo "$initial" | jq '.flag_crit')
init_summary=$(echo "$initial" | jq -r '.summary // empty')
init_sum_len=${#init_summary}

log "Initial state: history=$init_hist warn=$init_warn crit=$init_crit summary_len=$init_sum_len"
divider

# ==================================================
# Phase 1: Fill context until summarization fires
# ==================================================
log "${BOLD}PHASE 1: Filling context...${NC}"

WARN_SEEN=false
CRIT_SEEN=false
SUMMARY_FIRED=false
SUMMARY_ROUND=0
PRE_SUMMARY_HIST=0

for i in $(seq 1 $MAX_ROUNDS); do
    # cycle through prompts
    idx=$(( (i - 1) % PROMPT_COUNT ))
    prompt="${PROMPTS[$idx]}"

    resp=$(chat "$prompt")
    resp_text=$(echo "$resp" | jq -r '.response // empty')
    resp_len=${#resp_text}

    # check for slot-locked or memory-full responses
    if [[ "$resp_text" == *"just a second"* ]]; then
        warn "[$i] Slot locked — summarization in progress"
        sleep 3
    fi
    if [[ "$resp_text" == *"memory is full"* ]]; then
        warn "[$i] Memory full response"
    fi

    # poll state
    state=$(dump)
    hist=$(echo "$state" | jq '.history_len')
    flag_w=$(echo "$state" | jq '.flag_warn')
    flag_c=$(echo "$state" | jq '.flag_crit')
    summary=$(echo "$state" | jq -r '.summary // empty')
    sum_len=${#summary}

    # detect flag transitions
    if [[ "$flag_w" == "true" && "$WARN_SEEN" == "false" ]]; then
        WARN_SEEN=true
        success "[$i] flag_warn TRIGGERED (history=$hist)"
    fi

    if [[ "$flag_c" == "true" && "$CRIT_SEEN" == "false" ]]; then
        CRIT_SEEN=true
        PRE_SUMMARY_HIST=$hist
        success "[$i] flag_crit TRIGGERED (history=$hist)"
    fi

    # detect summarization completion
    if [[ "$SUMMARY_FIRED" == "false" && "$sum_len" -gt 20 && "$init_sum_len" -lt 20 ]]; then
        SUMMARY_FIRED=true
        SUMMARY_ROUND=$i
        success "[$i] SUMMARY GENERATED (${sum_len} chars)"
        log "  History went from $PRE_SUMMARY_HIST → $hist"
        log "  Flags: warn=$flag_w crit=$flag_c"
        divider
        break
    fi

    # also detect if history shrank (summarization happened and cleared old messages)
    if [[ "$SUMMARY_FIRED" == "false" && "$hist" -lt "$((init_hist + i - 5))" && "$sum_len" -gt 20 ]]; then
        SUMMARY_FIRED=true
        SUMMARY_ROUND=$i
        success "[$i] SUMMARY DETECTED via history shrink (${sum_len} chars, history=$hist)"
        divider
        break
    fi

    # progress every 10 rounds
    if (( i % 10 == 0 )); then
        log "[$i] history=$hist warn=$flag_w crit=$flag_c summary_len=$sum_len resp_len=$resp_len"
    fi

    sleep "$DELAY"
done

if [[ "$SUMMARY_FIRED" == "false" ]]; then
    err "Summarization never fired after $MAX_ROUNDS rounds"
    log "Final state: history=$hist warn=$flag_w crit=$flag_c summary_len=$sum_len"
    divider
    log "Dumping final state for manual inspection..."
    dump | jq '{history_len, flag_warn, flag_crit, summary: (.summary[:200] // "none"), is_idle}'
    exit 1
fi

# ==================================================
# Phase 2: Validate post-summarization state
# ==================================================
log "${BOLD}PHASE 2: Validating post-summary state...${NC}"

# let async settle
sleep 5
state=$(dump)
hist=$(echo "$state" | jq '.history_len')
flag_w=$(echo "$state" | jq '.flag_warn')
flag_c=$(echo "$state" | jq '.flag_crit')
summary=$(echo "$state" | jq -r '.summary // empty')
sum_len=${#summary}

log "Post-summary: history=$hist warn=$flag_w crit=$flag_c summary_len=$sum_len"

# flags should be cleared
if [[ "$flag_w" == "false" && "$flag_c" == "false" ]]; then
    success "Flags cleared after summarization"
else
    warn "Flags not cleared: warn=$flag_w crit=$flag_c"
fi

# history should be shorter than peak
if [[ "$PRE_SUMMARY_HIST" -gt 0 && "$hist" -lt "$PRE_SUMMARY_HIST" ]]; then
    success "History trimmed: $PRE_SUMMARY_HIST → $hist"
else
    warn "History not trimmed as expected (pre=$PRE_SUMMARY_HIST, post=$hist)"
fi

# summary should exist and be reasonable length
if [[ "$sum_len" -gt 50 ]]; then
    success "Summary exists ($sum_len chars)"
    log "Summary preview:"
    echo "$summary" | head -c 500
    echo ""
else
    err "Summary too short or missing ($sum_len chars)"
fi

divider

# ==================================================
# Phase 3: Coherence check — does it remember?
# ==================================================
log "${BOLD}PHASE 3: Post-summary coherence check...${NC}"

resp=$(chat "What topics have we been discussing? List the main subjects from our conversation.")
coherence=$(echo "$resp" | jq -r '.response // empty')
log "Coherence response:"
echo "$coherence"
divider

# check that it mentions at least some technical topics (from our prompts)
KEYWORDS=("CPU" "TCP" "UDP" "TLS" "memory" "Git" "Linux" "DNS" "GPU" "SSD" "network" "database" "container" "WiFi" "boot" "pipeline" "scheduling" "encryption" "protocol" "compiler")
hits=0
for kw in "${KEYWORDS[@]}"; do
    if echo "$coherence" | grep -qi "$kw"; then
        ((hits++)) || true
    fi
done

if [[ "$hits" -ge 3 ]]; then
    success "Coherence check passed — model recalled $hits topic keywords"
else
    warn "Coherence check weak — only $hits keyword matches (may need manual review)"
fi

divider

# ==================================================
# Phase 4: Keep chatting — verify system is stable
# ==================================================
log "${BOLD}PHASE 4: Post-summary stability (5 more messages)...${NC}"

for i in $(seq 1 5); do
    resp=$(chat "Quick follow-up question $i: explain one more thing about ${PROMPTS[$((i-1))]%%.*}.")
    resp_text=$(echo "$resp" | jq -r '.response // empty')
    if [[ -n "$resp_text" && "$resp_text" != "null" ]]; then
        success "Post-summary message $i OK (${#resp_text} chars)"
    else
        err "Post-summary message $i failed"
    fi
done

# final state
divider
log "${BOLD}FINAL STATE${NC}"
final=$(dump)
echo "$final" | jq '{history_len, flag_warn, flag_crit, summary_len: (.summary | length), is_idle}'

divider
success "${BOLD}SUMMARIZATION TEST COMPLETE${NC}"