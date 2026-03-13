# p-lanes (alpha stage)

A modular "microkernel"/wrapper for llama.cpp focused on: home-lab scaled hardware, low-latency, and KV slot pinned users.

[![License: AGPL v3](https://img.shields.io/badge/License-AGPL%20v3-blue.svg)](https://www.gnu.org/licenses/agpl-3.0)
[![Python 3.12+](https://img.shields.io/badge/python-3.12+-blue.svg)](https://www.python.org/downloads/release/python-3120/)

---

## 🚀 What is p-lanes?

p-lanes is a lightweight orchestrator for local AI. This software is designed specifically to optimize the interface of consumer-grade systems with a low, fixed set of users. While most systems prioritize adaptability for a fluctuating user base, p-lanes focuses on minimizing latency and reducing overhead to provide maximum speed and quality for a dedicated home system.

The project was born out of frustration with existing software that didn't fit my specific "Household Scale" goal of:
- **Dedicated Identity:** 3 named users plus a "guest" account, each with unique AI personalities and unique system privileges.
- **Modular Recovery:** A drop-in architecture that allows for heavy tinkering; if I "nuke" a system while experimenting or want to try something new, I don't have to re-code everything.
- **The "Instant-On" Goal:** Sub 2-second latency when a user activates the assistant, even after a full day of inactivity.

---

## 🧠 The Philosophy: Why p-lanes?

Engines like Aphrodite or vLLM are engineering marvels designed for enterprise-scale throughput. However, they are built to solve a problem most home-labs don't have: serving hundreds of concurrent users. In a local household environment, these frameworks often force trade-offs that degrade the user experience and punish consumer hardware.

Most frameworks handle your conversation memory (KV Cache) in one of three ways when the system is idle:
- **Discarding Cache:** The history is thrown away to save space. This leads to full re-tokenization (~2–10+ seconds of latency depending on context size).
- **Swapping to RAM:** This creates a significant RAM burden (~4GB per 32k token window per user, uncompressed) and minor latency hits as data moves back and forth across the system bus.
- **Swapping to SSD:** This leads to SSD wear and moderate latency hits (~1–3 seconds for context retrieval from disk).

By using llama.cpp as a lightweight foundation, p-lanes starts with significantly less overhead than feature-rich, memory-heavy alternatives. We then lock the engine into a "reserved seat" configuration. By pinning users to dedicated hardware slots, your memory stays exactly where it belongs—on the GPU—ensuring your assistant is always "warm" and ready for an instant response.

---

## ✨ Key Features

- **Deterministic Slot Mapping:** Users are assigned permanent VRAM slots for near-instant response. Fully adjustable capacity (e.g., 2 large-context slots or 10+ small-context slots, adjustable total window size).
- **Optional Utility Lane:** Background lanes for tasks like RAG retrieval or prompt fixing, stateless. This prevents "dirtying" the primary user history with system-level data. Gracefully falls back to the user slot when unavailable.
- **Automatic Summarization:** Logic to compress older conversation context into summaries to prevent VRAM overflow while preserving long-term memory. Customizable and optional scheduled summarizations.
- **Multi-Channel I/O:** Simultaneous voice, chat, and future vision interfaces — each with its own input/output providers — all feeding into the same pipeline and the same user slot. Speak in the kitchen and type on your phone; the LLM sees one continuous conversation.
- **Live Transcript Stream** *(planned):* Optional SSE (Server-Sent Events) endpoint that mirrors both sides of a conversation in real time. Connect a dashboard card to watch voice conversations appear as text, with chat and voice turns interleaved in a unified timeline.
- **Three-Gate Security Model:** Layered access control that never trusts the LLM as a decision maker. Gate 1 verifies user identity at the transporter. Gate 2 enforces stage-level access floors from core config. Gate 3 checks per-module permissions declared by each addon. Each gate can only raise the bar, never lower it.
- **Drop-In Architecture:** Modules and providers are self-contained folders with their own manifests and config files. Drop a folder in, restart, and the system auto-discovers it. Remove the folder to uninstall. Core config is never touched by addons.
- **Full Customization:** Control over model weights, summarization triggers, and KV window sizes.
- **Minimalist Overhead:** Headless, transparent code designed for 24/7 reliability on consumer-grade hardware.

---

## ⚠️ Limitations

To achieve deterministic low-latency, p-lanes makes several intentional trade-offs. This is a specialized household tool, not a general-purpose enterprise engine.

- **"Always-On" Core Architecture:** The p-lanes kernel and primary user slots are designed as persistent server processes. There is currently no mechanism to "load-on-request" for primary user slots; once the core is up, the VRAM for those users is pinned.
- **Linear Slot Division:** p-lanes operates as an extension of the llama.cpp slot system. VRAM is divided equally among all active slots; you cannot assign different context window sizes to different users in a single instance.
- **Hard Slot Boundaries:** Each user is locked into their allocated memory. A user's context window cannot "overflow" into another user's slot. If a user hits their limit, the summarization module must be triggered or risk request failure/truncation of context.
- **Static VRAM Pre-allocation:** Once a slot is pinned, that VRAM is reserved. It cannot be dynamically reclaimed for other tasks (like gaming) without stopping the engine.
- **Compute Contention:** While the memory is persistent, the GPU's cores are a shared resource. If multiple users trigger a request simultaneously, the tokens-per-second (TPS) will be divided across those active requests.
- **Hardware Ceiling:** VRAM is your hard limit. Performance is strictly tied to what fits physically on the GPU.

---

## 🔄 Data Flow

### Pipeline

Every request — regardless of whether it arrives as audio, text, or a photo — flows through the same pipeline:

```
Channel → Transporter → Classifier → Enricher → Processor → Responder → Finalizer → Channel
```

**Channel:** Named I/O endpoints, each with its own input/output provider pair. `/channel/voice` pairs an STT provider with a TTS provider. `/channel/chat` uses text in, text out. Multiple channels can be active simultaneously, and all channels share the same pipeline, security gates, and user slots.

**Transporter:** The internal plumbing. Standardizes varied input data into a unified format, resolves user identity via the channel's input provider, and enforces Gate 1 (is this user known?).

**Classifier:** Intent classification. Identifies what the user is asking for (general chat, device control, knowledge query, system command) and tags the request so downstream stages know which modules to activate.

**Enricher:** Injection of real-world context. Automatically tagging the current time, location, or live Home Assistant sensor data (e.g., "The living room lamp is currently ON") into the prompt. Modules here gather data but don't process it.

**Processor:** Data transformation. Compresses raw enrichment data via the utility slot, prepares structured API calls, or handles system tool commands. This is where heavy lifting happens before the LLM sees the prompt.

**Responder:** The primary inference point. Hits the user's pinned llama.cpp slot for conversation. If an earlier stage already produced a final response (like a system tool or a short-circuited device command), this stage skips the LLM call entirely.

**Finalizer:** Post-response actions. Formatting text for a specific UI, validating config change requests, preparing output for TTS, or writing to conversation history.

### Channels

Channels are how data enters and leaves the system. Each channel pairs an input provider (how to understand the request) with an output provider (how to deliver the response). All channels feed into the same pipeline.

```
/channel/voice  → STT provider + TTS provider       → audio in, audio out
/channel/chat   → text provider + text provider      → JSON in, JSON out
/channel/vision → multimodal provider + text provider → text + images in, JSON out (future)
```

Each provider is a drop-in package. The defaults ship with Whisper for STT and Kokoro for TTS, but swapping to an alternative (like Piper TTS or faster-whisper) is a config change — no core code modifications required.

Speaking in the kitchen and typing on your phone both land on the same user slot with the same conversation history. The response goes back through the channel it came from — voice input returns audio, chat input returns text. You won't accidentally get TTS blaring from a speaker because you quietly typed something on your phone.

### Security Model

Security is enforced at three hard gates. The LLM is never a decision maker for access control.

**Gate 1 — Identity (Transporter):** Is this user known to the system? Unknown users are dropped immediately — no response, no pipeline, nothing.

**Gate 2 — Stage Access (Dispatcher):** Can this user access this stage of the pipeline? Core config defines a minimum security level per stage. Guests might only clear the classifier and responder. The entire enricher, processor, and finalizer stages are skipped without even evaluating individual modules. Only the system owner can change these floors.

**Gate 3 — Module Permission (Dispatcher):** Can this user use this specific module? Each module declares its own required security level in its manifest. A module can make itself stricter than the stage floor but never more permissive. A drop-in module declaring `security_level: 0` is meaningless if it registers to a stage with a floor of 1.

### Example: Knowledge-Based Chat (RAG)

A user asks a question about a personal document in a chat window.
```
Channel:     /channel/chat — text input, text output
Transporter: Package text with user identity; Gate 1 passes
Classifier:  Intent = "knowledge_query"; Gate 2 clears this stage
Enricher:    RAG module retrieves relevant document chunks; Gate 3 passes
Processor:   Utility slot compresses raw data into a summary; Gate 3 passes
Responder:   User's pinned llama.cpp slot generates the answer
Finalizer:   Formats with Markdown for the chat UI
Output:      Text displayed in the chat window
```

### Example: Low-Latency Voice Control

A user says "Turn on the kitchen lights."
```
Channel:     /channel/voice — audio in via STT provider, audio out via TTS provider
Transporter: Parallel STT + voiceprint identification; Gate 1 passes
Classifier:  Intent = "device_control"; Gate 2 clears this stage
Enricher:    (no modules match this intent — stage skipped)
Processor:   HA bridge module converts intent into a Home Assistant API call
             and sets the final response directly; Gate 3 passes
Responder:   Final response already set — LLM call skipped entirely
Finalizer:   (no modules match this intent — stage skipped)
Output:      Kitchen lights turn on; TTS provider says "Lights on"
```
The voice control example never touches the LLM. The classifier identifies the intent, the processor handles the device call and sets the response, and the responder sees the response is already set and skips inference. Total latency is dominated by STT and TTS, not the model.

---

## 💻 Requirements

### Theoretical Minimums:
- **Hardware:** A GPU and a working computer. (llama.cpp supports CPU-only inference, but p-lanes is designed around GPU-pinned slots and is not optimized for CPU-only deployments.)
- **Software:** Python 3.12+, llama.cpp server, a supported STT/TTS provider for voice channels (optional), and Linux OS (may become Windows compatible later). Home Assistant is not a core dependency — p-lanes ships an optional HAOS custom integration (`brain_conversation`) for voice and chat UI.

### Tested Build (Development Environment):
- **CPU:** Intel Core Ultra 7
- **RAM:** 32GB
- **SSD:** 1TB, NVMe
- **GPU:** NVIDIA RTX 5060 Ti (16GB)
- **OS:** Proxmox VE (bare-metal hypervisor), HAOS on VM, LXC container for p-lanes + llama.cpp

---

## 🗺️ Roadmap

p-lanes is currently in active development. v0.3.0 completed the architectural transition to a modular "microkernel" structure. v0.4.0 focused on core hardening, voice pipeline, and external integration. v0.5.0 completed provider isolation and the normalized input contract.

### Project History

- **v0.1.0:** Monolithic structure. Proven concept with full text-chat functionality. (Modular nightmare.)
- **v0.2.0:** First major redesign. Ported to Python package format and separated core logic from modules.
- **v0.3.0:** Architectural split into drop-in components. Modules and providers self-contained with auto-discovery, self-declared manifests, and isolated config files. Core config no longer touched by addons.
- **v0.4.0:** Core hardening — utility lane full implementation, summarization double-fire and gate-leak fixes, security escalation fix, circular import refactor (`core/gates.py`), streaming post-processor fix, unsafe SSE JSON fix. Voice pipeline — Whisper STT and Kokoro TTS providers, WebSocket `/channel/voice` endpoint. HAOS custom integration — conversation, STT, and TTS entities with user name routing. `users.yaml` split to keep real names out of git.
- **v0.5.0:** (Current) Provider isolation — each provider is a fully self-contained package with its own config, no provider config in core. `MessageEnvelope` normalized input contract — all channels (text, voice, API, HA) normalize to the same envelope before core; pipeline carries it throughout. `device_id` return address for future multi-satellite routing. `TranscribeResult` replaces bare string from STT providers, carrying language and confidence metadata. `get_user(None)` guest fallback for unidentified speakers (voice print path).

### Development Status

- [x] Design layout and system flow.
- [x] Kernel core coding and dispatch logic.
- [x] Core configuration implementation (global slots, user windows, system permissions, stage security floors).
- [x] Basic channel setup — text/API provider, stability testing.
- [x] Public alpha release: upload active builds to GitHub.
- [x] Voice channel implementation (STT + TTS providers, WebSocket `/channel/voice`, HAOS integration).
- [ ] Multi-channel support — simultaneous voice + chat on the same user slot (implemented, not yet stress-tested under concurrent load).
- [ ] Pipeline stage implementation — Classifier, Enricher, Processor, and Finalizer stages (currently the pipeline is a flat `handle_message`/`handle_stream`; staged dispatch is the design target).
- [ ] Gate 2 (stage-level access floors) and Gate 3 (per-module permissions) enforcement — Gate 1 identity check is live; Gates 2 and 3 are config skeleton only.
- [ ] Basic classifier and enricher modules (Home Assistant device control integration).
- [ ] Transcript SSE stream for live conversation mirroring.
- [ ] Vision channel — multimodal input provider (text + images via Qwen3-VL).
- [ ] Universal installer script and `installer.py` for automated setup.
- [ ] Provider and module templates (documentation for community contributors).
- [ ] Cross-platform validation and final stability testing.

---

## ⚖️ License & Attribution

**p-lanes** is licensed under the **GNU AGPL-3.0**.

This is a **Copyleft** project: you are free to modify and share it, but any derivative works must also be open-source, keep all original author attributions, and be licensed under the AGPL.

### Third-Party Components

p-lanes is an orchestrator. It does not bundle or depend on any specific STT or TTS engine — these are swappable providers. The following are commonly used with p-lanes, and their respective licenses remain in effect:
- **llama.cpp**: MIT License (required — the inference backend)
- **Whisper**: MIT License (default STT provider, swappable)
- **Kokoro**: Apache 2.0 License (default TTS provider, swappable)
- **Piper**: MIT License (alternative TTS provider)

*Original Author: Logicish (2026)*
