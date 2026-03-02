# Stress Test: Summarization & Isolation Under Pressure
**Date:** March 2, 2026
**Build:** p-lanes v0.3.0 (Alpha)
**Hardware:** RTX 5060 Ti 16GB, Proxmox LXC with GPU passthrough
**Software:** llama.cpp (pinned), Qwen3-VL-8B-Instruct Q6_K, FastAPI/uvicorn

---

## Test Environment

Context was intentionally reduced to force summarization within a few exchanges.

| Component | Value |
| :--- | :--- |
| Model | Qwen3-VL-8B-Instruct (Q6_K quantization) |
| Slot config | 5 slots (4 user + 1 utility) |
| Total context | 5,120 tokens (1,024 per slot) |
| Summarization thresholds | warn: 70% (~716 tokens), crit: 80% (~819 tokens) |
| Summary budget | 10% of remaining context (~90 tokens) |
| Recent messages budget | 15% of remaining context (~134 tokens) |
| System header budget | 128 tokens (static) |
| KV cache type | q8_0 |
| Test method | Sequential curl requests with 1s delay between calls |

---

## Test Phases

### Phase 0-1: Baseline & Secret Storage

System started clean. User1 stored a secret code (`alpha-03387-gamma-4b`) and received confirmation in 0.40s at 97 tokens. All slots at zero history, no flags, no summaries.

### Phase 2: User2 — 5 Rounds of Heavy Technical Content

User2 was asked progressively detailed questions about combustion engines, electric motors, hybrid powertrains, hydrogen fuel cells, and a comparative ranking. Each response consumed the full 1,024-token context.

| Round | Topic | Response Time | Tokens | Summarization Triggered |
| :--- | :--- | :--- | :--- | :--- |
| 1 | Combustion engines | 8.72s | 593 | No |
| 2 | Electric motors | 6.61s | 1,024 (crit) | Yes — completed in 1.69s |
| 3 | Hybrid powertrains | 7.98s | 1,024 (crit) | Yes — completed in 1.68s |
| 4 | Hydrogen fuel cells | 6.74s | 1,024 (crit) | Yes — completed in 1.68s |
| 5 | Comparative ranking | 7.98s | 1,024 (crit) | Yes — completed in 2.42s |

User2 triggered **four consecutive summarization cycles**. Each cycle: utility slot generated a summary (140-171 tokens), kept 1 recent message, cleared flags, saved profile. The incoming request waited for the slot lock and was served immediately after summarization completed. No 500s, no bricked state.

### Phase 3: User3 — 4 Rounds of Coffee Content

Same pattern, different topic domain.

| Round | Topic | Response Time | Tokens | Summarization Triggered |
| :--- | :--- | :--- | :--- | :--- |
| 1 | Seed to cup | 9.86s | 602 | No |
| 2 | Extraction chemistry | 6.68s | 1,024 (crit) | Yes — completed in 1.77s |
| 3 | Global economics | 8.59s | 1,024 (crit) | Yes — completed in 1.75s |
| 4 | Automated coffee farm | 6.77s | 1,024 (crit) | Yes — completed in 1.85s |

User3 triggered **three summarization cycles**. Same behavior: summarize, keep recent, clear flags, continue.

### Phase 4: Chaos Round — All Users

Quick-fire sanity checks after heavy summarization activity.

| User | Prompt | Response | Time |
| :--- | :--- | :--- | :--- |
| user1 | "What is 7 times 13?" | "91" | 0.13s |
| user2 | "What color is the sky?" | "Blue." | 0.25s |
| user3 | "What is the capital of Japan?" | "Tokyo." | 0.23s |
| guest | Rubber duck joke | Delivered a joke | 0.66s |
| randomstranger | "Do you know who I am?" | Mapped to guest, responded | 0.37s |

All users responsive. No lingering state corruption from summarization. Unknown user correctly mapped to guest slot.

### Phase 5: Post-Summarization Recall

The real question: do the summaries preserve enough context to remember what was discussed?

**User2** — asked to list the four propulsion systems discussed:
> "Internal Combustion Engine, Battery Electric Vehicle, Hybrid, Hydrogen Fuel Cell"

All four recalled correctly despite four summarization cycles compressing the conversation.

**User3** — asked to name three topics from the coffee discussion:
> "Automated Harvesting Robotics, Quality Sorting AI Systems, Sustainable Processing Integration"

Topics recalled from the most recent round (automated coffee farm). Earlier topics (seed to cup, chemistry, economics) were compressed into the summary — the model pulled from what was most recent in its window, which is expected behavior at this context size.

### Phase 6: Isolation Test

User1 was idle during all of phases 2-5 while 7 summarization cycles fired on other slots.

- **Stored:** `alpha-03387-gamma-4b`
- **Recalled:** `alpha-03387-gamma-4b`
- **Recall latency:** 0.25s

Slot isolation held. User1's KV cache was untouched.

### Phase 7: Final Slot Status

| User | History | Has Summary | Flags |
| :--- | :--- | :--- | :--- |
| user1 | 6 messages | No | Clean |
| user2 | 5 messages | Yes | Clean |
| user3 | 5 messages | Yes | Clean |
| guest | 4 messages | No | Clean |
| utility | 0 messages | No | Clean |

All flags cleared. Summaries persisted for user2 and user3. Utility slot stayed clean (no accumulated history from summarization calls).

### Phase 8: Edge Cases

