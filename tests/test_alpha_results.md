# Alpha Test: Slot Isolation & Baseline Performance
**Date:** March 2, 2026
**Build:** p-lanes v0.3.0 (Alpha)
**Hardware:** RTX 5060 Ti 16GB, Proxmox LXC with GPU passthrough
**Software:** llama.cpp (pinned), Qwen3-VL-8B-Instruct Q6_K, FastAPI/uvicorn

---

## Test Environment

| Component | Value |
| :--- | :--- |
| Model | Qwen3-VL-8B-Instruct (Q6_K quantization) |
| VRAM at idle | 12,446 MiB (model fully loaded, no active inference) |
| Slot config | 5 slots (4 user + 1 utility), hardcoded assignment |
| Total context | 61,440 tokens (12,288 per slot) |
| KV cache type | q8_0 |
| Test method | Sequential curl requests with 1s delay between calls |

---

## Latency & Token Counts

All requests were sent sequentially — no concurrent load.

| Test | User | Action | Response Time | Total Tokens | Output Length |
| :--- | :--- | :--- | :--- | :--- | :--- |
| 3 | user1 | Store a secret code | 0.61s | 109 | 99 chars |
| 4 | user1 | Short story request | 4.11s | 382 | 951 chars |
| 5 | user2 | Long technical guide | 8.76s | 627 | 2,532 chars |
| 6 | user3 | Long history essay | 8.79s | 632 | 1,995 chars |
| 7 | user1 | Recall secret code | 0.24s | 417 | 20 chars |
| 9 | guest | Identity question | 0.28s | 71 | 58 chars |
| 10 | guest | Unknown user mapped | 0.10s | 90 | 11 chars |

**Note on total_tokens:** This is the full context window usage as reported by llama.cpp — system prompt + all conversation history + the new response. It is not just the output token count. For example, test 7 shows 417 tokens for a 20-character reply because user1 had 6 messages of history loaded into the slot at that point. At 417/12,288, user1 was at roughly 3.4% of slot capacity.

---

## VRAM Behavior

| State | VRAM Usage | Delta from Idle |
| :--- | :--- | :--- |
| Pre-start (no llama-server) | 34 MiB | — |
| Model loaded, idle | 12,446 MiB | — |
| Peak during inference | 12,452 MiB | +6 MiB |
| Post-shutdown | 34 MiB | — |

The 6 MiB increase during inference is CUDA workspace allocation — consistent with previous tests. VRAM usage is effectively static regardless of how many slots are active or how many tokens are in flight. No OOM risk observed at this context size.

---

## Thermals & Power

| Metric | Idle | Peak (during test 5) | Post-test cooldown |
| :--- | :--- | :--- | :--- |
| Temperature | 33°C | 55°C | 42°C (within 15s) |
| Power draw | 7-8W | 171W | 24-30W |
| Fan speed | 51% | 51% | 51% |

GPU stayed well within thermal limits. Fan didn't ramp up.

---

## Slot Isolation Test

The core validation: user1 stored a secret, user2 and user3 each consumed 600+ tokens on their own slots, then user1 was asked to recall the secret.

- **Stored:** `alpha-03387-gamma-4b`
- **Recalled:** `alpha-03387-gamma-4b`
- **Recall latency:** 0.24s

No cross-slot contamination. User1's KV cache was untouched by user2/user3 activity.

Post-test slot status confirmed expected state:
- user1: 6 messages in history (3 request/response pairs)
- user2: 2 messages (1 pair)
- user3: 2 messages (1 pair)
- guest: 0 → 2 after tests 9 and 10 (unknown user correctly mapped to guest)
- utility: 0 (unused during test)

---

## What Worked

- Slot isolation held under sequential multi-user load
- KV cache persistence confirmed (0.24s warm recall vs 0.61s cold first message)
- Guest fallback resolved unknown users without error
- Gate 1 rejected nothing incorrectly, mapped correctly
- Clean startup and shutdown — no orphaned llama-server processes, VRAM fully released
- Profile save on shutdown captured all user state to disk
- Structured logging produced clean, parseable output for every request

---

## What Was NOT Tested

- **Concurrent requests:** All calls were sequential. llama.cpp with `--parallel 5` accepts concurrent requests but processes them round-robin on the GPU. Under simultaneous load, individual response times will increase proportionally. This needs a separate concurrency test.
- **Streaming endpoint:** Only `/channel/chat` (blocking) was tested. `/channel/chat/stream` (SSE) was not exercised.
- **Crash recovery:** LLM stayed healthy throughout. The reactive recovery in `call()` and the proactive health check in the background loop were not triggered.
- **Summarization:** No user approached token thresholds (highest was 632/12,288 = 5.1%). Neither flag_warn nor flag_crit fired.
- **Utility slot calls:** `call_utility()` was not exercised (no modules that use it exist yet).
- **LLM restart endpoint:** `/llm/restart` was not called.
- **Edge cases:** Empty messages, max-length messages, rapid repeated requests, malformed payloads, simultaneous same-user requests.
- **Modules:** No modules exist yet. The pipeline ran classifier/enricher/responder/finalizer phases with empty registries.

---

## Known Limitations

- **Sequential GPU processing:** The GPU handles one inference at a time regardless of slot count. Parallel slots provide memory isolation, not compute parallelism. Multi-user response times degrade linearly with concurrent load.
- **No install automation tested:** setup.py and systemd service creation were not part of this test run.
- **Single hardware config:** Only tested on one system (RTX 5060 Ti 16GB, Proxmox LXC). Behavior on other GPUs or container runtimes is unknown.
