# p-lanes

A modular pipeline orchestrator for llama.cpp, built for household-scale AI — low-latency, multi-user, multi-channel, with per-user pinned GPU slots.

[![License: AGPL v3](https://img.shields.io/badge/License-AGPL%20v3-blue.svg)](https://www.gnu.org/licenses/agpl-3.0)
[![Python 3.12+](https://img.shields.io/badge/python-3.12+-blue.svg)](https://www.python.org/downloads/release/python-3120/)

---

## What is p-lanes?

p-lanes is a lightweight orchestrator for local AI, designed specifically for consumer-grade hardware with a small, fixed set of users. While most frameworks optimize for a fluctuating user base, p-lanes focuses on minimizing latency and overhead for a dedicated home system.

The project was built out of frustration with existing software that didn't fit the "Household Scale" goal:
- **Dedicated Identity:** Named users with unique personas, unique system privileges, and persistent memory.
- **Modular Recovery:** Drop-in architecture — heavy tinkering doesn't require re-coding the system. Modules and providers are self-contained files.
- **The "Instant-On" Goal:** Sub 2-second latency when a user activates the assistant, even after a full day of inactivity.

---

## The Philosophy: Why p-lanes?

Frameworks like Aphrodite or vLLM are built for enterprise-scale throughput — serving hundreds of concurrent users. In a household environment, that design punishes consumer hardware.

Most frameworks handle idle conversation memory (KV Cache) in one of three ways:
- **Discard:** History thrown away to save space. Full re-tokenization on next request (2–10+ seconds).
- **Swap to RAM:** Significant RAM burden (~4GB per 32k window, uncompressed) and latency on context reload.
- **Swap to SSD:** SSD wear and moderate latency (~1–3 seconds).

p-lanes uses llama.cpp as a lightweight foundation and locks it into a **reserved seat** configuration. Users are pinned to dedicated VRAM slots. Memory stays on the GPU. The assistant is always warm.

---

## Key Features

- **Deterministic Slot Mapping:** Users are assigned permanent VRAM slots. Context persists across sessions in-memory without re-tokenization. Slot count and context window size are configurable.
- **Utility Lane:** A dedicated ephemeral slot for background LLM tasks — summarization, structured command parsing, response verification — without dirtying user conversation history. Gracefully falls back to the user slot when unavailable.
- **Automatic Summarization:** Compresses conversation history when slots fill up. Two modes: async (utility lane — user keeps talking while it runs) and in-place (slot lock, brief wait). Emergency trim fallback if the LLM call fails. Scheduled daily summary + optional llama-server restart.
- **Multi-Channel I/O:** Voice and text channels simultaneously, all feeding the same pipeline and the same user slot. Voice uses sentence-buffered streaming TTS so audio starts before the full response is generated.
- **Live Broadcast Stream:** Optional SSE endpoint that mirrors token output in real time. Connect a dashboard to watch voice conversations appear as text.
- **Two-Gate Security Model:** Layered access control that never trusts the LLM. Gate 1 verifies identity at the transporter. Gate 2 enforces per-module permission levels declared in config. Each gate can only raise the bar, never lower it.
- **Drop-In Architecture:** Modules are single `.py` files. Providers are subdirectories. Drop them in, restart, and the system auto-discovers them. No manifest files. No manual registration.
- **Semantic Intent Routing:** Embedding-based classifier with configurable intent buckets, dual confidence thresholds, and per-intent temperature overrides — all in YAML, no code changes to tune.
- **RAG Pipeline:** ChromaDB + BGE-M3 embeddings. Shared topic collections plus per-user private collections. Header-aware markdown chunking. Ingest triggered on startup or via admin endpoint.
- **Web Search:** SearXNG + optional Jina Reader for full-page content on thin snippets. `local_search` intent tries RAG first and falls back to web only if RAG is empty.
- **Home Assistant Integration:** Device control (domain-level cumulative permissions), sensor state queries, area-based entity resolution.
- **Background Response Verification:** Non-blocking Gemini Flash check on eligible intents. PII gate runs first via utility slot. Flags written to disk for review — response is never modified.
- **Persistent SQLite Store:** Shared aiosqlite database with WAL mode, foreign keys, and versioned migrations. Domain tables for health, household, finances, components, and environment data.
- **Scheduled Jobs:** Cron-based background tasks (nightly web crawler, daily summarization). Idle-gating and duration caps supported.
- **Minimalist Overhead:** Headless, transparent code designed for 24/7 reliability on consumer hardware.

---

## Limitations

p-lanes makes intentional trade-offs to achieve deterministic low-latency. It is a specialized household tool, not a general-purpose engine.

- **Always-On Core:** User slots are persistent processes. There is no load-on-request mechanism for primary slots — once the core is up, VRAM for those users is pinned.
- **Linear Slot Division:** VRAM is divided equally across all active slots. Different context window sizes per user are not supported in a single instance.
- **Hard Slot Boundaries:** Each user is locked to their allocated memory. Context cannot overflow into another user's slot — the summarizer must compress before the slot fills.
- **Static VRAM Pre-allocation:** Once slots are pinned, that VRAM is reserved. It cannot be reclaimed dynamically without stopping the service.
- **Compute Contention:** GPU cores are shared. Simultaneous requests from multiple users will divide tokens-per-second across those active requests.
- **Hardware Ceiling:** VRAM is the hard limit. Performance is tied to what physically fits on the GPU.

---

## Pipeline

Every request flows through the same staged pipeline regardless of input channel:

```
Channel → Transporter → Classifier → Enricher → LLM Processor → Responder → Finalizer → Channel
```

**Transporter:** Normalizes input (text or voice) into a `MessageEnvelope`. Enforces Gate 1 (identity check). Unknown users are mapped to the guest slot if enabled, or rejected.

**Classifier:** Intent classification. Identifies what the user is asking (`device_control`, `ha_sensor`, `web_search`, `local_search`, `general`, etc.) and sets the intent on the pipeline context. Modules registered to this phase also handle early short-circuit cases (greetings, flagging bad responses).

**Enricher:** Context injection. Modules gather data and write it into the pipeline context — HA sensor state, RAG results, web search results, resolved entity lists. Modules that fully handle a request (e.g. device control) set a `skip_processor` flag here, bypassing the LLM.

**LLM Processor:** The kernel calls the user's pinned llama.cpp slot with a prompt built from the enrichments. If `skip_processor` is set, this step is skipped entirely. Handles context overflow with emergency summarization and retry.

**Responder:** Post-response actions. Currently: background Gemini hallucination verification (non-blocking — response is never delayed or modified).

**Finalizer:** Final output overrides. No modules currently registered.

---

## Channels

Channels are named I/O endpoints, each pairing an input method with an output method.

```
POST /channel/chat         → text in, JSON out
POST /channel/chat/stream  → text in, SSE token stream out
WS   /channel/voice        → audio in (STT), audio out (TTS)
```

All channels share the same pipeline, security gates, and user slots. A user speaking by voice and typing in chat maintains one continuous conversation history on the same slot.

Voice responses are sentence-buffered: TTS synthesis starts on the first complete sentence while the LLM continues streaming, reducing first-audio latency.

---

## Security Model

Two hard gates. The LLM never makes access decisions.

**Gate 1 — Identity (transporter):** Is this user known to the system? Unknown users resolve to guest (if enabled) or are rejected with a 403. This happens before any pipeline code runs.

**Gate 2 — Module Permission (dispatcher):** Can this user access this specific module? Each module has a minimum security level in `config.yaml`. The dispatcher silently skips any module the user isn't cleared for before calling it.

Security levels (0–4): GUEST, USER, POWER, TRUSTED, ADMIN. Level is set in `users.yaml` and is never read from user-writable profile files.

Device control uses a separate cumulative permission layer: level N grants all device domains listed at levels ≤ N (lights/switches at 1, climate/covers at 2, locks at 3).

---

## Requirements

**Theoretical minimums:**
- A GPU (CPU-only inference works in llama.cpp but p-lanes is designed around GPU-pinned slots)
- Python 3.12+
- llama.cpp server build
- Linux OS

**Optional integrations:**
- Whisper-compatible STT server for voice channel
- Kokoro-compatible TTS server for voice channel
- Home Assistant for device control and sensor queries
- SearXNG instance for web search
- Gemini API key for response verification

**Tested build:**
- CPU: Intel Core Ultra 7
- RAM: 32GB, SSD: 1TB NVMe
- GPU: NVIDIA RTX 5060 Ti (16GB)
- OS: Proxmox VE (bare-metal), HAOS on VM, LXC container for p-lanes + llama.cpp

See [Architecture.md](Architecture.md) for full system details, pipeline internals, and contributor rules.

---

## Version History

- **v0.1.0:** Monolithic structure. Proven concept with full text-chat functionality.
- **v0.2.0:** Ported to Python package format. Core logic separated from modules.
- **v0.3.0:** Drop-in component architecture. Modules and providers self-contained with auto-discovery and isolated config files.
- **v0.4.0:** Core hardening (utility lane, summarization fixes, security gate fixes, circular import refactor). Voice pipeline (Whisper STT, Kokoro TTS, WebSocket `/channel/voice`). HAOS custom integration.
- **v0.5.0:** Provider isolation — each provider is a fully self-contained package. `MessageEnvelope` normalized input contract across all channels. `device_id` return address for output routing. `TranscribeResult` replaces bare string from STT providers.
- **v0.6.0 (current):** SQLite persistent store — shared aiosqlite database, WAL mode, foreign keys, versioned migrations, 8 domain tables. Architecture and documentation overhaul.

---

## Roadmap

### Voice & Identity
- [ ] Voiceprint enrollment — capture and store per-user voiceprint embeddings
- [ ] Voiceprint runtime identification — speaker ID on incoming audio before slot routing; replaces or augments token-based user identification
- [ ] pyannote-audio in STT container — diarization support required for voiceprint pipeline

### Modules & Pipeline
- [ ] Media routing — route `media_control` intent to the active satellite based on `device_id`
- [ ] Timer persistence — route timers through HA timer entities so they survive service restarts
- [ ] Calendar module — intent bucket exists; no handler yet
- [ ] Admin mode module — text-based interactive sub-process for ADMIN users (slot state, ingest triggers, entity refresh, LLM restart); stateful command loop via `ctx.metadata`
- [ ] Utility lane priority queue — P1: active user requests, P2: multi-step background tasks, P3: low-priority jobs; fair scheduling for concurrent utility slot work

### RAG & Knowledge
- [ ] Notes module — LLM-triggered saves to user private RAG collection with validation
- [ ] Intake formatter — universal file intake pipeline (`/intake/pending/`); multi-pass work queue; handles text, markdown, images (vision LLM), PDF; explicit filename/sidecar metadata wins over LLM classification; feeds RAG ingest
- [ ] Shared network folder — SMB share for file drop from Windows machines into the intake pipeline
- [ ] RAG data expansion — populate `shared/` markdown files; QA-pair format for technical specs
- [ ] Game library scraper — Steam/GOG library → game wiki content → `shared_games` collection; two-stage retrieval (title index → section filter)
- [ ] BGE-M3 embedding service — evaluate separation from main process as embedding load grows

### Health & Data
- [ ] Garmin biometric integration — pull daily metrics (HRV, sleep, steps, stress) from Garmin Connect API into SQLite
- [ ] Plant photo health module — multimodal: photo input → vision pipeline → species ID, health assessment, care recommendations
- [ ] Financial file analyzer — bank statements and receipts → categorized spending → graph-ready output; ADMIN-only, utility slot

### Infrastructure
- [ ] Remote access + mobile UI — Cloudflare Tunnel → HAOS → p-lanes bridge; SSE token stream for mobile; Android web wrapper; HAOS user ID mapping to p-lanes security slots
- [ ] ChromaDB → Qdrant migration — RAM tiering and scalar quantization for larger RAG collections
- [ ] Samba config service — FastAPI wizard for SMB share management; smb.conf writer, smbpasswd wrapper, share/user management endpoints

---

## License & Attribution

**p-lanes** is licensed under the **GNU AGPL-3.0**.

This is a Copyleft project: you are free to modify and share it, but any derivative works must also be open-source, keep all original author attributions, and be licensed under the AGPL.

### Third-Party Components

p-lanes is an orchestrator. It does not bundle or depend on any specific STT or TTS engine — these are swappable providers. The following are commonly used with p-lanes:
- **llama.cpp**: MIT License (required — inference backend)
- **Whisper**: MIT License (default STT provider, swappable)
- **Kokoro**: Apache 2.0 License (default TTS provider, swappable)

*Original Author: Logicish (2026)*
