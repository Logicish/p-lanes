# p-lanes — Architecture

> Authoritative architecture reference for p-lanes. Designed for contributors and LLM coding assistants alike. When in doubt, this document decides.

---

## Table of Contents

- [Overview](#overview)
- [Package Structure](#package-structure)
- [Pipeline](#pipeline)
- [Channels](#channels)
- [Security Model](#security-model)
- [Providers](#providers)
- [Modules](#modules)
- [Context Object](#context-object)
- [Core Components](#core-components)
- [System Tools](#system-tools)
- [Data Flow](#data-flow)
- [Extensibility](#extensibility)
- [Rules for Contributors](#rules-for-contributors)

---

## Overview

p-lanes is a microkernel orchestrator for llama.cpp. It pins users to dedicated GPU KV cache slots for near-instant response, routes requests through a modular pipeline, and supports multiple simultaneous I/O channels (voice, chat, vision).

```
Channel → Transporter → Classifier → Enricher → Processor → Responder → Finalizer → Channel
```

**Core principles:**
- `main.py` is ~4 lines. All logic lives in core, service, or modules.
- Modules and providers are drop-in folders with YAML manifests — no manual registration.
- The LLM is a tool, not a decision maker. Security is enforced at three hard gates.
- Config.py is core-only. Addons carry their own config files.

**Hardware (tested build):**

| Component | Spec |
|---|---|
| GPU | NVIDIA RTX 5060 Ti 16GB |
| RAM | 32GB |
| Host | Proxmox VE (bare-metal hypervisor) |
| LLM | Qwen3-VL 8B Q6_K_M via llama.cpp |
| KV Cache | 5 slots × 12k context, Q6 compression |
| VRAM | ~12.25GB used, ~3GB buffer |

---

## Package Structure

```
p-lanes/
├── main.py                              # Microkernel entry (~4 lines)
├── config.py                            # Core-only config (pipeline, channels, security floors, slots)
│
├── core/
│   ├── llm.py                           # llama.cpp lifecycle, call_slot(), token tracking
│   ├── slots.py                         # User objects, slot locks, flags
│   ├── transport.py                     # FastAPI server, channel routing, transcript SSE
│   ├── context.py                       # Context dataclass — the pipeline's API surface
│   ├── registry.py                      # Module auto-discovery + @register decorator
│   ├── config_loader.py                 # load_addon_config() for modules and providers
│   ├── responder.py                     # Built-in LLM response (registered to "responder" stage)
│   ├── summarizer.py                    # Context compression, KV wipe/reinject
│   ├── providers/
│   │   ├── base.py                      # InputProvider, OutputProvider, Attachment, ProcessedInput
│   │   ├── input/
│   │   │   ├── stt_device_map/          # STT + static device-to-user mapping
│   │   │   ├── stt_voiceprint/          # STT + parallel speaker identification
│   │   │   ├── multimodal/              # Text + audio + images
│   │   │   └── text_only/               # JSON text input (chat/dev)
│   │   └── output/
│   │       ├── kokoro_tts/              # Kokoro TTS (default)
│   │       ├── piper_tts/               # Piper TTS (alternative)
│   │       └── text_only/               # Text passthrough (chat/dev)
│   └── tools/
│       ├── tool_runner.py               # Parses "lanes ..." commands
│       ├── base.py                      # BaseTool interface
│       └── builtins/                    # Built-in admin tools
│
├── service/
│   └── dispatcher.py                    # Pipeline executor, Gate 2 + Gate 3 enforcement
│
├── modules/                             # Drop-in module folders (auto-discovered)
│   ├── intent_classifier/               # Semantic intent routing
│   ├── rag/                             # Retrieval augmented generation
│   ├── rag_processor/                   # RAG data compression via utility slot
│   ├── ha_bridge/                       # Home Assistant device control
│   └── config_manager/                  # Admin config changes via chat
│
└── users/{user_id}/                     # Per-user runtime data
    ├── profile.json                     # Persona, security level, slot assignment
    ├── summary.txt                      # Rolling conversation summary
    └── history.db                       # SQLite conversation log
```

Each module folder contains:
```
modules/{name}/
├── module.yaml      # Manifest: name, enabled, stage, intents, security_level
├── config.yaml      # Module-specific settings (optional)
├── __init__.py      # Exposes handle()
└── {name}.py        # Runtime logic
```

Each provider folder contains:
```
core/providers/{capability}/{name}/
├── provider.yaml    # Manifest: name, capability, description
├── config.yaml      # Provider-specific settings (optional)
└── provider.py      # Implementation + singleton
```

---

## Pipeline

Every request flows through five stages in order. Modules register to stages and declare which intents they handle. The dispatcher filters by intent, checks security, and runs matching modules.

```python
PIPELINE = ["classifier", "enricher", "processor", "responder", "finalizer"]
```

| Stage | Purpose | Example Modules |
|---|---|---|
| `classifier` | Classify intent, set `ctx.intent` | intent_classifier |
| `enricher` | Gather data, inject context | rag |
| `processor` | Transform data, execute actions | rag_processor, ha_bridge, system_tools |
| `responder` | Generate LLM response | llm_respond (built-in) |
| `finalizer` | Post-response validation/overrides | config_manager |

**Intent-based activation matrix:**

```
                  classifier  enricher  processor  responder  finalizer
                  ─────────   ────────  ─────────  ─────────  ─────────
general_chat      intent_cls     —          —       llm_resp      —
device_control    intent_cls    rag     ha_bridge   llm_resp*     —
health_query      intent_cls    rag     rag_proc    llm_resp      —
knowledge_query   intent_cls    rag     rag_proc    llm_resp      —
vision_query      intent_cls     —          —       llm_resp      —
config_change     intent_cls     —          —       llm_resp   config_mgr
system_tool       intent_cls     —      sys_tools   llm_resp*     —

  * = llm_respond runs but skips (ctx.final_text already set)
  — = no module registered for this intent at this stage (zero cost)
```

The LLM is not special — it's a built-in module registered to `responder`. Any module can call the LLM on any slot at any stage via `ctx.call_slot()`.

---

## Channels

Channels are named I/O endpoints. Each pairs an input provider with an output provider. All channels share the same pipeline, security gates, user slots, and conversation history.

```python
# config.py
CHANNELS = {
    "voice":  {"input": "stt_voiceprint", "output": "kokoro_tts"},
    "chat":   {"input": "text_only",      "output": "text_only"},
    # "vision": {"input": "multimodal",   "output": "text_only"},
}
```

**Key behaviors:**
- Each channel gets a `/channel/{name}` endpoint
- Same user, same slot, any channel — voice and chat share one conversation
- Response follows the request channel (voice → audio, chat → text)
- Adding a channel = one config entry + restart

### Transcript Stream (Optional)

An SSE endpoint mirrors both sides of a conversation in real time across all channels.

```python
ENABLE_TRANSCRIPT_SSE = True   # config.py
```

- **Endpoint:** `/transcript/{user_id}`
- **Events:** role, text, source channel, timestamp
- **Use case:** HAOS dashboard card showing voice conversations as text
- Read-only — never alters the pipeline
- Gated: ADMIN watches any user, USER watches own only

### Cross-Channel Example

```
TIME   CHANNEL   CONTENT                                        SLOT TOKENS
─────  ───────   ─────────────────────────────────────────────  ──────────
19:30  voice     "What should I make for dinner?"                    1,204
19:30  voice     ← "How about pasta carbonara?"
19:32  chat      "What ingredients do I need?"                       1,847
19:32  chat      ← "Spaghetti, eggs, parmesan, pancetta..."
19:45  voice     "Set a timer for 12 minutes"                        2,103
19:45  voice     ← "Timer set."
19:50  vision    "What does this say?" [photo of wine label]         2,891
19:50  vision    ← "That's a 2019 Chianti. Great with carbonara."

All on slot 0. Transcript SSE shows the full timeline.
```

---

## Security Model

Three hard gates. The LLM never makes security decisions. Each gate can only raise the bar, never lower it.

```
Gate 1 — Identity          Gate 2 — Stage Floor         Gate 3 — Module Permission
(transporter)              (dispatcher)                 (dispatcher)
WHO are you?               WHERE can you go?            CAN you use this?
                           
Reads: user profiles       Reads: config.py             Reads: module.yaml
Unknown → dropped          STAGE_SECURITY floors        security_level field
Known → enter pipeline     Below floor → skip stage     Below level → skip module
```

```python
# config.py — only the system owner can change these
STAGE_SECURITY = {
    "classifier": SecurityLevel.GUEST,      # Everyone
    "enricher":   SecurityLevel.USER,       # No enrichment for guests
    "processor":  SecurityLevel.USER,       # No processing for guests
    "responder":  SecurityLevel.GUEST,      # Everyone gets a response
    "finalizer":  SecurityLevel.TRUSTED,    # Post-processing for trusted+
}
```

**Access by security level:**

| Level | Stages | Notes |
|---|---|---|
| GUEST (0) | classifier → responder | Basic LLM chat only |
| USER (1) | classifier → enricher → processor → responder | Full pipeline minus finalizer |
| TRUSTED (2) | All stages | Modules up to level 2 |
| ADMIN (3) | All stages, all modules | Everything including config changes |

**Why three gates?** Gate 2 prevents a drop-in module from bypassing stage restrictions. A module declaring `security_level: 0` is meaningless if its stage has a floor of 1. Gate 2 is always evaluated first.

---

## Providers

Providers handle I/O outside the pipeline — before input enters and after output leaves. Each is a self-contained folder with a manifest and optional config.

### Base Interfaces

```python
class InputProvider:
    name: str
    async def process(self, raw_request: dict) -> ProcessedInput: ...
    async def startup(self) -> None: ...
    async def shutdown(self) -> None: ...
    def register_routes(self, app: FastAPI) -> None: ...   # Optional custom endpoints

class OutputProvider:
    name: str
    async def process(self, text: str, user: User) -> OutputResult: ...
    async def startup(self) -> None: ...
    async def shutdown(self) -> None: ...
    def register_routes(self, app: FastAPI) -> None: ...   # Optional custom endpoints
```

### Provider Config

Each provider reads its own `config.yaml` — core config is never touched:

```yaml
# core/providers/input/stt_voiceprint/config.yaml
stt_url: "http://localhost:8080"
voiceprint_url: "http://localhost:8081"
voiceprint_threshold: 0.82
```

```python
from core.config_loader import load_addon_config
cfg = load_addon_config(__file__)   # Finds config.yaml next to this file
```

### Custom Routes

Providers can register additional endpoints for non-standard integrations:

```python
class SensorBridge(InputProvider):
    def register_routes(self, app: FastAPI):
        @app.post("/sensor")
        async def receive_sensor(data: dict):
            self.latest_readings[data["sensor_id"]] = data
            return {"status": "ok"}
```

The transporter calls `register_routes(app)` on all active providers at startup.

### Modules vs. Providers

| | Modules | Providers |
|---|---|---|
| Location | `modules/` | `core/providers/{capability}/{name}/` |
| When | During pipeline stages | Before/after the pipeline |
| Discovery | `module.yaml` manifest | `provider.yaml` manifest |
| Hot-reload | Yes (`lanes reload`) | No (restart required) |
| Custom routes | No | Optional |

---

## Modules

Modules are drop-in pipeline components. Drop a folder in `modules/`, restart (or `lanes reload`), done.

### Manifest

```yaml
# modules/intent_classifier/module.yaml
name: intent_classifier
enabled: true
stage: classifier
intents: ["*"]
security_level: 0
description: "Semantic intent classification"
```

### Interface

```python
from core.registry import register

@register("intent_classifier", stage="classifier", intents=["*"])
async def handle(ctx: Context) -> Context:
    # classify and set ctx.intent
    return ctx
```

### Module Rules

1. Never import from other modules, `service/`, or `core/llm.py`
2. Access LLM via `ctx.call_slot()` and `ctx.call_utility()` only
3. Never check permissions — the dispatcher handles it
4. Never write to `config.py` — use own `config.yaml`
5. Always return `ctx`, even if unchanged
6. All IO must be async
7. May read `ctx.attachments` but must not assume they exist

---

## Context Object

The single data object flowing through the pipeline. Every module reads from it, writes to it, and returns it.

```python
@dataclass
class Context:
    user: User                                    # Who is this
    message: str                                  # What they said
    intent: str | None = None                     # Set by classifier
    attachments: list[Attachment] = []             # Images, docs from multimodal input
    metadata: dict = {}                            # Module communication
    prompt_extras: list[str] = []                  # Enrichment data for the LLM prompt
    response: LLMResponse | None = None            # Raw LLM response
    final_text: str | None = None                  # Final output text

    async def call_slot(slot, system, content)     # Call a specific LLM slot
    async def call_utility(system, content)        # Use utility slot (or fallback to user slot)
```

**LLM access tiers:**

| Method | Use Case |
|---|---|
| `ctx.call_slot(N, system, content)` | Explicit slot targeting |
| `ctx.call_utility(system, content)` | Data processing — utility slot with graceful fallback |
| `ctx.call_slot(ctx.user.slot, ...)` | User-facing response (what `llm_respond` does) |

When no utility slot is available (`UTILITY_SLOT = None`), `call_utility()` falls back to the user slot automatically. The `utility_fallback` flag tells the summarizer to compress more aggressively on the next cycle.

---

## Core Components

### Slot Architecture

```
┌──────────┐ ┌──────────┐ ┌──────────┐ ┌──────────┐ ┌──────────┐
│ SLOT 0   │ │ SLOT 1   │ │ SLOT 2   │ │ SLOT 3   │ │ SLOT 4   │
│ user_a   │ │ user_b   │ │ user_c   │ │ guest    │ │ utility  │
│ ADMIN    │ │ TRUSTED  │ │ USER     │ │ GUEST    │ │ EPHEMERAL│
│ persist  │ │ persist  │ │ persist  │ │ persist  │ │ wipe each│
│ Lock()   │ │ Lock()   │ │ Lock()   │ │ Lock()   │ │ Lock()   │
└──────────┘ └──────────┘ └──────────┘ └──────────┘ └──────────┘
  any channel  any channel  any channel  fallback    call_utility()
```

- All channels for a user hit the same slot — voice and chat share history
- Each slot has an `asyncio.Lock()` that serializes concurrent requests
- Ephemeral slots wipe KV cache after every call (prevents data leakage)
- `UTILITY_SLOT = None` disables the utility slot (modules fall back automatically)

### Summarizer

Compresses conversation context when slots fill up:

- **`flag_crit`** (truncated or tokens > `THRESHOLD_CRIT`): immediate background summarization
- **`flag_big`** (tokens > `THRESHOLD_WARN`) + idle: summarize on next cycle
- **Process:** acquire slot lock → generate summary → wipe KV → reinject system prompt + persona + summary + recent history → release lock
- **Lock contention:** if a message arrives during summarization → "Give me just a second..." → wait up to 6s → drop if still locked

### Transporter (core/transport.py)

```python
@app.post("/channel/{channel_name}")
async def receive(channel_name: str, raw_request: dict):
    channel = channels[channel_name]                        # Route to provider pair
    processed = await channel["input"].process(raw_request) # Input provider
    
    # Gate 1 — identity check
    user = slots.get_user(processed.user_id)
    if not user: return {"error": "unauthorized"}
    
    # Transcript broadcast (if enabled)
    # Pipeline execution
    # Transcript broadcast (response)
    
    output = await channel["output"].process(response_text, user)  # Output provider
    return output.to_response()
```

### Dispatcher (service/dispatcher.py)

```python
async def run(ctx: Context) -> Context:
    for stage in config.PIPELINE:
        # GATE 2 — stage security floor
        if ctx.user.security_level < config.STAGE_SECURITY.get(stage, 0):
            continue

        for module in registry.get(stage, ctx.intent):
            # GATE 3 — module permission
            if ctx.user.security_level < module.security_level:
                continue
            ctx = await module.handle(ctx)

    return ctx
```

---

## System Tools

Admin introspection via the `lanes` prefix. Classified as `system_tool` intent, handled in the `processor` stage, short-circuits the responder.

```
lanes help                 Available tools (filtered by security level)
lanes pipeline [intent]    Module execution order for an intent
lanes slots                Slot state (user, tokens, flags, idle)
lanes channels             Active channels and provider pairs
lanes security             Stage floors + module permission levels
lanes health               System health (GPU, slots, uptime, disk)
lanes debug [on|off]       Toggle per-request pipeline trace
lanes trace [last|N]       View trace from in-memory ring buffer
lanes config               Read-only core config dump
lanes summary [user]       Current conversation summary
lanes history [user] [N]   Last N conversation turns
lanes wipe [user|slot]     Force-wipe KV cache (confirmation required)
lanes reload               Hot-reload module registry
lanes test [module]        Run module self-test
```

---

## Data Flow

### Request Lifecycle

```
Client ─── POST /channel/{name} ───► Transporter
                                         │
                                    Input Provider
                                    (STT, voiceprint, text, multimodal)
                                         │
                                    ┌─ GATE 1 ─┐
                                    │ Identity  │
                                    └─────┬─────┘
                                         │
                                      main.py
                                    (build Context)
                                         │
                              ┌──── Dispatcher ────┐
                              │                    │
                              │  ┌─ GATE 2 ──────┐ │
                              │  │ Stage floor    │ │
                              │  └───────┬────────┘ │
                              │          │          │
                              │  ┌─ GATE 3 ──────┐ │
                              │  │ Module perm    │ │
                              │  └───────┬────────┘ │
                              │          │          │
                              │   module.handle()   │
                              │     (per stage)     │
                              └──────────┬──────────┘
                                         │
                                   Output Provider
                                   (TTS or text)
                                         │
Client ◄──────────── response ───────────┘
```

### Voice Chat — "Tell me a joke"

```
/channel/voice → stt_voiceprint → user=dad
  Gate 1: ✓
  classifier → intent = "general_chat"
  responder  → llm_respond → slot 0 → joke
/channel/voice ← TTS provider → audio → speaker
```

### Device Control — "Turn on the kitchen lights"

```
/channel/voice → stt_voiceprint → user=dad
  Gate 1: ✓
  classifier → intent = "device_control"
  processor  → ha_bridge → HA API call → ctx.final_text = "Lights on."
  responder  → final_text set, LLM skipped
/channel/voice ← TTS provider → "Lights on."
```

### RAG Health Query

```
/channel/voice → stt_voiceprint → user=dad
  classifier → intent = "health_query"
  enricher   → rag → ctx.prompt_extras = [47 weight entries]
  processor  → rag_processor → call_utility() → compressed summary
  responder  → llm_respond → slot 0 → "You're down 13 pounds."
/channel/voice ← TTS provider → audio
```

### Vision Query (future)

```
/channel/vision → multimodal → user=dad + [image attachment]
  classifier → intent = "vision_query"
  responder  → llm_respond → build_content() → multimodal blocks → Qwen3-VL
/channel/vision ← text_only → JSON response
```

---

## Extensibility

| Layer | Install | Restart? | Uninstall |
|---|---|---|---|
| Module | Drop folder in `modules/` | No (`lanes reload`) | Delete folder |
| Provider | Drop folder in `core/providers/` | Yes | Delete folder |
| Channel | Add entry to `config.py` CHANNELS | Yes | Remove entry |
| Stage | Add to `config.py` PIPELINE | Yes | Remove entry |
| System tool | Drop file in `core/tools/builtins/` | Yes | Delete file |

| Feature | How | Core Changes |
|---|---|---|
| New TTS/STT engine | Drop provider folder | None |
| New chat interface | Add channel config entry | None |
| Vision/multimodal | Add multimodal provider + channel | None |
| RAG pipeline | Drop module in `modules/` | None |
| Device control | Drop module in `modules/` | None |
| Sensor endpoint | Provider with `register_routes()` | None |
| Live transcript | Set `ENABLE_TRANSCRIPT_SSE = True` | None |
| New admin command | Drop file in `core/tools/builtins/` | None |

---

## Rules for Contributors

1. **`main.py` never grows.** Logic belongs in core, service, or modules.
2. **`core/` never imports from `modules/`.** The boundary is absolute.
3. **Modules never import** from other modules, `service/`, or `core/llm.py`.
4. **LLM access** is through `ctx.call_slot()` and `ctx.call_utility()` only.
5. **Security is three gates only.** Modules never check their own permissions.
6. **Config.py is core-only.** Addon settings live in their own `config.yaml`.
7. **Whitelist validation only.** Never blacklist.
8. **The LLM is never a security decision maker.**
9. **All module IO must be async.** No blocking the event loop.
10. **Ephemeral slots wipe after every call.** No exceptions.
11. **Token tracking** uses `usage.total_tokens` from the latest response only. Never accumulate.
12. **Context is the API.** Modules communicate through `ctx.metadata` and `ctx.prompt_extras`.
13. **Gate 2 before Gate 3.** A module cannot lower the stage floor.
14. **Drop-in discovery.** Manifests are scanned automatically. No manual registration.
