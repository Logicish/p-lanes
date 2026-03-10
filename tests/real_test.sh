#!/bin/bash
# p-lanes Summarization Stress Test
#
# Goal: Push user1 and user2 past 10,240 tokens to guarantee
#       summarization triggers. Verify post-summary recall.
#
# Pre-test: DELETE all saved profile/history data manually.
#
# Config expected:
#   slots.ctx_total: 51200
#   slots.count: 5
#   Per slot: 10,240 tokens
#   warn at ~7,168 tokens (70%)
#   crit at ~8,192 tokens (80%)
#
# Strategy:
#   - Send long prompts (burns input tokens)
#   - Request detailed responses (burns output tokens)
#   - Check slot state after every few turns
#   - Two users hammered in parallel phases
#   - Anchors planted early, verified post-summarization
#
# Estimated runtime: 15-25 minutes
# Usage: bash real_test.sh [host]

HOST="${1:-localhost:7860}"
ENDPOINT="http://${HOST}/channel/chat"
STREAM_ENDPOINT="http://${HOST}/channel/chat/stream"

RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
CYAN='\033[0;36m'
MAGENTA='\033[0;35m'
NC='\033[0m'

PASS=0
FAIL=0

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
    echo ">>> ${user}: ${msg:0:100}..."
    echo ""
    local response
    response=$(curl -s -X POST "$ENDPOINT" \
        -H "Content-Type: application/json" \
        -d "{\"user_id\": \"${user}\", \"message\": \"${msg}\"}")
    echo "$response" | python3 -m json.tool 2>/dev/null
    echo ""
    sleep 1
}