| Input | Expected | Actual | Status |
| :--- | :--- | :--- | :--- |
| Single character ("?") | Valid response | Responded (20 chars) | Pass |
| "Buffalo" x500 | Handle or reject gracefully | Responded (20 chars, 709 tokens) | Pass |
| Missing message field | 422 validation error | `Field required` | Pass |
| Empty message | 422 validation error | `String should have at least 1 character` | Pass |
| Extra fields in payload | 422 rejection | `Extra inputs are not permitted` | Pass |
| GET on POST-only endpoint | 404 | `Route /channel/chat not found` | Pass |
| Nonexistent route | 404 | `Route /api/v2/chat not found` | Pass |

No 500 errors on any edge case. Pydantic validation caught all malformed payloads. The catch-all route handled method and path mismatches.

Note: the "Buffalo" x500 input consumed 709 tokens in a single message — over 69% of the slot's total context. The system accepted it and responded. At production context sizes this would be unremarkable; at 1,024 per slot it's a good test of how the system handles a single message that eats most of the available window.

---

## VRAM Behavior

| State | VRAM Usage | Delta from Idle |
| :--- | :--- | :--- |
| Pre-start (no llama-server) | 34 MiB | — |
| Model loaded, idle | 8,240 MiB | — |
| Peak during inference | 8,292 MiB | +52 MiB |
| Post-test idle | 8,292 MiB | +52 MiB |

VRAM at 5,120 total context is significantly lower than the 12,446 MiB observed at 61,440 context. The ~4.2 GB difference is entirely KV cache — confirming that context size is the primary VRAM cost after the model itself. The 52 MiB increase during inference is CUDA workspace allocation. No OOM risk.

---

## Thermals & Power

| Metric | Idle | Peak | Post-test cooldown |
| :--- | :--- | :--- | :--- |
| Temperature | 33°C | 74°C | 44°C (within 30s) |
| Power draw | 8-9W | 181W | 25-48W |
| Fan speed | 51% → 69% (ramped under sustained load) |

GPU ran hotter than the alpha test due to sustained back-to-back inference with summarization cycles creating additional load. Peak of 74°C is still well within safe operating range (throttle at ~83°C for this GPU). Fan ramped from 51% to 69% — first time fans responded to load in testing, indicating the longer sustained inference window compared to the lighter alpha test.

---

## Summarization Behavior

At 1,024 tokens per slot, every heavy response triggered crit and forced immediate summarization. This is expected — the context window is artificially small. Key observations:

**Summarization latency:** 1.68s to 2.42s per cycle. This is the time for the utility slot to receive the old messages, generate a compressed summary, and return it. Acceptable even under rapid-fire conditions.

**Summary quality:** Summaries were 140-171 tokens. At this context size, the summary budget is only ~90 tokens (10% of remaining), but the model slightly exceeded the `max_tokens` target. The summaries preserved enough context for users to recall major topics by name.

**Recent message retention:** Only 1 message kept per cycle (budget is 134 tokens, but a single model response at 1,500+ chars already exceeds that). At production context sizes (8k-12k per slot), the recent window would comfortably hold 4-10 message pairs.

**Prompt truncation:** Several summarization prompts were truncated before being sent to the utility slot (logged as `summarize_prompt_truncated`). The truncation kept the utility call within budget and avoided the 400 context overflow errors seen in the pre-fix test. This is the safety mechanism working as intended.

**Summarize-every-turn loop:** At 1,024 per slot, there's almost no headroom between post-summarization state (~30% full) and crit threshold (80%). One heavy response fills the gap, triggering another cycle. This would not occur at production sizes — the headroom would allow 15-30 exchanges between summarization events.

---

## What Worked

- Summarization completed successfully on every trigger (7/7 cycles)
- No emergency trims needed — utility slot handled all summarization requests
- No 500 errors across the entire test (previously: 7 consecutive 500s with bricked users)
- Post-summarization recall confirmed summaries preserve topical context
- Slot isolation held through 7 summarization cycles on adjacent slots
- Lock contention resolved cleanly — incoming requests waited for summarization and were served immediately after
- Edge cases produced proper 422/404 responses, not crashes
- Utility slot stayed clean (0 messages) — no history pollution from summarization calls
- Profile save fired after each summarization — state persisted to disk

## What This Test Does Not Prove

- **Production-size behavior:** At 8k-12k per slot, summarization would fire rarely and keep more recent messages. This test only validates the mechanism works — not that the experience is smooth at real sizes.
- **Concurrent load:** All requests were sequential. Under simultaneous multi-user load, summarization on one slot while another user is waiting could create noticeable latency.
- **Summary accumulation over time:** Each cycle here starts nearly fresh. In production, summaries would compound across many cycles — the quality of iterated summarization over hours or days of conversation is untested.
- **Streaming endpoint:** Only the blocking `/channel/chat` was tested. The SSE streaming path was not exercised.

---

## Comparison to Pre-Fix Test

The same stress test was run before the summarization budget fix. Results:

| Metric | Pre-Fix | Post-Fix |
| :--- | :--- | :--- |
| 500 errors | 7 | 0 |
| Bricked users | 2 (user2, user3 permanently unresponsive) | 0 |
| Summarization success | 0/2 attempts (utility slot overflow) | 7/7 |
| Post-summarization recall | Not possible (users bricked) | Both users recalled topics |
| User1 secret recall | Passed (unaffected slot) | Passed |

The fix: budget-aware prompt construction for the utility slot, with prompt truncation as a safety net and emergency trim as a last resort.
