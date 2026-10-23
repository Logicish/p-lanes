# p-lanes

A small Python service that sits in front of llama.cpp and runs a household AI assistant on one consumer GPU: a few known users, voice and text, smart-home control, and answers grounded in local reference data and web search.

[![License: AGPL v3](https://img.shields.io/badge/License-AGPL%20v3-blue.svg)](https://www.gnu.org/licenses/agpl-3.0)
[![Python 3.12+](https://img.shields.io/badge/python-3.12+-blue.svg)](https://www.python.org/downloads/release/python-3120/)

Status: pre-release, **v0.4.14**. It runs my house daily, but things still change often.

> Source is being stabilized privately; the code will be published here at a stable release.

---

## Why it exists

Most inference servers are built for many users who come and go. A house is the opposite: three or four people, the same ones every day, and the main thing that matters is how fast the assistant answers when someone talks to it.

llama.cpp keeps each conversation's KV cache in a *slot*. If the slot still holds your conversation, a new message only costs its own tokens. If it doesn't, the whole conversation is read again. Measured on the test machine with a 4.5k-token history:

| | Time to first token |
|---|---|
| Your slot still holds the conversation | ~0.13 s |
| Conversation has to be re-read | ~1.9 s |

p-lanes gives each user their own slot and keeps it that way:
- One slot per user, always the same one.
- The prompt only ever grows at the end. Per-turn notes (time, tool results) are saved in the history instead of being swapped in and out, so earlier parts never change.
- When a user goes idle, older turns are folded into a summary and the new, shorter prompt is pre-read in the background, so the next message starts warm.

In practice that keeps the first token at roughly 0.15–0.2 s per turn, regardless of how long the conversation is.

---

## How it's built

The core is small and micro-kernel-like: it handles identity, slots, the request pipeline and the LLM calls, and little else. Everything else registers itself at startup:

- **Tools**: handlers for device control, home state, web search and so on, described in a YAML file.
- **Modules**: single `.py` files hooked into a pipeline phase or the scheduler.
- **Providers**: self-contained packages for outside services (speech, smart home, embeddings, database, vector store), each with its own config.

A request goes through the same steps whatever channel it came from:

```
channel → identity gate → router → tool (optional) → LLM in the user's slot → post-processing → channel
```

- **Router**: embedding classifier plus a few keyword nets (emergency, "nevermind", short follow-ups). It decides whether a message is chat, a tool call, or a fact lookup.
- **Tools**: run directly when the router is confident, otherwise a small tool-picking LLM call on a utility lane decides. Failed or switched-off features return a fixed reply instead of letting the model improvise.
- **LLM**: always the user's own slot and persona. Tool results arrive as notes before the user's message; the model only phrases them.

---

## What it does

- **Per-user lanes**: own slot, persona, summary and turn log per user; a shared guest/utility lane.
- **Voice and text**: voice satellites and a web UI share one conversation per user. Replies stream to text-to-speech clause by clause, and the model is told whether a reply will be spoken or read.
- **Voice-print ID**: satellite audio is matched against enrolled voices to pick the user.
- **Smart-home control**: device control and state queries, using the names, aliases and rooms already set up in the smart-home system; "my light" means the speaker's light.
- **Grounded answers**: questions about named things are checked against local reference data first, then web search. Live questions (scores, prices, "who is the current…") go straight to web search.
- **RAG**: a vector store with shared and per-user collections.
- **Summaries and compaction**: idle compaction keeps the last few exchanges word for word and merges older ones into a capped summary.
- **Turn log**: every turn is stored per user in a database for later review, with automatic size limits.
- **GPU guard**: pauses and, if needed, stops the inference server when the GPU runs too hot, and brings it back afterwards.
- **Security**: two gates outside the LLM. Identity is checked before the pipeline runs; each module and device domain has a minimum security level. The model never makes access decisions.

---

## Limitations

These are deliberate trade-offs for a small, fixed household.

- **Slots are fixed.** Each user's slot is reserved for the life of the process; the context size is split evenly between slots.
- **One user, one slot.** A conversation can't spill into another slot; it has to be compacted before it fills.
- **Shared GPU.** Simultaneous requests share tokens per second.
- **VRAM is the ceiling.** The model, slots, embeddings and speech services all have to fit on one card.
- **9B-class model.** The model is small; grounding and fixed replies exist because it can't be trusted on facts or actions on its own.

---

## Requirements

- Linux, Python 3.12+
- llama.cpp server (CUDA build recommended)
- An NVIDIA GPU; p-lanes is built around GPU-resident slots
- Optional: speech-to-text and text-to-speech services, a smart-home system, a vector store, web search

**Test machine**
- Intel Core Ultra 7, 32 GB RAM, 1 TB NVMe
- NVIDIA RTX 5060 Ti 16 GB
- p-lanes and llama.cpp in a Linux container
- A 9B model (Q6_K), 4 slots × 8k context

---

## Versions

Version numbers are `0.MAJOR.MINOR` while the project is pre-release: the middle number moves on a structural change, the last one on fixes and additions.

- **v0.1**: single-file prototype; text chat works.
- **v0.2**: proper Python package; core separated from features.
- **v0.3**: drop-in modules and providers with auto-discovery and per-component config.
- **v0.4**: flow change from enricher modules to a tool registry and embedding router; turn log, GPU guard, sampling profiles.
  - Since then: vector-store RAG, voice-print ID, clause-streamed speech, prompt-cache layout and idle compaction with pre-warm, persona rewrite, router fixes from real use, grounded lookups, smart-home names and rooms.

---

## Roadmap

- Device commands by room ("my bedroom lights" = every light in my room) and satellite location for "the lights".
- Follow-up memory for devices ("no, it's still on", "make it red instead").
- Latency masking for slower turns (lookups, compaction).
- Local documents in RAG: manuals, notes, PDF ingestion.
- Router fine-tuning from the turn log.
- Re-enabling parked features one at a time: timers, notes, media routing.

---

## License

p-lanes is licensed under the **GNU AGPL-3.0**. You're free to modify and share it; derivative works must stay open source under the AGPL and keep the original attribution. Full text in [LICENSE](LICENSE); third-party notes in [NOTICE](NOTICE).

p-lanes doesn't bundle its backends. It requires **llama.cpp** (MIT) for inference; everything else is a swappable provider.

*Original author: Logicish (2026)*
