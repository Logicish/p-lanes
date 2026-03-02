# 📊 Alpha Test: Slot Isolation & VRAM Data
**Date:** March 2, 2026  
**Hardware:** RTX 5060 Ti (16GB)  
**Software:** p-lanes v0.3.2 (Alpha) + llama.cpp

---

## 🛠 Test Environment

| Component | Details |
| :--- | :--- |
| **Model** | Qwen3-VL-8B-Instruct (Q6_K) |
| **VRAM Baseline** | 12.45 GB (Static) |
| **Config** | 5 Slots (4 User, 1 Utility) |
| **Total Context** | 61,440 Tokens |

---

## ⏱ Observed Latency
| Test | Action | Time | Tokens |
| :--- | :--- | :--- | :--- |
| **Test 3** | Secret Storage (User1) | **0.61s** | 109 |
| **Test 4** | 200-word Story (User1) | **4.11s** | 382 |
| **Test 5** | Heavy Technical Text (User2) | **8.76s** | 627 |
| **Test 6** | Heavy History Text (User3) | **8.79s** | 632 |
| **Test 7** | **Recall Secret (User1)** | **0.24s** | 417 |

---

## 📈 Hardware Realities
### VRAM Behavior
* **Static Usage:** 12,446 MiB.
* **Peak Usage:** 12,452 MiB.
* **Note:** The 6 MiB increase linked to CUDA housekeeping, ~6 MiB during all previous tests. The slots stayed pinned where they were told to stay.

### Thermals & Power

* **Temp:** Peaked at 55°C.
* **Power:** 175W.

---

## 🔍 Test 7: The Isolation Check
After User 2 and User 3 pushed ~1,300 tokens through the pipe, User 1 asked for their code back.

* **Input:** "Reply with the secret code I gave you earlier and nothing else."
* **Output:** `alpha-03387-gamma-4b`
* **Speed:** 0.24s.

User 1’s memory stayed intact despite the other two users trying to hog the buffer. No cross-talk detected.

---

## ✅ The Good & The Bad

### The Good
* **Warm starts work:** 0.24s response time suggests the KV cache didn't dump to RAM or disk.
* **VRAM is predictable:** It didn't bloat or OOM during the 500+ word generations.
* **Clean Exit:** It didn't leave orphan `llama-server` processes hanging around after shutdown.

### The Bad
* **Sequential bottlenecks:** Memory is isolated, but the GPU can still only think about one thing at a time. If everyone talks at once, the 0.24s goes out the window.
* **Manual Setup:** No install yet, modules not tested yet, only tested on one system, edge cases not tested, etc.