send_quiet() {
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

# Check if a field matches expected value, print pass/fail
check() {
    local label="$1"
    local actual="$2"
    local expected="$3"
    if [ "$actual" = "$expected" ]; then
        echo -e "  ${GREEN}✓ PASS: ${label} (got: ${actual})${NC}"
        ((PASS++))
    else
        echo -e "  ${RED}✗ FAIL: ${label} (expected: ${expected}, got: ${actual})${NC}"
        ((FAIL++))
    fi
}

# Check if value is greater than threshold
check_gt() {
    local label="$1"
    local actual="$2"
    local threshold="$3"
    if [ "$actual" -gt "$threshold" ] 2>/dev/null; then
        echo -e "  ${GREEN}✓ PASS: ${label} (${actual} > ${threshold})${NC}"
        ((PASS++))
    else
        echo -e "  ${RED}✗ FAIL: ${label} (${actual} not > ${threshold})${NC}"
        ((FAIL++))
    fi
}

# Check response contains expected substring
check_recall() {
    local label="$1"
    local user="$2"
    local msg="$3"
    local expected="$4"
    local response
    response=$(curl -s -X POST "$ENDPOINT" \
        -H "Content-Type: application/json" \
        -d "{\"user_id\": \"${user}\", \"message\": \"${msg}\"}")
    local text
    text=$(echo "$response" | python3 -c "import sys,json; print(json.load(sys.stdin).get('response',''))" 2>/dev/null)
    if echo "$text" | grep -qi "$expected"; then
        echo -e "  ${GREEN}✓ PASS: ${label} — found '${expected}' in response${NC}"
        ((PASS++))
    else
        echo -e "  ${RED}✗ FAIL: ${label} — '${expected}' not found in: ${text:0:120}${NC}"
        ((FAIL++))
    fi
    sleep 1
}

slots_compact() {
    echo ""
    echo -e "${MAGENTA}  --- Slot Snapshot ---${NC}"
    curl -s "http://${HOST}/slots" | python3 -c "
import sys, json
data = json.load(sys.stdin)
for uid, info in data['slots'].items():
    flags = []
    if info['flag_warn']: flags.append('WARN')
    if info['flag_crit']: flags.append('CRIT')
    if info['has_summary']: flags.append('HAS_SUMMARY')
    flag_str = ' [' + ', '.join(flags) + ']' if flags else ''
    print(f\"  {uid:10s} slot={info['slot']}  hist={info['history_len']:3d}  sec={info['security']}{flag_str}\")
"
    echo ""
}

slots_check_flags() {
    local user="$1"
    curl -s "http://${HOST}/slots" | python3 -c "
import sys, json
data = json.load(sys.stdin)
info = data['slots'].get('${user}', {})
warn = info.get('flag_warn', False)
crit = info.get('flag_crit', False)
summ = info.get('has_summary', False)
hist = info.get('history_len', 0)
print(f'{warn}|{crit}|{summ}|{hist}')
" 2>/dev/null
}

wait_and_check() {
    local user="$1"
    local label="$2"
    local flags
    flags=$(slots_check_flags "$user")
    local warn=$(echo "$flags" | cut -d'|' -f1)
    local crit=$(echo "$flags" | cut -d'|' -f2)
    local summ=$(echo "$flags" | cut -d'|' -f3)
    local hist=$(echo "$flags" | cut -d'|' -f4)
    echo -e "  ${MAGENTA}${label}: ${user} hist=${hist} warn=${warn} crit=${crit} summary=${summ}${NC}"
}

# ==================================================
divider "PHASE 0: Preflight"
# ==================================================
echo ""
echo "Target: ${HOST}"
echo "Expected: 51,200 total ctx / 5 slots / 10,240 per slot"
echo "Goal: Push user1 + user2 past 10k tokens each"
echo ""

echo -e "${YELLOW}Checking health...${NC}"
HEALTH=$(curl -s "http://${HOST}/health")
echo "$HEALTH" | python3 -m json.tool 2>/dev/null

LLM_OK=$(echo "$HEALTH" | python3 -c "import sys,json; print(json.load(sys.stdin).get('llm_running', False))" 2>/dev/null)
check "LLM running" "$LLM_OK" "True"

echo ""
echo -e "${YELLOW}Verifying clean state...${NC}"
slots_compact

# Verify no stale summaries
for u in user1 user2 user3; do
    flags=$(slots_check_flags "$u")
    summ=$(echo "$flags" | cut -d'|' -f3)
    hist=$(echo "$flags" | cut -d'|' -f4)
    check "${u} starts with no summary" "$summ" "False"
    check "${u} starts with empty history" "$hist" "0"
done

# ==================================================
divider "PHASE 1: Plant Anchors"
# ==================================================
# Short exchanges — just enough to plant verifiable facts.

send "user1" \
    "Remember these exactly. Secret code: foxtrot-99182-delta-7z. Favorite city: Reykjavik. Confirm both back to me word for word." \
    "user1 anchor"

send "user2" \
    "Remember these exactly. Pet name: Admiral Whiskers. Pet weight: 13.2 pounds. Confirm both back to me word for word." \
    "user2 anchor"

send "user3" \
    "Remember this exactly. Passphrase: 'the moon has no atmosphere' and backup PIN: 5507. Confirm both." \
    "user3 anchor (control — will NOT be hammered)"

slots_compact

# ==================================================
divider "PHASE 2: Hammer User1 — Long Prompts, Long Responses"
# ==================================================
# Strategy: Send meaty prompts that are themselves 200+ tokens,
# and request exhaustive responses. Each exchange should burn
# 600-1200 tokens total (prompt + response).

echo "Filling user1 toward summarization..."
echo ""

send_quiet "user1" \
    "I need a thorough technical comparison. Compare the TCP and UDP transport protocols in detail. Cover the three-way handshake for TCP, how flow control and congestion avoidance work with sliding windows, Nagle's algorithm, how UDP achieves lower latency by skipping reliability guarantees, typical use cases for each in modern applications like gaming, video streaming, VoIP, and web browsing, and explain how QUIC attempts to combine the best of both. Be exhaustive — I want at least 5-6 detailed paragraphs." \
    "user1 turn 1: TCP vs UDP deep dive"

wait_and_check "user1" "After turn 1"

send_quiet "user1" \
    "Now explain the DNS resolution process end to end. Start from when a user types a URL in the browser. Cover the browser cache, OS resolver cache, recursive resolver, root nameservers, TLD nameservers, authoritative nameservers, glue records, DNS over HTTPS vs DNS over TLS, DNSSEC validation chain, and how CDNs use geographic DNS routing to direct users to nearby edge servers. Include how TTL affects caching at each layer. Be thorough — cover every step." \
    "user1 turn 2: DNS deep dive"

wait_and_check "user1" "After turn 2"

send_quiet "user1" \
    "Explain the full TLS 1.3 handshake process in detail. Cover the ClientHello, ServerHello, key exchange with ephemeral Diffie-Hellman, how the handshake eliminates the RSA key exchange from TLS 1.2, session resumption with PSK, 0-RTT early data and its replay attack risks, the role of certificate transparency logs, OCSP stapling, and how HSTS preloading works. Compare the number of round trips in TLS 1.2 vs 1.3. I want a comprehensive walkthrough." \
    "user1 turn 3: TLS 1.3 handshake"

wait_and_check "user1" "After turn 3"

send_quiet "user1" \
    "Describe how HTTP/2 and HTTP/3 improve on HTTP/1.1. Cover multiplexing, header compression with HPACK and QPACK, server push, stream prioritization, how HTTP/2 suffers from head-of-line blocking at the TCP layer and how HTTP/3 fixes this by running over QUIC, connection migration when switching networks, and the practical impact on page load times for complex web applications with many assets. Explain why some sites still haven't adopted HTTP/3." \
    "user1 turn 4: HTTP/2 and HTTP/3"

wait_and_check "user1" "After turn 4"

send_quiet "user1" \
    "Explain BGP routing in depth. Cover how autonomous systems advertise routes, the difference between iBGP and eBGP, path selection attributes including AS path length, local preference, MED, and weight. Explain route reflectors, confederations, BGP communities, how route leaks and hijacks happen with real-world examples like the Pakistan YouTube hijack, and how RPKI and ROAs are being deployed to prevent them. Cover the convergence problem and why BGP is considered fragile for the scale of the modern internet." \
    "user1 turn 5: BGP routing"

wait_and_check "user1" "After turn 5"

send_quiet "user1" \
    "Walk me through how a modern load balancer works at layers 4 and 7. Cover the differences between L4 (TCP/UDP forwarding, DSR, NAT) and L7 (HTTP routing, header inspection, SSL termination). Explain consistent hashing for backend selection, health checking mechanisms, connection draining during deploys, sticky sessions and their downsides, rate limiting at the load balancer level, and how cloud providers like AWS implement this with ALB vs NLB vs GWLB. Include how keepalive connections interact with load balancing decisions." \
    "user1 turn 6: load balancers"

wait_and_check "user1" "After turn 6"

send_quiet "user1" \
    "Explain container networking in Kubernetes. Cover the pod network model where every pod gets an IP, how CNI plugins like Calico and Flannel implement this, the difference between overlay networks using VXLAN vs direct routing with BGP, how kube-proxy handles service routing with iptables vs IPVS, how DNS works inside a cluster with CoreDNS, NetworkPolicy resources for microsegmentation, ingress controllers and how they map to load balancers, and service mesh architectures like Istio that use sidecar proxies for mTLS and traffic management." \
    "user1 turn 7: K8s networking"

wait_and_check "user1" "After turn 7"

send_quiet "user1" \
    "Describe the full lifecycle of a packet from a browser on a home network to a server in a cloud data center and back. Cover the application layer HTTP request, TLS encryption, TCP segmentation, IP routing through the home router with NAT, traversal through the ISP's network, peering at an internet exchange point, arrival at the cloud provider's edge, routing through their internal network to the correct availability zone, arrival at the host's virtual network interface, delivery to the container or VM, and the entire reverse path for the response. Include MTU considerations and where fragmentation might occur." \
    "user1 turn 8: packet lifecycle"

wait_and_check "user1" "After turn 8"

send_quiet "user1" \
    "Explain how CDNs work at a technical level. Cover how origin servers are shielded by edge caches, how cache keys are computed from URLs and headers, cache invalidation strategies including TTL-based expiration, purge APIs, and stale-while-revalidate, how TLS certificates are managed across thousands of edge nodes, how CDNs handle dynamic content with edge compute like Cloudflare Workers, the economics of peering agreements and why CDNs place servers inside ISP networks, and how anycast routing directs users to the nearest edge POP. Discuss cache hit ratios and their impact on origin load." \
    "user1 turn 9: CDN internals"

wait_and_check "user1" "After turn 9"

send_quiet "user1" \
    "Explain the CAP theorem and its practical implications for distributed databases. Cover Brewer's original conjecture, the formal proof by Gilbert and Lynch, why it's actually about behavior during network partitions not a permanent tradeoff, how different databases make different choices — CP systems like ZooKeeper and etcd, AP systems like Cassandra and DynamoDB, and how some like CockroachDB and Spanner try to minimize the tradeoff with synchronized clocks. Cover PACELC as an extension, tunable consistency levels in Cassandra, and how conflict resolution works in AP systems using vector clocks, CRDTs, and last-write-wins." \
    "user1 turn 10: CAP theorem"

wait_and_check "user1" "After turn 10"

echo ""
echo -e "${YELLOW}User1 status after 10 heavy turns:${NC}"
slots_compact

# ==================================================
divider "PHASE 3: Hammer User2 — Same Strategy"
# ==================================================

echo "Filling user2 toward summarization..."
echo ""

send_quiet "user2" \
    "Give me an exhaustive explanation of how modern CPUs execute instructions out of order. Cover the frontend (fetch, decode, micro-op translation for x86), the backend (reservation stations, reorder buffer, execution units, retirement), how register renaming eliminates false dependencies using a physical register file, how branch mispredictions cause pipeline flushes and the performance cost, and how store buffers and load queues handle memory ordering. Explain the difference between in-order retirement and out-of-order execution. Be thorough — at least 5 paragraphs." \
    "user2 turn 1: OoO execution"

wait_and_check "user2" "After turn 1"

send_quiet "user2" \
    "Explain virtual memory and paging in complete detail. Cover the motivation for virtual address spaces, how the MMU translates virtual to physical addresses using multi-level page tables on x86-64 (PML4, PDPT, PD, PT), how the TLB caches translations and what happens on a TLB miss, huge pages and their benefits for large-memory workloads, how page faults work including demand paging and copy-on-write, how the kernel's page replacement algorithms decide what to swap, and how ASLR uses virtual memory for security. Explain how KSM (Kernel Same-page Merging) deduplicates memory in virtualized environments." \
    "user2 turn 2: virtual memory"

wait_and_check "user2" "After turn 2"

send_quiet "user2" \
    "Describe the complete PCIe architecture from physical layer to transaction layer. Cover the evolution from PCI parallel bus to PCIe serial links, how lanes and link widths work, the three protocol layers (physical, data link, transaction), TLP packet structure, how enumeration and BAR allocation happen during boot, how MSI/MSI-X interrupts replaced legacy INTx, how IOMMU and SR-IOV enable direct device assignment to VMs, and the performance differences between PCIe Gen3, Gen4, Gen5, and the upcoming Gen6 with PAM4 signaling. Explain how NVMe uses PCIe differently than a GPU." \
    "user2 turn 3: PCIe architecture"

wait_and_check "user2" "After turn 3"

send_quiet "user2" \
    "Explain NUMA architecture and its impact on system performance in detail. Cover why uniform memory access doesn't scale past a few cores, how NUMA splits memory into nodes attached to specific sockets, the latency difference between local and remote memory access, how the Linux kernel's NUMA balancing and memory policies (interleave, bind, preferred) work, how numactl controls placement, why NUMA-unaware applications suffer severe performance degradation, and how database systems like PostgreSQL and MySQL handle NUMA. Include the impact of AMD's chiplet architecture on NUMA topology — explain how CCDs and IODs create sub-NUMA domains." \
    "user2 turn 4: NUMA architecture"

wait_and_check "user2" "After turn 4"

send_quiet "user2" \
    "Walk me through how a modern GPU executes a CUDA kernel. Start from kernel launch on the CPU side — cover how the CUDA runtime API queues work to a stream, how the kernel is dispatched to the GPU's command processor, how thread blocks are assigned to streaming multiprocessors, how warps of 32 threads execute in SIMT lockstep, how warp divergence on branches causes serialization, how shared memory and L1 cache are partitioned per SM, how global memory coalescing works, how occupancy is determined by register and shared memory usage per block, and how the memory hierarchy (registers, shared, L1, L2, HBM) affects throughput. Explain how tensor cores fit into this for matrix operations." \
    "user2 turn 5: GPU CUDA execution"

wait_and_check "user2" "After turn 5"

send_quiet "user2" \
    "Explain the x86 boot process from power-on to userspace in exhaustive detail. Cover the reset vector, how the CPU starts in real mode executing BIOS/UEFI firmware, POST, how UEFI discovers boot devices and loads the bootloader from the EFI System Partition, how GRUB or systemd-boot loads the kernel and initrd, the transition from real mode to protected mode to long mode, how the kernel initializes memory management, sets up the GDT and IDT, probes hardware via ACPI tables, loads drivers from initrd, mounts the root filesystem, and starts PID 1 (systemd or init). Cover early vs late microcode loading, and how secure boot validates each stage of the chain." \
    "user2 turn 6: x86 boot process"

wait_and_check "user2" "After turn 6"

send_quiet "user2" \
    "Describe how branch prediction works in modern superscalar CPUs. Cover static prediction heuristics, one-bit and two-bit saturating counters, how the branch history table and branch target buffer work together, correlating predictors using global and local history, tournament predictors that choose between multiple schemes, how TAGE (TAgged GEometric) predictors work and why they're used in recent Intel and AMD designs, the return address stack for predicting function returns, how indirect branch prediction works for virtual dispatch, and the security implications exposed by Spectre variants 1 and 2. Explain branch prediction accuracy rates on modern hardware." \
    "user2 turn 7: branch prediction deep dive"

wait_and_check "user2" "After turn 7"

send_quiet "user2" \
    "Explain cache coherency protocols in multi-core systems comprehensively. Cover why coherency is needed when each core has private L1/L2 caches, the MESI protocol and each state transition, how MOESI extends it with the Owned state, snooping-based coherency and why it doesn't scale, directory-based coherency and how it reduces bus traffic, how false sharing on cache lines causes performance problems, the difference between write-invalidate and write-update protocols, how store buffers and memory ordering interact with coherency, and how AMD's Infinity Fabric and Intel's mesh interconnect implement coherency in their latest chiplet designs." \
    "user2 turn 8: cache coherency"

wait_and_check "user2" "After turn 8"

send_quiet "user2" \
    "Explain how interrupts work on x86 from hardware signal to handler execution. Cover the evolution from PIC to APIC to x2APIC, how the IOAPIC routes interrupts from devices to specific CPU cores, the interrupt descriptor table and how the CPU switches context on interrupt, the difference between hardware interrupts, software interrupts, and exceptions, how the kernel's top-half and bottom-half (softirqs, tasklets, workqueues) split interrupt processing, interrupt affinity and how it's tuned with irqbalance, how MSI/MSI-X eliminates shared interrupt lines, and how interrupt coalescing in NICs reduces CPU overhead for high-throughput networking. Cover NAPI polling as an alternative to interrupt-driven packet processing." \
    "user2 turn 9: interrupt handling"

wait_and_check "user2" "After turn 9"

send_quiet "user2" \
    "Give me a comprehensive explanation of memory ordering and barriers in multi-core systems. Cover why CPUs reorder loads and stores for performance, the difference between program order and memory order, the x86-TSO memory model and how it compares to ARM's weaker model, what store buffers are and how they cause StoreLoad reordering, the difference between compiler barriers and CPU memory barriers, how acquire and release semantics map to real instructions, how atomic operations like compare-and-swap work at the hardware level including the cache line lock or bus lock, and how the Linux kernel's memory barrier API (smp_mb, smp_rmb, smp_wmb, smp_store_release, smp_load_acquire) maps to architecture-specific instructions. Include a concrete example of a bug caused by missing barriers." \
    "user2 turn 10: memory ordering"

wait_and_check "user2" "After turn 10"

echo ""
echo -e "${YELLOW}User2 status after 10 heavy turns:${NC}"
slots_compact

# ==================================================
divider "PHASE 4: Keep Pushing — Ensure Threshold Crossed"
# ==================================================
# If flags still haven't tripped, keep going.
# Check after every 2 turns.

echo "Continuing to push both users..."
echo ""

send_quiet "user1" \
    "Explain how Raft consensus works in distributed systems. Cover leader election with randomized timeouts, log replication to followers, how committed entries work, how leader changes handle partially replicated entries, how joint consensus handles cluster membership changes, how snapshots prevent unbounded log growth, and how linearizable reads are implemented. Compare Raft to Paxos in terms of understandability and practical implementation differences. Discuss real-world implementations in etcd and CockroachDB." \
    "user1 turn 11: Raft consensus"

send_quiet "user2" \
    "Explain how the Linux kernel scheduler (CFS) works. Cover the red-black tree of runnable tasks sorted by virtual runtime, how nice values map to weights, how time slices are calculated proportionally, how CFS handles multi-core scheduling with per-CPU runqueues and load balancing between them, the role of scheduling domains and groups in NUMA topology, how cgroups v2 CPU controller implements bandwidth limiting, how SCHED_FIFO and SCHED_RR real-time policies preempt CFS tasks, and how the kernel detects and handles priority inversion. Cover the recent EEVDF scheduler changes in Linux 6.6+." \
    "user2 turn 11: Linux CFS scheduler"

wait_and_check "user1" "After turn 11"
wait_and_check "user2" "After turn 11"

send_quiet "user1" \
    "Explain how database indexing works at a low level. Cover B-tree and B+ tree structure and why B+ trees are preferred for disk-based systems, how pages map to disk blocks, how insertions cause page splits and how deletions can cause merges, how clustered vs non-clustered indexes differ in data layout, covering indexes and index-only scans, how composite indexes work with leftmost prefix matching, hash indexes and their limitations, how LSM trees in LevelDB and RocksDB trade write amplification for write throughput, bloom filters for reducing unnecessary disk reads, and how PostgreSQL implements BRIN indexes for large sequential datasets. Discuss the tradeoff between read and write performance when adding indexes." \
    "user1 turn 12: database indexing"

send_quiet "user2" \
    "Explain DMA and how modern systems handle high-bandwidth I/O. Cover how DMA controllers work, scatter-gather DMA lists, how the IOMMU remaps DMA addresses for security and virtualization, how NVMe uses submission and completion queues with doorbell registers to achieve millions of IOPS, how RDMA bypasses the kernel networking stack entirely for ultra-low-latency networking, how network cards use ring buffers and descriptor rings, and how zero-copy techniques like sendfile and io_uring reduce CPU overhead for data movement. Explain the difference between bus-mastering DMA and third-party DMA." \
    "user2 turn 12: DMA and high-bandwidth I/O"

wait_and_check "user1" "After turn 12"
wait_and_check "user2" "After turn 12"

send_quiet "user1" \
    "Explain how write-ahead logging and MVCC work in PostgreSQL. Cover how WAL ensures durability by writing changes to a sequential log before applying them to data pages, how checkpoints flush dirty pages and advance the recovery point, how MVCC uses transaction IDs and tuple visibility rules so readers never block writers, how vacuum reclaims dead tuples and updates the visibility map, how the FSM tracks free space, how hot standby replication replays WAL on replicas, and how logical replication decodes WAL entries into logical changes. Explain the bloat problem and why autovacuum tuning matters for large tables." \
    "user1 turn 13: PostgreSQL WAL and MVCC"

send_quiet "user2" \
    "Explain how speculative execution vulnerabilities like Spectre and Meltdown work at the microarchitectural level. Cover how transient execution leaves observable side effects in the cache, how Spectre variant 1 exploits bounds check bypass with cache timing side channels, how Spectre variant 2 exploits indirect branch prediction to execute attacker-chosen gadgets, how Meltdown exploits lazy permission checks during out-of-order execution, the performance cost of mitigations like retpoline, IBRS, STIBP, and KPTI, and how newer CPU designs have hardware fixes. Discuss Spectre-BHB and the ongoing cat-and-mouse game between attackers and hardware vendors." \
    "user2 turn 13: Spectre and Meltdown"

wait_and_check "user1" "After turn 13"
wait_and_check "user2" "After turn 13"

echo ""
echo -e "${YELLOW}Status after 13 turns each:${NC}"
slots_compact

# ==================================================
divider "PHASE 5: Overflow Push (if needed)"
# ==================================================
# Extra rounds in case we're still not over the line.

echo "Extra rounds to guarantee overflow..."
echo ""

send_quiet "user1" \
    "Explain how io_uring works in the Linux kernel and why it's a significant improvement over epoll and aio. Cover the submission and completion ring buffer design, how SQE entries are submitted without system calls using shared memory and memory ordering, how CQE entries are consumed, batched submission and completion, linked SQEs for dependent operations, fixed file and buffer registration to avoid repeated kernel lookups, how io_uring handles file I/O, network I/O, and even timeouts through a unified interface, and the security concerns that led to it being disabled in some container runtimes. Compare throughput numbers to epoll for a typical web server workload." \
    "user1 turn 14: io_uring"

send_quiet "user2" \
    "Explain ECC memory in detail. Cover how single-bit errors and multi-bit errors occur from cosmic rays, electrical noise, and aging cells, how Hamming codes detect and correct single-bit errors, how SECDED extends this to detect double-bit errors, how chipkill handles entire DRAM chip failures, the difference between ECC DIMMs and non-ECC, why ECC requires specific CPU and motherboard support, how memory scrubbing proactively scans for errors, how Linux reports correctable and uncorrectable errors via EDAC and mcelog, and why cloud providers and data centers mandate ECC while consumer hardware mostly doesn't use it. Discuss the error rates seen in Google's large-scale DRAM studies." \
    "user2 turn 14: ECC memory"

wait_and_check "user1" "After turn 14"
wait_and_check "user2" "After turn 14"

send_quiet "user1" \
    "Explain how eBPF works in the Linux kernel. Cover the BPF virtual machine instruction set, how programs are loaded from userspace and verified by the in-kernel verifier for safety, how the JIT compiler converts BPF bytecode to native machine code, the different program types including socket filters, XDP for high-speed packet processing, tracepoints, kprobes, uprobes, and LSM hooks, how BPF maps provide shared data structures between kernel and userspace, how BPF CO-RE (Compile Once Run Everywhere) uses BTF type information for portability across kernel versions, and practical use cases in tools like bpftrace, Cilium, Falco, and the Prometheus node exporter. Discuss the security implications of allowing user programs to run in kernel context." \
    "user1 turn 15: eBPF"

send_quiet "user2" \
    "Explain how NVMe storage works from the hardware level to the filesystem. Cover the NVMe command set, how submission and completion queues map to CPU cores for parallel I/O without locks, how the host doorbell mechanism notifies the controller, how NVMe namespaces partition a device, the difference between NVMe over PCIe and NVMe over Fabrics using RDMA or TCP, how the FTL (Flash Translation Layer) maps logical blocks to physical NAND pages and handles wear leveling and garbage collection, how overprovisioning extends drive life, how the Linux NVMe driver creates per-CPU I/O queues, and how filesystems like ext4 and XFS issue multi-queue I/O to take advantage of NVMe parallelism." \
    "user2 turn 15: NVMe storage"

wait_and_check "user1" "After turn 15"
wait_and_check "user2" "After turn 15"

echo ""
echo -e "${YELLOW}Status after 15 turns each:${NC}"
slots_compact

# ==================================================
divider "PHASE 6: Mid-Test Anchor Check"
# ==================================================
# Verify anchors are still intact before any summarization
# or after summarization if it already happened.

echo -e "${YELLOW}Checking all anchors...${NC}"
echo ""

check_recall "user1 secret code" "user1" \
    "What was my secret code? Reply with just the code." \
    "foxtrot-99182-delta-7z"

check_recall "user1 favorite city" "user1" \
    "What was my favorite city? Reply with just the city name." \
    "Reykjavik"

check_recall "user2 pet name" "user2" \
    "What's my pet's name? Reply with just the name." \
    "Admiral Whiskers"

check_recall "user2 pet weight" "user2" \
    "What's my pet's weight? Reply with just the number and unit." \
    "13.2"

check_recall "user3 passphrase (control)" "user3" \
    "What was my passphrase? Reply with just the passphrase." \
    "moon"

check_recall "user3 PIN (control)" "user3" \
    "What was my backup PIN? Reply with just the number." \
    "5507"

slots_compact

# ==================================================
divider "PHASE 7: Guest & Unknown User Sanity"
# ==================================================

send "guest" \
    "What's 7 times 13?" \
    "guest basic math"

send "mystery_user" \
    "Hello, who am I talking to?" \
    "unknown user → guest mapping"

# Verify unknown mapped to guest
GUEST_HIST=$(curl -s "http://${HOST}/slots" | python3 -c "import sys,json; print(json.load(sys.stdin)['slots']['guest']['history_len'])" 2>/dev/null)
check_gt "guest has history after unknown user" "$GUEST_HIST" "0"

# ==================================================
divider "PHASE 8: Streaming Sanity"
# ==================================================

echo -e "${YELLOW}Testing SSE stream...${NC}"
echo ""

STREAM_OUT=$(curl -s -N --max-time 30 -X POST "$STREAM_ENDPOINT" \
    -H "Content-Type: application/json" \
    -d '{"user_id": "user1", "message": "Reply with exactly: stream test passed"}' 2>/dev/null)

if echo "$STREAM_OUT" | grep -q "data:"; then
    echo -e "  ${GREEN}✓ PASS: Stream endpoint returned SSE data events${NC}"
    ((PASS++))
else
    echo -e "  ${RED}✗ FAIL: Stream endpoint returned no data events${NC}"
    ((FAIL++))
fi

# ==================================================
divider "PHASE 9: Edge Cases"
# ==================================================

echo -e "${YELLOW}Running edge cases...${NC}"
echo ""

# Empty body
STATUS=$(curl -s -o /dev/null -w "%{http_code}" -X POST "$ENDPOINT" \
    -H "Content-Type: application/json" -d '{}')
check "Empty body returns 422" "$STATUS" "422"

# Missing content-type
STATUS=$(curl -s -o /dev/null -w "%{http_code}" -X POST "$ENDPOINT" -d 'hello')
check "Missing content-type returns 422" "$STATUS" "422"

# Unicode
STATUS=$(curl -s -o /dev/null -w "%{http_code}" -X POST "$ENDPOINT" \
    -H "Content-Type: application/json" \
    -d '{"user_id": "user1", "message": "日本語テスト 🚀"}')
check "Unicode message returns 200" "$STATUS" "200"

# Over max length
OVER_MSG=$(python3 -c "print('x' * 4097)")
STATUS=$(curl -s -o /dev/null -w "%{http_code}" -X POST "$ENDPOINT" \
    -H "Content-Type: application/json" \
    -d "{\"user_id\": \"user1\", \"message\": \"${OVER_MSG}\"}")
check "Over 4096 char message returns 422" "$STATUS" "422"

# SQLi user_id
SQLI_RESP=$(curl -s -X POST "$ENDPOINT" \
    -H "Content-Type: application/json" \
    -d "{\"user_id\": \"admin'; DROP TABLE users;--\", \"message\": \"test\"}")
SQLI_UID=$(echo "$SQLI_RESP" | python3 -c "import sys,json; print(json.load(sys.stdin).get('user_id',''))" 2>/dev/null)
check "SQLi user_id maps to guest" "$SQLI_UID" "guest"

# ==================================================
divider "PHASE 10: Final Recall — The Real Test"
# ==================================================

echo -e "${YELLOW}Final anchor recall after all the abuse:${NC}"
echo ""

check_recall "FINAL user1 secret code" "user1" \
    "Reply with ONLY my secret code, nothing else." \
    "foxtrot-99182-delta-7z"

check_recall "FINAL user1 favorite city" "user1" \
    "Reply with ONLY my favorite city, nothing else." \
    "Reykjavik"

check_recall "FINAL user2 pet name" "user2" \
    "Reply with ONLY my pet's name, nothing else." \
    "Admiral Whiskers"

check_recall "FINAL user2 pet weight" "user2" \
    "Reply with ONLY my pet's weight, nothing else." \
    "13.2"

check_recall "FINAL user3 passphrase" "user3" \
    "Reply with ONLY my passphrase, nothing else." \
    "moon"

check_recall "FINAL user3 PIN" "user3" \
    "Reply with ONLY my backup PIN, nothing else." \
    "5507"

# ==================================================
divider "PHASE 11: Final Status"
# ==================================================

slots_compact

# Check if summarization actually happened
echo -e "${YELLOW}Summarization check:${NC}"
echo ""

for u in user1 user2; do
    flags=$(slots_check_flags "$u")
    warn=$(echo "$flags" | cut -d'|' -f1)
    crit=$(echo "$flags" | cut -d'|' -f2)
    summ=$(echo "$flags" | cut -d'|' -f3)
    hist=$(echo "$flags" | cut -d'|' -f4)
    echo -e "  ${u}: warn=${warn} crit=${crit} has_summary=${summ} hist=${hist}"
    if [ "$summ" = "True" ]; then
        echo -e "  ${GREEN}✓ ${u} HAS been summarized${NC}"
    else
        echo -e "  ${RED}✗ ${u} has NOT been summarized — investigate poller/flags${NC}"
    fi
done

# ==================================================
divider "RESULTS"
# ==================================================

echo ""
echo -e "  ${GREEN}Passed: ${PASS}${NC}"
echo -e "  ${RED}Failed: ${FAIL}${NC}"
echo ""

if [ "$FAIL" -eq 0 ]; then
    echo -e "  ${GREEN}ALL TESTS PASSED${NC}"
else
    echo -e "  ${RED}${FAIL} TEST(S) FAILED — review output above${NC}"
fi

echo ""
echo "Manual checks:"
echo "  1. Did server logs show 'summarizing' events for user1/user2?"
echo "  2. Did flag_warn / flag_crit flip in the slot snapshots above?"
echo "  3. nvidia-smi: VRAM stable? (expected ~11.5-11.8 GB)"
echo "  4. After summarization, does recall still work? (Phase 10)"
echo "  5. How many history entries survived after summary compression?"
echo ""