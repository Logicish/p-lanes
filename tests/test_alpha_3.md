# p-lanes v0.3.0 — Summarization Stress Test Report

**Date:** 2026-03-03  
**Config:** 51,200 ctx_total / 5 slots / 10,240 per slot  
**Hardware:** RTX 5060 Ti 16GB, Qwen3-VL 8B Q6_K  
**Test runtime:** ~6 minutes  
**Automated result:** 26/26 passed  

---

## What Passed

**Summarization triggered and completed for both hammered users.** user1 hit `token_warn` at 7,688 tokens (turn 12), `token_critical` at 8,327 tokens (turn 13). user2 hit `token_warn` at 7,302 tokens (turn 11), `token_critical` at 8,595 tokens (turn 13). Both summaries completed, profiles saved, history compressed from 28 entries to 2 kept recent + summary.

**All anchor recalls survived summarization.** user1 recalled `foxtrot-99182-delta-7z` and `Reykjavik` post-summary. user2 recalled `Admiral Whiskers` and `13.2 pounds` post-summary. user3 (control, never summarized) recalled passphrase and PIN correctly throughout. 6/6 at mid-test, 6/6 at final recall.

**Clean startup confirmed.** All 5 profiles showed `profile_not_found_using_defaults` — no stale data from previous runs. All slots started at `history_len=0`, `has_summary=false`.

**Slot isolation held.** No cross-slot contamination. Unknown users (`mystery_user`, SQLi payload) mapped to guest correctly. Guest history shared across unknown users as expected.

**Edge cases clean.** Empty body → 422. Missing content-type → 422. Unicode → 200. Over-length → 422. SQLi user_id → mapped to guest. Zero 500s, zero unhandled exceptions.

**VRAM stable.** 11,690 MiB at model load → 11,724 MiB at peak. 34 MiB growth over the run, consistent with KV cache fill across active slots. No leak observed.

**GPU thermals acceptable.** Peaked at 75°C under sustained load at 180W TDP. Fan ramped to 77%. Cooled back to 42°C within ~60s of idle.

---

## What Needs Attention

### Slot Lock Contention During Summarization

This is the main finding. When both users hit `token_critical` at turn 13, the summarizer fired for each. The test script immediately sent turn 14 messages, which competed with the summarizer for slot access.

Timeline:
- **20:39:18** — user1 hits crit (8,327 tokens), summarizer starts
- **20:39:19** — user2's turn 13 request arrives, starts generating
- **20:39:41** — user2 hits crit (8,595 tokens), summarizer starts for user2
- **20:39:42** — user1 turn 14 arrives → `waiting_for_slot_lock` → **6s timeout** → request served on bloated context
- **20:39:50** — user2 turn 14 arrives → `waiting_for_slot_lock` → **6s timeout** → same problem
- **20:39:51** — user1 summary completes (33.21s on utility slot, 1,223 summary tokens)
- **20:40:24** — user2 summary completes (42.55s on utility slot, 1,250 summary tokens)

user2's summary took longer because user1's summary was occupying the utility slot first — user2 queued behind it.

The turns served during the lock timeout window ran on the full pre-summary context. Not catastrophic — everything recovered by turn 15 — but the system was in a degraded state for ~30-40 seconds where crit-flagged slots were still accepting messages without summary relief.

### Summarization Prompt Truncation

Both users triggered `summarize_prompt_truncated` warnings:
- user1: estimated 11,539 tokens vs 9,213 budget
- user2: estimated 11,842 tokens vs 9,213 budget

The summarizer couldn't fit the full conversation history into its prompt and had to truncate. Older context was lost during compression. The summaries still preserved anchors (proven by recall), but some mid-conversation detail was dropped. This is expected behavior at these context sizes, but worth noting that summaries are lossy.

### Response Time Degradation at High Token Counts

Response times scaled roughly linearly with slot token count:
- Turn 1 (~100-700 tokens): 0.5-8.8s
- Turn 10 (~6,400-6,600 tokens): 9.8-10.0s
- Turn 13 (~8,300-8,600 tokens): 10.0-10.3s
- During summarization contention (turn 14-15): 20-22s for user1, 10s for user2

The 20s+ response for user1 turn 14 was caused by the slot lock timeout (6s wait) plus generation on a bloated context. Post-summary responses dropped back to sub-1s for recall queries.

---

## Key Metrics

| Metric | Value |
|--------|-------|
| Total test duration | ~6 min |
| Automated tests | 26/26 passed |
| Summarization triggers | 2 (user1, user2) |
| user1 warn threshold hit | Turn 12, 7,688 tokens |
| user1 crit threshold hit | Turn 13, 8,327 tokens |
| user2 warn threshold hit | Turn 11, 7,302 tokens |
| user2 crit threshold hit | Turn 13, 8,595 tokens |
| user1 summary generation time | 33.21s |
| user2 summary generation time | 42.55s |
| Post-summary history (both users) | 2 kept recent + summary |
| Summary token size | ~1,223 (user1), ~1,250 (user2) |
| Slot lock timeouts | 2 (both 6s) |
| VRAM baseline | 11,690 MiB |
| VRAM peak | 11,724 MiB |
| GPU temp peak | 75°C |
| GPU power peak | 180W (TDP) |
| HTTP 500s | 0 |
| Unhandled exceptions | 0 |

---

## TODO

### 1. Block incoming messages during active summarization

**Problem:** When a slot hits `token_critical` and the summarizer starts, the slot still accepts new chat requests. These requests either timeout waiting for the lock or get served on the bloated pre-summary context, adding more tokens to an already over-budget slot.

**Fix:** When summarization is in-flight for a slot, reject or queue incoming messages for that user with a short backoff response (e.g., `{"status": "summarizing", "retry_after": 5}`). The caller can retry after a few seconds. In normal household use the odds of two users hitting crit simultaneously are low, and a few seconds of latency is an acceptable tradeoff for preventing the lock contention mess seen in this test.

**Scope:** Transport layer — gate the request before it reaches the LLM. Check a per-slot `is_summarizing` flag and return early if set.

### 2. Auto-clear guest history on idle

**Problem:** The guest slot accumulates history from all unknown/unmapped users. Over time (or across many casual interactions), this fills the guest slot context with irrelevant conversation fragments from different people.

**Fix:** During the existing `is_idle` background check cycle, if `guest.is_idle` evaluates true, clear the guest slot's history. Guest conversations are ephemeral by nature — there's no value in persisting them, and no profile to save a summary to.

**Scope:** Summarizer background loop — add a check alongside the existing idle detection. Something like:

```
if user_id == "guest" and slot.is_idle:
    slot.clear_history()
```

No summarization needed — just wipe it.

### 3. Investigate summary prompt budget vs actual context size

**Problem:** Both summarization runs hit `summarize_prompt_truncated`, meaning the conversation exceeded the summary prompt budget by 20-28%. The summaries still worked (anchors recalled), but information was silently dropped.

**Options:**
- Accept it — lossy compression is the point, and the important facts survived.
- Trigger summarization earlier (lower crit threshold) so there's less to compress.
- Increase the summary prompt budget if the utility slot can handle it.

Low priority — the current behavior is functional. But if users start complaining about forgotten mid-conversation details, this is where to look.
