# p-lanes — Architecture

> Authoritative architecture reference for p-lanes v0.5.0. When in doubt, this document decides.

---

## Table of Contents

- [Explain Like I'm 5](#explain-like-im-5)
- [Overview](#overview)
- [Hardware](#hardware)
- [Pipeline Diagram](#pipeline-diagram)
- [Package Structure](#package-structure)
- [Pipeline](#pipeline)
- [Security Model](#security-model)
- [Context Object](#context-object)
- [Providers](#providers)
- [Modules](#modules)
- [Core Components](#core-components)
- [Data Flow Examples](#data-flow-examples)
- [Rules for Contributors](#rules-for-contributors)

---

## Explain Like I'm 5

Imagine a house with a few people living in it. Each person can talk to an AI assistant — by voice in the kitchen, or by typing on a phone. The problem with most AI software is that it's slow: every time you talk to it, it has to reload your whole conversation from scratch. That takes seconds.

p-lanes fixes that by giving each person a **reserved seat in GPU memory**. Your conversation stays on the GPU between sessions. When you talk, the AI is already warm and ready — like a person sitting in the room waiting, not one who had to run in from outside.

When a message comes in, it travels through a **pipeline**: a chain of steps where each step does one job. First the system figures out what you're asking (intent). Then it gathers context — home sensor state, personal notes, live web results. Then the AI writes a response. Then the response is sent back as voice or text depending on how the message arrived.

Each step is a **module** — a plug-in file that can be added, removed, or swapped without touching the core. The core just runs whatever modules are registered; it doesn't care what they do.

There are **two security gates**. Gate 1 is at the door: if the system doesn't recognize you, nothing happens. Gate 2 is inside: each module declares the minimum security level needed to use it, and the dispatcher silently skips any module the user isn't cleared for. The AI never makes security decisions.

---

## Overview

p-lanes is a "microkernel" pipeline orchestrator for llama.cpp. It pins users to dedicated GPU KV cache slots for near-instant response, routes requests through a staged module pipeline, and integrates voice and text into one unified conversation per user.

**Core principles:**
- `main.py` is the kernel. It wires startup, runs the pipeline, and calls the LLM. It contains no feature logic.
- Modules and providers are drop-in files/folders. The core never imports them directly — they self-register at startup.
- The LLM is called by the kernel, not by modules. Modules set context; the kernel decides when to call.
- Security is two hard gates. The LLM never makes access decisions.
- Config is split: `config.py` owns core settings; each provider reads its own `config.yaml`.

---

## Hardware

Tested build:

| Component | Spec |
|---|---|
| GPU | NVIDIA RTX 5060 Ti 16GB |
| RAM | 32GB |
| Host | Proxmox VE (bare-metal), LXC container for p-lanes + llama.cpp |
| Model | Qwen3.5-9B-Q5_K_M.gguf + mmproj-Qwen3.5-9B-f16.gguf |
| KV Cache | 5 slots × 8,192 tokens (40,960 total), q8_0 compression |
| Flash Attention | Enabled |
| Reasoning blocks | Disabled at llama-server level |

---

## Pipeline Diagram

```
  INPUT
  ─────────────────────────────────────────────
  POST /channel/chat          WS /channel/voice
  POST /channel/chat/stream   (audio → STT → text)
         │                           │
         └─────────────┬─────────────┘
                       ▼
            ┌─────────────────────┐
            │     TRANSPORTER     │  core/transport.py
            │     [ GATE 1 ]      │  known user? → 403 if not
            └──────────┬──────────┘
                       │ MessageEnvelope
                       ▼
            ┌─────────────────────┐
            │   PipelineContext   │  core/pipeline.py
            └──────────┬──────────┘
                       │
       ┌───────────────▼───────────────┐
       │       CLASSIFIER PHASE        │
       │  [ Gate 2 per module ]        │
       │  ───────────────────────────  │
       │  flag_reply         [pri  5]  │
       │  semantic_router    [pri 50]  │ → sets ctx.intent
       │  hello_world        [pri 50]  │
       │  weather_query      [pri 50]  │
       │  timer              [pri 50]  │
       └───────────────┬───────────────┘
                       │
       ┌───────────────▼───────────────┐
       │        ENRICHER PHASE         │
       │  [ Gate 2 per module ]        │
       │  ───────────────────────────  │
       │  entity_enricher    [pri 10]  │ → ctx.metadata["resolved_entities"]
       │  device_control     [pri 20]  │ → may set skip_processor = True
       │  rag_enricher       [pri 30]  │ → ctx.enrichments (knowledge base)
       │  web_search         [pri 50]  │ → ctx.enrichments (web results)
       │  ha_query           [pri 50]  │ → ctx.enrichments (sensor state)
       └───────────────┬───────────────┘
                       │
               skip_processor?
           ┌──── Yes ──┴── No ──────┐
           │                        ▼
           │          ┌─────────────────────────┐
           │          │      LLM PROCESSOR       │  main.py kernel
           │          │   (pinned llama.cpp slot) │  llm.call() / llm.call_stream()
           │          │                           │  → ctx.response_text
           │          └────────────┬─────────────┘
           │                       │
           └───────────────────────┤
                                   ▼
       ┌───────────────────────────────────────┐
       │          RESPONDER PHASE              │
       │  [ Gate 2 per module ]                │
       │  ─────────────────────────────────    │
       │  response_verifier  [pri 50]          │ → background Gemini check
       └───────────────────────┬───────────────┘
                               │
       ┌───────────────────────▼───────────────┐
       │           FINALIZER PHASE             │
       │        (no modules registered)        │
       └───────────────────────┬───────────────┘
                               │ ctx.final_output or ctx.response_text
  OUTPUT
  ─────────────────────────────────────────────
  JSON body             WS binary audio frames
  SSE token stream      (TTS synthesis in transport)
```

---

## Package Structure

```
p-lanes/
├── main.py                          # Kernel: lifespan, handle_message, handle_stream
├── config.py                        # Single source of truth — reads config.yaml + users.yaml
├── config.yaml                      # System config: slots, LLM, sampling, modules, security
├── users.yaml                       # Per-user slot + security assignments (gitignored)
│
├── core/
│   ├── llm.py                       # llama.cpp process lifecycle, call(), call_stream(), call_internal()
│   ├── slots.py                     # User dataclass, slot locks, profile I/O, shutdown save
│   ├── transport.py                 # FastAPI routes, Gate 1, WS voice, SSE broadcast endpoint
│   ├── pipeline.py                  # PipelineContext dataclass — the pipeline's data contract
│   ├── events.py                    # @register decorator, 4-phase registry, module auto-discovery
│   ├── envelope.py                  # MessageEnvelope input contract, Source enum
│   ├── summarizer.py                # Context compression — async (utility) and in-place modes
│   ├── scheduler.py                 # @schedule decorator, background cron job runner
│   ├── broadcast.py                 # SSE event broadcast for token stream listeners
│   ├── gates.py                     # Summarization gate state (prevents double-fire)
│   ├── gemini.py                    # Gemini Flash external inference backend + rate limiter
│   ├── secrets.py                   # !secret tag support, get_secret()
│   └── log.py                       # Structured logging (structlog)
│
├── service/
│   └── service.py                   # Phase runner: Gate 2 enforcement, priority sort, dispatch
│
├── modules/                         # Single .py files — self-register via @register on import
│   ├── semantic_router.py           # classifier · embedding-based intent classification
│   ├── hello_world.py               # classifier · greeting detection + early response
│   ├── flag_reply.py                # classifier · "flag that" → writes flagged response files
│   ├── weather_query.py             # classifier · outside weather via HA
│   ├── timer.py                     # classifier · timer/alarm intent handler
│   ├── entity_enricher.py           # enricher   · resolves device names → HA entity list
│   ├── device_control.py            # enricher   · validates + executes HA device commands
│   ├── ha_query.py                  # enricher   · reads HA sensor state into enrichments
│   ├── rag_enricher.py              # enricher   · ChromaDB semantic search → context injection
│   ├── web_search.py                # enricher   · SearXNG + optional Jina Reader
│   ├── response_verifier.py         # responder  · background Gemini hallucination check
│   └── crawler.py                   # scheduled  · nightly Jina web crawl (not a pipeline module)
│
└── providers/                       # Service providers — autodiscovered from subdirectories
    ├── base.py                      # Abstract bases: Provider, STTProvider, TTSProvider, EmbeddingProvider
    ├── whisper/                     # STTProvider — remote Whisper transcription
    ├── kokoro/                      # TTSProvider — remote Kokoro TTS
    ├── embeddings/                  # EmbeddingProvider — local BGE-M3 sentence embeddings
    ├── homeassistant/               # HA REST API client
    ├── rag/                         # ChromaDB persistent store + file ingestor
    └── sqlite/                      # aiosqlite persistent DB at /var/lib/p-lanes/db/p-lanes.db
```

**User data** (per-user, at `/var/lib/p-lanes/users/{user_id}/`):
- `profile.json` — persona, voice_id, rag_scope, area; written on shutdown
- `summary.txt` — rolling conversation summary; written after each summarization

Conversation history is held in memory on the User object and is not persisted to disk between restarts (intentional — slot context is always rebuilt from summary + recent injected turns).

---

## Pipeline

Every request flows through four registered phases. The LLM call happens between the enricher and responder phases and is handled directly by the kernel — it is not a registered phase and modules do not call it.

### Registered Phases

```python
# core/events.py
PHASES = ("classifier", "enricher", "responder", "finalizer")
```

| Phase | Handled by | Purpose |
|---|---|---|
| `classifier` | service.py → modules | Classify intent, set `ctx.intent`, optionally abort or short-circuit |
| `enricher` | service.py → modules | Gather context into `ctx.enrichments` and `ctx.metadata` |
| `[LLM]` | main.py kernel | Build prompt, call llama.cpp slot, set `ctx.response_text` |
| `responder` | service.py → modules | Post-response side effects (background verification, etc.) |
| `finalizer` | service.py → modules | Final output overrides (no modules currently registered) |

### Module Execution Order

Within each phase, `service.py` runs modules in priority order (`MODULE_PRIORITIES` in config.yaml, default 50). Ties broken alphabetically. Gate 2 silently skips modules the user lacks permission for.

### Skip Processor

A module that fully handles a request (e.g. `device_control`) sets `ctx.skip_processor = True` and writes to `ctx.response_text`. The kernel sees this and skips the LLM call. The exchange is still injected into conversation history.

### Intent Routing

`semantic_router` uses BGE-M3 embeddings. On first request it builds centroid vectors from example phrases in `modules/intents.yaml`. Subsequent requests embed the message and find the closest bucket via cosine similarity.

- Below `confidence_threshold` (0.55) → no intent candidate
- Above threshold but below `confidence_required` (0.65 global, per-intent overrides exist) → intent not set
- Above `confidence_required` → `ctx.intent` set, per-intent `temperature_override` applied

Intent buckets: `device_control`, `ha_sensor`, `outside_weather`, `media_control`, `timer_alarm`, `calendar`, `web_search`, `local_search`, `general`.

`web_search` (direct internet queries) → web_search module runs.
`local_search` (lookup queries) → rag_enricher runs first; if it finds a hit, web_search skips. If RAG is empty, web_search fires as fallback.

---

## Security Model

Two hard gates. The LLM never makes security decisions.

```
Gate 1 — Identity                  Gate 2 — Module Permission
(core/transport.py)                (service/service.py)

WHO are you?                       CAN you use this module?

Reads: users.yaml (via config.py)  Reads: MODULE_PERMISSIONS in config.yaml
Unknown user_id → 403              Below required level → silently skipped
Known → pipeline continues         Cleared → module.handle(ctx) called
```

Unknown user IDs are mapped to the `guest` slot (if guest is enabled) before Gate 1 evaluates — so truly unknown IDs that don't resolve to guest are rejected.

### Security Levels

```python
# config.py
class SecurityLevel:
    GUEST   = 0
    USER    = 1
    POWER   = 2
    TRUSTED = 3
    ADMIN   = 4
```

Security level is loaded from `users.yaml` at startup and is **never** read from `profile.json`. This prevents level escalation via user-writable profile files.

### Device Domain Permissions

A separate cumulative permission layer inside `device_control.py`. Level N grants all domains listed at levels ≤ N.

```yaml
# config.yaml
device_domain_permissions:
  1: ["light", "switch", "fan", "input_boolean"]
  2: ["climate", "cover"]
  3: ["lock"]
```

Users below level 1 (GUEST) cannot control any devices regardless of module permission.

---

## Context Object

`PipelineContext` is the single data object flowing through every phase. Modules read from it, write to it, and return it.

```python
# core/pipeline.py
@dataclass
class PipelineContext:
    # --- input (set at construction) ---
    user:      User              # resolved User object (slot, security, history, locks)
    envelope:  MessageEnvelope   # full normalized input contract

    # --- classifier output ---
    intent:       str       = ""    # set by semantic_router
    tags:         list[str] = []
    requires_llm: bool      = True

    # --- enricher output ---
    enrichments: list[dict] = []
    # each entry: {"source": "...", "content": "..."}
    # assembled into the LLM prompt by build_enriched_prompt()

    # --- inter-module structured data ---
    metadata: dict = {}
    # keyed by convention: metadata["resolved_entities"], etc.

    # --- kernel output ---
    response_text: str   = ""
    total_tokens:  int   = 0
    truncated:     bool  = False
    elapsed:       float = 0.0

    # --- finalizer output ---
    final_output: str = ""      # what actually gets sent to the channel

    # --- control flags ---
    aborted:        bool = False
    abort_reason:   str  = ""
    skip_processor: bool = False   # set True to bypass LLM call

    # --- sampling override ---
    temperature_override: float | None = None   # set by semantic_router per intent
```

**Convenience properties** on ctx (delegate to envelope): `raw_message`, `source`, `device_id`, `language`, `stt_confidence`, `voice_confidence`, `attachments`, `conversation_id`, `message_id`.

**Prompt builder:** `ctx.build_enriched_prompt()` assembles all `ctx.enrichments` into labeled blocks, then appends the raw user message. If `enrichments` is empty, returns the raw message unchanged.

---

## Providers

Providers handle services the pipeline depends on. They are not pipeline stages — they start at boot and are available globally via the `providers` registry.

### Base Types

```python
# providers/base.py
class Provider(ABC):           # start(), stop(), is_ready
class STTProvider(Provider):   # transcribe(audio, sample_rate) → TranscribeResult
class TTSProvider(Provider):   # synthesize(text) → bytes
                               # synthesize_stream(text) → AsyncIterator[bytes]
class EmbeddingProvider(Provider):  # embed(texts), embed_async(texts)
```

`TranscribeResult` carries `text`, `vad` (bool — False means no speech detected), `language`, and `stt_confidence`.

### Installed Providers

| Provider | Type | Role |
|---|---|---|
| `whisper` | STTProvider | Remote Whisper speech-to-text |
| `kokoro` | TTSProvider | Remote Kokoro text-to-speech |
| `embeddings` | EmbeddingProvider | Local BGE-M3 sentence embeddings |
| `homeassistant` | Provider | HA REST API — state reads and service calls |
| `rag` | Provider | ChromaDB vector store + file ingestor |
| `sqlite` | Provider | aiosqlite persistent DB with versioned migrations |

### SQLite Provider

Single shared database at `/var/lib/p-lanes/db/p-lanes.db`. WAL mode, foreign keys on per connection.

**Tables:** `garmin_metrics`, `weight_log`, `plants`, `plant_checkins`, `transactions`, `component_inventory`, `weather_history`, `workouts`.

Schema version tracked in `_meta` table (`key=schema_version`). Migrations are an append-only list in `provider.py → _MIGRATIONS` — never edit existing entries.

```python
db = providers.get_db()
row     = await db.fetchone("SELECT * FROM weight_log WHERE id = ?", (id,))
rows    = await db.fetchall("SELECT * FROM weight_log ORDER BY date DESC")
last_id = await db.execute("INSERT INTO weight_log (date, kg) VALUES (?,?)", (date, kg))
```

### RAG Provider

ChromaDB at `/var/lib/p-lanes/chroma/`. Data files at `/var/lib/p-lanes/rag/`.

**Collections:** one per shared topic (`shared_home`, `shared_cooking`, `shared_electronics`, `shared_games`, `shared_general`) plus one private collection per named user (`user_{user_id}`). All use cosine similarity (HNSW). Embeddings provided by the `embeddings` provider.

Ingest state tracked by file mtime at `/var/lib/p-lanes/chroma/ingest_state.json`. The ingestor is header-aware (splits markdown on `##` headers). Trigger re-ingest: `POST /admin/rag/ingest`.

### Provider Discovery

At startup, `providers.autodiscover()` scans all subdirectories under `providers/` and imports each `provider.py`. Providers register themselves on import. Core never imports a provider by name.

---

## Modules

Modules are the only place where feature logic lives. Each module is a single `.py` file that self-registers at import time via `@register`.

### Registration

```python
from core.events import register
from core.pipeline import PipelineContext

@register("module_name", phase="classifier")
async def handle(ctx: PipelineContext) -> PipelineContext:
    ...
    return ctx
```

All modules are imported automatically when `import modules` runs at startup. No manifest files. No manual registration.

### Valid Phases

```python
PHASES = ("classifier", "enricher", "responder", "finalizer")
```

### Scheduled Modules

A module running on a cron schedule (not in the pipeline) uses `@schedule` from `core/scheduler.py`. The crawler is the only current example.

```python
from core.scheduler import schedule

@schedule(cron="0 23 * * *", requires_idle=True, max_duration=3600)
async def crawl():
    ...
```

Scheduled modules are discovered and started separately from pipeline modules.

### Module Rules

1. Register to exactly one phase via `@register`. Return `ctx` even if unchanged.
2. Never import from other modules. Use `ctx.metadata` for structured handoffs between modules in the same phase.
3. Write text context for the LLM into `ctx.enrichments`. Never build the LLM prompt manually.
4. To skip the LLM: set `ctx.skip_processor = True` and write `ctx.response_text`.
5. To abort the pipeline: set `ctx.aborted = True` and `ctx.abort_reason`.
6. Never check your own permissions — Gate 2 in the dispatcher handles it.
7. All I/O must be async. Never block the event loop.
8. For enricher modules: check `ctx.intent` at the top and return early if the intent isn't yours.
9. Module config lives in its own YAML file. Never touch `config.py`.
10. Direct `llm.call_internal()` use is permitted only for modules that need a clean utility-slot LLM call (e.g. device_control parsing a command into structured JSON). It must never write to user conversation history.

---

## Core Components

### Kernel (main.py)

The kernel owns the LLM call. It is not a module and cannot be swapped out.

```
handle_message / handle_stream:
  1. Resolve user from envelope.user_id (Gate 1 happened in transport)
  2. Build PipelineContext
  3. run_pre_processor → classifier phase + enricher phase
  4. If slot lock held: wait up to SUMMARIZE_LOCK_WAIT seconds
  5. If not skip_processor:
       call llm.call() or llm.call_stream()
       On LLMContextOverflow: emergency_summarize → retry once
  6. If skip_processor and response_text set: inject exchange into history
  7. run_post_processor → responder phase + finalizer phase
  8. Return ctx.final_output or ctx.response_text
  9. Broadcast to SSE listeners (no-op if broadcast disabled)
```

### Slot Architecture

```
┌──────────┐ ┌──────────┐ ┌──────────┐ ┌──────────┐ ┌──────────┐
│  SLOT 0  │ │  SLOT 1  │ │  SLOT 2  │ │  SLOT 3  │ │  SLOT 4  │
│  ADMIN   │ │ TRUSTED  │ │   USER   │ │  GUEST   │ │ UTILITY  │
│ persist  │ │ persist  │ │ persist  │ │ persist  │ │ ephemeral│
│ Lock()   │ │ Lock()   │ │ Lock()   │ │ Lock()   │ │ Lock()   │
└──────────┘ └──────────┘ └──────────┘ └──────────┘ └──────────┘
 voice+chat   voice+chat   voice+chat   fallback     summarizer
                                                     + verifier
```

- Each slot has an `asyncio.Lock()` serializing concurrent requests.
- All channels for a given user hit the same slot — voice and chat share history.
- The guest slot is shared by any unrecognized user (if guest is enabled).
- The utility slot runs background LLM calls (summarization, device command parsing, PII checks) without touching user conversation history. Its KV state is not preserved between calls.

### Summarizer

Compresses conversation history when slots fill up. Two modes depending on whether the utility lane is enabled:

**Async mode** (utility lane on — default):
- Snapshot history at index N; fire summarization on utility slot without locking
- User keeps talking while summarization runs
- On completion: merge new summary + recent messages from snapshot + any messages that arrived during summarization
- Summarization gate stays closed until the next clean LLM response confirms the slot state is fresh

**In-place mode** (utility lane disabled):
- Acquire slot lock → run summarization on user's own slot → apply → release
- User receives "Give me just a second..." if lock is held longer than SUMMARIZE_LOCK_WAIT

**Triggers:**
- `flag_warn` (tokens > 70% of slot): summarize on next idle cycle
- `flag_crit` (tokens > 80% of slot): immediate background task on every response
- `LLMContextOverflow`: emergency in-place summarization, always, inline with the blocked request
- Scheduled: daily (cron in config.yaml), runs all non-guest slots; optionally restarts llama-server after

**Token budgets** (at 8,192 tokens/slot):
- System header carve-out: 128 tokens
- Summary cap: 10% of remaining ≈ 806 tokens
- Recent history kept: 15% of remaining ≈ 1,210 tokens

**Fallback:** if the LLM call fails during summarization, `_emergency_trim()` keeps only recent messages within budget, preserves existing summary, clears flags. Partial memory beats a broken slot.

**Guest slots** are never summarized — history is wiped on idle instead.

### Transporter (core/transport.py)

FastAPI server. Normalizes all input into `MessageEnvelope` before the pipeline.

**Endpoints:**

| Method | Path | Auth | Purpose |
|---|---|---|---|
| POST | `/channel/chat` | Gate 1 | Blocking text in, JSON out |
| POST | `/channel/chat/stream` | Gate 1 | Text in, SSE token stream out |
| WS | `/channel/voice` | Gate 1 (query param) | PCM audio in, WAV audio out (sentence-buffered) |
| GET | `/channel/listen/{user_id}` | Gate 1 (query param) | SSE broadcast — own stream only |
| GET | `/health` | none | LLM status, provider list, version |
| GET | `/slots` | none | All slot states (flags, idle, history length) |
| POST | `/llm/restart` | ADMIN | Restart llama-server process |
| GET | `/admin/dump` | ADMIN | Full prompt dump for all users |
| GET | `/admin/dump/{user_id}` | ADMIN | Full prompt dump for one user |
| POST | `/admin/entity-index/refresh` | ADMIN | Clear entity index, rebuild on next request |
| POST | `/admin/rag/ingest` | ADMIN | Trigger background RAG re-ingest |

**Voice WebSocket protocol:**
- Client → server: binary frames (WAV audio, 16kHz mono)
- Server → client: binary frames (WAV audio, 24kHz mono), JSON control events
- Control events: `ready`, `transcript`, `silence`, `text` (TTS fallback), `done`, `error`
- Client ping → server pong
- Response is sentence-buffered: TTS runs per sentence as LLM streams, reducing first-audio latency

**Broadcast listener** (`/channel/listen/{user_id}`): SSE stream of token and done events. Same-user only — users can only subscribe to their own stream.

### MessageEnvelope (core/envelope.py)

The normalized input contract. All channels produce one before the pipeline.

```python
@dataclass
class MessageEnvelope:
    user_id:          str | None     # None = unidentified; slots.get_user maps to guest
    source:           Source         # TEXT | VOICE | API | HA
    text:             str | None     # None if purely attachment-based

    message_id:       str            # auto-generated UUID
    timestamp:        datetime       # auto-generated UTC

    conversation_id:  str | None = None
    device_id:        str | None = None   # return address for output routing
    language:         str | None = None   # STT-detected language code
    stt_confidence:   float | None = None
    voice_confidence: float | None = None
    attachments:      list[Attachment] | None = None
```

---

## Data Flow Examples

### Voice Control — "Turn on the kitchen lights"

```
WS /channel/voice
  → audio bytes → Whisper STT → "turn on the kitchen lights"
  → MessageEnvelope(source=VOICE)
  → Gate 1: known user ✓
  → classifier:
      semantic_router → intent = "device_control"
  → enricher:
      entity_enricher [10] → resolves "kitchen lights" → metadata["resolved_entities"]
      device_control  [20] → domain check ✓, call_internal() parses command,
                             HA API called → skip_processor=True, response_text="Done."
      rag_enricher    [30] → intent not in RAG_INTENTS → skip
      web_search      [50] → intent not "web_search"/"local_search" → skip
      ha_query        [50] → intent not "ha_sensor" → skip
  → kernel: skip_processor=True → LLM skipped
  → responder:
      response_verifier → "device_control" not in verify_intents → skip
  → transport: Kokoro TTS → audio → client
```

### Knowledge Query (RAG → local knowledge)

```
POST /channel/chat → "what's the SRAM spec for the ESP32-S3?"
  → Gate 1: ✓
  → classifier: intent = "local_search"
  → enricher:
      rag_enricher [30] → embeds query, searches shared_electronics → hit found
                          ctx.enrichments += [{"source": "knowledge base...", "content": "..."}]
      web_search   [50] → local_search intent, RAG hit detected → skipped
  → kernel: build_enriched_prompt() → enrichment block + user message → llm.call()
  → responder: response_verifier → "local_search" not in verify_intents → skip
  → JSON response
```

### General Chat with Background Verification

```
POST /channel/chat → "what happened in the news today?"
  → Gate 1: ✓
  → classifier: intent = "web_search"
  → enricher:
      web_search [50] → SearXNG query → Jina fetch for thin snippets
                        ctx.enrichments += [{"source": "web search results", "content": "..."}]
  → kernel: llm.call() → response_text set
  → responder:
      response_verifier → intent "web_search"... check config verify_intents
                          fires asyncio.create_task (non-blocking)
                          → utility slot: PII check ("CLEAR"/"DIRTY")
                          → if CLEAR: Gemini verify → flags file if hallucination detected
  → response returned immediately (verifier runs in background)
```

---

## Rules for Contributors

1. **`main.py` only grows for wiring.** Feature logic belongs in modules or core subsystems.
2. **`core/` never imports from `modules/` or `providers/`.** Boundary is absolute.
3. **Modules never import from other modules.** Use `ctx.metadata` for structured handoff.
4. **Write context for the LLM into `ctx.enrichments`.** Never build the prompt manually.
5. **To skip the LLM:** set `ctx.skip_processor = True` and write `ctx.response_text`.
6. **`llm.call_internal()` is for clean utility-slot calls only** (e.g. parsing structured JSON). It must never inject into user conversation history.
7. **Security is two gates only.** Modules never check their own permissions.
8. **Core config (`config.py`) is read-only at runtime.** Addons use their own YAML files.
9. **Whitelist validation only.** Never use a blacklist at security decision points.
10. **The LLM is never a security decision maker.**
11. **All module I/O must be async.** Never block the event loop.
12. **Never use `systemctl restart` when editing profile files.** `shutdown_all()` writes profiles on stop — changes get overwritten. Always: stop → write → start.
13. **Append-only migrations.** Never edit existing entries in `sqlite/provider.py → _MIGRATIONS`.
14. **Security level is set by `users.yaml` only.** It is never read from `profile.json` — this is intentional to prevent escalation via user-writable files.
15. **Drop-in discovery.** New module: add a `.py` file to `modules/`. New provider: add a subdirectory to `providers/`. No manual registration anywhere.
16. **Module permissions default to USER (1)** if not listed in `MODULE_PERMISSIONS`. Explicitly set to GUEST (0) only when intentional.
