# main.py
#
# Author:  Logicish
# Company: Logic-Ish Designs
# Date:    5/15/2026
#
# ==================================================
# Microkernel entry point. v0.7.0.
# Knows about: slots, llm, service, transport,
#              summarizer, events, log, pipeline,
#              broadcast, providers, envelope,
#              tool_registry, tool_adapter.
#
# v0.7.0 pipeline:
#   Channel → Transporter → Classifier
#   → Tool Pipeline (fast-path | tool LLM)
#   → Conv LLM (always — persona guarantee)
#   → Responder → Finalizer → Channel
#
# Tool pipeline paths:
#   fast_path_tool set  → execute @tool directly (no tool LLM)
#   tool_hint set       → tool LLM extracts args → execute @tool
#   none of the above   → plain conv LLM (general conversation)
#
# Conv LLM always runs. Tool results (or classifier directives)
# are injected as extra_messages so the persona always
# narrates the response.
#
# Enricher phase is skipped — all enrichers have been
# converted to @tool handlers. finance_process is the
# only legacy exception and requires a dedicated admin path.
#
# Run with:
#   uvicorn main:app --host 0.0.0.0 --port 7860
# ==================================================

# ==================================================
# Version
# ==================================================

VERSION = "0.7.0"

# ==================================================
# Imports
# ==================================================
import asyncio
from contextlib import asynccontextmanager
from typing import AsyncIterator, TYPE_CHECKING

import aiohttp
import structlog
from fastapi import FastAPI

import config
from config import LLM_TIMEOUT, CONTEXT_PER_SLOT
from core.log import setup_logging
from core import llm, slots, summarizer, broadcast, scheduler
from core import tool_registry, tool_adapter, turn_log, gpu_guard
from core.llm import LLMContextOverflow
from core.envelope import MessageEnvelope
from core.pipeline import PipelineContext
from core.transport import create_routes
from service import service as svc
import providers

if TYPE_CHECKING:
    from core.slots import User

log = structlog.get_logger()

# max tool LLM round-trips per turn (guard against infinite loops)
_TOOL_LOOP_MAX = 2

# same placement for turns where no tool ran — a system-prompt rule alone gets
# buried under long history and the persona invents actions ("Timer set.")
_NO_TOOL_MSG = {
    "role": "user",
    "content": (
        "[System note: No tool or device action ran this turn. Do not claim to have "
        "set, added, played, sent, called, locked, saved, reminded, or changed anything, "
        "and do not report any device state. If the user asked you to control a device "
        "or set up something like a timer, list or reminder, say plainly you can't do "
        "that right now — no invented reason, no offer to do it later. Talking, "
        "answering, joking, telling stories and picking random things are fine.]"
    ),
}


# reply when llm is held (core/gpu_guard.py, failed recovery)
_HOLD_MSG = {
    "gpu_hot":      "My hardware is running hot, so I'm taking a short break to cool down. Try again in a few minutes.",
    "gpu_critical": "My hardware overheated, so I've shut my brain off to cool down. I'll be back in a few minutes.",
}
_HOLD_MSG_DEFAULT = "My language model is offline right now. I've flagged it for the admin."


def _held_reply(turn: "turn_log.Turn") -> str | None:
    reason = llm.hold_reason()
    if reason is None:
        return None
    turn.status, turn.error = "held", reason
    return _HOLD_MSG.get(reason, _HOLD_MSG_DEFAULT)


def _pick_profile(ctx: PipelineContext, user: "User") -> dict:
    """Choose the per-turn sampling profile (config sampling_profiles).
    Tool ran → relay; disabled feature → feature_off; think → think
    (only if the slot has room); else the router's choice or chat."""
    if ctx.tool_result is not None:
        name = "relay"
    elif any(t.startswith("disabled:") for t in ctx.tags):
        name = "feature_off"
    elif ctx.thinking:
        used = user.last_total_tokens or 0
        if CONTEXT_PER_SLOT - used >= config.THINK_MIN_HEADROOM:
            name = "think"
        else:
            # not enough slot left for think block + answer — the 2026-10-06
            # runs returned EMPTY replies here; answer without thinking
            ctx.thinking = False
            name = "factual"
            log.info("think_skipped_no_headroom", user_id=user.user_id, used=used)
    else:
        name = ctx.sampling_profile or "chat"
    ctx.sampling_profile = name
    ctx.tags.append(f"profile:{name}")
    return config.SAMPLING_PROFILES.get(name, {})


def _tool_note(directive: str) -> dict:
    # the tool result + instruction, placed right before the user's message.
    # Left only in the system prompt it was ignored even at temp 0.2 ("no smart
    # locks" → "the front door is wide open", fix-check 2026-10-06).
    return _note(f"{directive} This is the only tool result this turn — do not "
                 "claim any other action or state.")


# self-harm / death cues → a care note for this turn (house rule alone was
# ignored: "if i die, will you miss me?" → "don't go dying on me just yet")
_CARE_CUES = (
    "if i die", "if i died", "when i die", "want to die", "wanna die",
    "kill myself", "killing myself", "end it all", "end my life", "suicid",
    "self harm", "self-harm", "hurt myself", "hurting myself", "cut myself",
    "don't want to be here", "dont want to be here", "not want to be here",
    "wish i was dead", "wish i were dead", "better off without me",
)

_CARE_NOTE = (
    "The user mentioned dying or hurting themselves. Answer warmly and "
    "sincerely, without jokes or sarcasm. Gently ask if they're okay and "
    "whether something is going on. Stay in character but be kind."
)


def _care_note(ctx: PipelineContext) -> dict | None:
    text = ctx.raw_message.lower()
    if any(cue in text for cue in _CARE_CUES):
        ctx.tags.append("care_note")
        return _note(_CARE_NOTE)
    return None


# switched-off features → fixed reply. With only a directive the persona still
# answered "Timer set for 10 minutes." / "Yeah, the Lakers won." (fix-check
# 2026-10-06), so these turns skip the LLM.
_FEATURE_OFF_WHAT = {
    "outside_weather": "check the weather",
    "timer_alarm":     "set timers or alarms",
    "add_note":        "keep notes, lists or reminders",
    "media_control":   "play music or control the TV",
    "web_search":      "look up live info like scores, news, prices or store hours",
    "local_search":    "search your saved notes",
    "verify_last":     "fact-check answers",
    "system_status":   "check the servers",
    "list_commands":   "list my commands",
    "finance_process": "process finance files",
}


# router safety net / unsupported bucket (router tune 2026-10-07) — "call
# emergency services" used to get the scores/news off-message, "call mom"
# got "I can try calling your mom…"
_EMERGENCY_REPLY = (
    "I can't call anyone from here. If this is an emergency, call 911 right now."
)
_UNSUPPORTED_REPLY = (
    "Sorry, I can't do that. I'm not connected to phones, messages, "
    "deliveries or maps."
)


def _fixed_reply(ctx: PipelineContext) -> str | None:
    """Replies the LLM must not improvise: a failed / empty tool result is
    repeated verbatim, a disabled feature gets a plain 'can't do that yet'."""
    if ctx.tool_result is not None and ctx.tool_name and \
            tool_registry.is_failure(ctx.tool_name, ctx.tool_result):
        return ctx.tool_result
    if ctx.intent == "emergency":
        return _EMERGENCY_REPLY
    if ctx.intent == "unsupported":
        return _UNSUPPORTED_REPLY
    for tag in ctx.tags:
        if tag.startswith("disabled:"):
            intent = tag.split(":")[1]
            what = _FEATURE_OFF_WHAT.get(intent, "do that")
            return f"Sorry, I can't {what} yet. That feature is switched off for now."
    return None


# topics where the persona kept producing harmful takes (Stalin "pure
# efficiency", Genghis Khan "peak efficiency") despite the house rule
_TOPIC_NOTES = (
    (("dictator", "hitler", "stalin", "mussolini", "pol pot", "genocide"),
     "Do not name a favorite dictator or praise any dictator, tyrant or "
     "atrocity. In character, say briefly that you don't have one and why."),
)


def _topic_note(ctx: PipelineContext) -> dict | None:
    text = ctx.raw_message.lower()
    for cues, note in _TOPIC_NOTES:
        if any(c in text for c in cues):
            ctx.tags.append("topic_note")
            return _note(note)
    return None


def _note(text: str) -> dict:
    # classifier directive as a user-role note right before the user's message
    return {"role": "user", "content": f"[System note: {text}]"}


# ==================================================
# Lifespan
# ==================================================

@asynccontextmanager
async def lifespan(app: FastAPI):
    setup_logging()
    log.info("p_lanes_starting", version=VERSION)

    slots.init_all_users()

    import modules  # noqa: F401 — triggers @register and @tool decorators
    scheduler.discover_jobs()

    providers.autodiscover()

    async with aiohttp.ClientSession(
        timeout=aiohttp.ClientTimeout(total=LLM_TIMEOUT)
    ) as session:
        ready = await llm.start(session)
        if not ready:
            log.error("llm_start_failed")

        # before anything that can send work to the GPU
        await gpu_guard.start()

        await providers.start_all()
        await summarizer.start_scheduler()
        await summarizer.start_background_loop()
        await scheduler.start()

        log.info("p_lanes_ready", version=VERSION,
                 tools=tool_registry.tool_count(),
                 handlers=tool_registry.handler_count())
        yield
        log.info("p_lanes_shutting_down")

        await scheduler.stop()
        await gpu_guard.stop()
        await summarizer.stop_scheduler()
        await summarizer.stop_background_loop()
        await turn_log.drain()
        await providers.stop_all()

    slots.shutdown_all()
    await llm.stop()
    log.info("p_lanes_stopped")


# ==================================================
# App
# ==================================================

app = FastAPI(title="p-lanes", version=VERSION, lifespan=lifespan)


# ==================================================
# Tool pipeline helpers
# ==================================================

async def _run_tool_llm(
    manifest: list[dict],
    ctx: PipelineContext,
    user: "User",
) -> tuple[bool, bool]:
    """
    Call the tool LLM with the given manifest. If it selects a tool,
    execute it and populate ctx.directive.

    Returns (directive_set, tool_executed):
      directive_set  — ctx.directive was populated (tool result OR P1 decline text)
      tool_executed  — a real @tool handler ran (False for P1 declines)

    Uses the guest slot (no conversation history). Loops at most
    _TOOL_LOOP_MAX times — v0.7.0 breaks after the first tool call.
    """
    tool_messages  = [{"role": "user", "content": ctx.raw_message}]
    tool_executed  = False

    for _ in range(_TOOL_LOOP_MAX):
        try:
            content, tool_calls_raw, finish_reason = await llm.call_with_tools(
                messages      = tool_messages,
                tools         = manifest,
                fallback_slot = user.slot,
            )
        except Exception as e:
            log.error("tool_llm_failed", error=str(e), user_id=user.user_id)
            break

        ctx.tool_call_count += 1

        if finish_reason != "tool_calls" or not tool_calls_raw:
            # the tool LLM's own prose is NOT a result — it answered questions
            # itself and the persona relayed it as fact ("Lakers won 112-106",
            # "store closes at 9 PM"; review 2026-10-06). Treat as no tool.
            log.info("tool_llm_no_call",
                     finish_reason=finish_reason, user_id=user.user_id,
                     has_directive=bool(content))
            break  # tool_executed stays False

        tc = tool_adapter.translate(tool_calls_raw)
        if not tc:
            log.warning("tool_llm_bad_translation", user_id=user.user_id)
            break

        result        = await tool_registry.execute(tc.name, tc.args, ctx)
        ctx.directive = tool_registry.build_directive(tc.name, result)
        ctx.tool_name, ctx.tool_args, ctx.tool_result = tc.name, tc.args, result
        tool_executed = True

        log.info("tool_llm_executed",
                 tool=tc.name, call_count=ctx.tool_call_count,
                 user_id=user.user_id)

        # preserve multi-turn context for future extension
        tool_messages.append(
            tool_adapter.build_assistant_message(tool_calls_raw, content or "")
        )
        tool_messages.append(
            tool_adapter.build_tool_result_message(tc.call_id, result)
        )

        break  # v0.7.0: one tool call per turn

    return ctx.directive is not None, tool_executed


async def _resolve_tool(
    ctx: PipelineContext,
    user: "User",
) -> list[dict]:
    """
    Run the tool pipeline. Returns extra_messages for the conv LLM —
    always includes a grounding note placed right before the user's message.

    Path 0 — classifier pre-set directive:
      A classifier module (e.g. flag_reply) already determined the
      response and stored it in ctx.directive. Return it immediately.

    Path 1 — fast-path:
      Router had high confidence on a non-arg-extraction intent and
      resolved ctx.fast_path_tool. Execute the @tool handler directly.

    Path 2 — tool LLM:
      Router set ctx.tool_hint (uncertain routing or arg-extraction
      intent). Send the manifest to the tool LLM; it selects and calls
      the tool. Directive is built from the result.
    """

    # path 0: classifier already built the directive
    if ctx.directive:
        log.debug("tool_resolve_classifier_directive",
                  intent=ctx.intent, user_id=ctx.user.user_id)
        return [{"role": "system", "content": ctx.directive}, _note(ctx.directive)]

    # path 1: fast-path — execute @tool directly, no tool LLM
    if ctx.fast_path_tool:
        if tool_registry.is_registered(ctx.fast_path_tool):
            result        = await tool_registry.execute(ctx.fast_path_tool, {}, ctx)
            ctx.directive = tool_registry.build_directive(ctx.fast_path_tool, result)
            ctx.tool_name, ctx.tool_args, ctx.tool_result = ctx.fast_path_tool, {}, result
            ctx.tool_call_count = 1
            log.info("fast_path_executed",
                     tool=ctx.fast_path_tool, user_id=ctx.user.user_id)
            return [{"role": "system", "content": ctx.directive}, _tool_note(ctx.directive)]
        log.warning("fast_path_tool_unregistered",
                    tool=ctx.fast_path_tool, user_id=ctx.user.user_id)
        return [_NO_TOOL_MSG]

    # path 2: tool LLM — arg extraction or uncertain routing
    if ctx.tool_hint is not None or ctx.tool_candidates is not None:
        manifest = tool_registry.get_manifest(
            security_level = ctx.user.security_level,
            candidates     = ctx.tool_candidates,
        )
        if manifest:
            directive_set, tool_ran = await _run_tool_llm(manifest, ctx, user)
            if directive_set and ctx.directive:
                msgs = [{"role": "system", "content": ctx.directive}]
                msgs.append(_tool_note(ctx.directive) if tool_ran else _NO_TOOL_MSG)
                return msgs

    return [_NO_TOOL_MSG]


# ==================================================
# Pipeline -- Blocking
# ==================================================

async def handle_message(envelope: MessageEnvelope) -> str:
    # every exit path ends in exactly one turn_log row
    turn = turn_log.begin(envelope)
    try:
        turn.response = await _handle_message(envelope, turn)
        if turn.status == "pending":
            turn.status = "ok"
        return turn.response
    except Exception as e:
        turn.status, turn.error = "error", str(e)
        raise
    finally:
        turn_log.submit(turn)


async def _handle_message(envelope: MessageEnvelope, turn: "turn_log.Turn") -> str:
    user = slots.get_user(envelope.user_id)
    if user is None:
        turn.status = "denied"
        return "Access denied."

    user.last_request = None
    ctx = PipelineContext(user=user, envelope=envelope)
    turn.user, turn.ctx = user, ctx

    # gpu_guard / failed recovery -- answer without touching the LLM
    held = _held_reply(turn)
    if held:
        return held

    # --- classifier phase ---
    # enricher phase is skipped — @tool handlers replace all enrichers.
    # finance_process (legacy enricher, ADMIN only) is the one exception
    # and will be migrated in a follow-up.
    ctx = await svc.run_phase("classifier", ctx)
    turn.ctx = ctx

    if ctx.aborted:
        turn.status = "aborted"
        return ctx.abort_reason or "Request cancelled."

    # --- slot lock (in-place summarization in progress) ---
    if user.slot_lock.locked():
        released = await summarizer.wait_for_lock(user)
        if not released:
            turn.status = "busy"
            return "Give me just a second..."

    # --- tool pipeline ---
    extra_messages = await _resolve_tool(ctx, user)
    sampling       = _pick_profile(ctx, user)
    care           = _care_note(ctx)
    if care:
        extra_messages = [*extra_messages, care]
        sampling       = {**sampling, "temperature": 0.5, "dry_multiplier": 0.0}
    topic          = _topic_note(ctx)
    if topic:
        extra_messages = [*extra_messages, topic]
    fixed          = _fixed_reply(ctx)

    if fixed:
        # no LLM — keep history consistent so the next turn sees the exchange
        user.add_message("user", ctx.raw_message)
        user.add_message("assistant", fixed)
        ctx.response_text = fixed
        ctx.tags.append("fixed_reply")
        ctx = await svc.run_post_processor(ctx)
        turn.ctx = ctx
        result = ctx.final_output or fixed
        broadcast.publish(user.user_id, {"event": "response", "data": result})
        return result

    # --- conv LLM (always runs — persona guarantee) ---
    try:
        response = await llm.call(
            user, ctx.raw_message,
            temperature_override = ctx.temperature_override,
            thinking             = ctx.thinking,
            extra_messages       = extra_messages,
            sampling             = sampling,
        )
    except LLMContextOverflow:
        await summarizer.emergency_summarize(user)
        try:
            response = await llm.call(
                user, ctx.raw_message,
                temperature_override = ctx.temperature_override,
                thinking             = ctx.thinking,
                extra_messages       = extra_messages,
                sampling             = sampling,
            )
        except LLMContextOverflow:
            log.error("context_overflow_after_summarize", user_id=user.user_id)
            turn.status = "overflow"
            return "My memory is full. I've cleaned up what I can -- try again."

    ctx.response_text = response.content
    ctx.total_tokens  = response.total_tokens
    ctx.truncated     = response.truncated
    ctx.elapsed       = response.elapsed

    if user.flag_crit:
        asyncio.create_task(summarizer.summarize_if_needed(user))

    # --- responder + finalizer ---
    ctx = await svc.run_post_processor(ctx)
    turn.ctx = ctx

    result = ctx.final_output or ctx.response_text
    broadcast.publish(user.user_id, {"event": "response", "data": result})
    return result


# ==================================================
# Pipeline -- Streaming
# ==================================================

async def handle_stream(envelope: MessageEnvelope) -> AsyncIterator[str]:
    # every exit path ends in exactly one turn_log row —
    # including the client dropping mid-stream (GeneratorExit)
    turn  = turn_log.begin(envelope)
    parts = []
    try:
        async for chunk in _handle_stream(envelope, turn):
            parts.append(chunk)
            yield chunk
        if turn.status == "pending":
            turn.status = "ok"
    except Exception as e:
        turn.status, turn.error = "error", str(e)
        raise
    finally:
        if turn.status == "pending":
            turn.status = "disconnected"
        turn.response = "".join(parts)
        turn_log.submit(turn)


async def _handle_stream(envelope: MessageEnvelope, turn: "turn_log.Turn") -> AsyncIterator[str]:
    user = slots.get_user(envelope.user_id)
    if user is None:
        turn.status = "denied"
        yield "Access denied."
        return

    user.last_request = None
    ctx = PipelineContext(user=user, envelope=envelope)
    turn.user, turn.ctx = user, ctx

    # gpu_guard / failed recovery -- answer without touching the LLM
    held = _held_reply(turn)
    if held:
        yield held
        return

    # --- classifier phase ---
    ctx = await svc.run_phase("classifier", ctx)
    turn.ctx = ctx

    if ctx.aborted:
        turn.status = "aborted"
        yield ctx.abort_reason or "Request cancelled."
        return

    # --- slot lock ---
    if user.slot_lock.locked():
        released = await summarizer.wait_for_lock(user)
        if not released:
            turn.status = "busy"
            yield "Give me just a second..."
            return

    # --- tool pipeline ---
    extra_messages = await _resolve_tool(ctx, user)
    sampling       = _pick_profile(ctx, user)
    care           = _care_note(ctx)
    if care:
        extra_messages = [*extra_messages, care]
        sampling       = {**sampling, "temperature": 0.5, "dry_multiplier": 0.0}
    topic          = _topic_note(ctx)
    if topic:
        extra_messages = [*extra_messages, topic]
    fixed          = _fixed_reply(ctx)

    if fixed:
        user.add_message("user", ctx.raw_message)
        user.add_message("assistant", fixed)
        ctx.response_text = fixed
        ctx.tags.append("fixed_reply")
        yield fixed
        broadcast.publish(user.user_id, {"event": "token", "data": fixed})
        ctx = await svc.run_post_processor(ctx)
        turn.ctx = ctx
        broadcast.publish(user.user_id, {"event": "done", "data": ""})
        return

    # --- conv LLM streaming (always runs) ---
    accumulated = []

    try:
        async for chunk in llm.call_stream(
            user, ctx.raw_message,
            temperature_override = ctx.temperature_override,
            thinking             = ctx.thinking,
            extra_messages       = extra_messages,
            sampling             = sampling,
        ):
            accumulated.append(chunk)
            yield chunk
            broadcast.publish(user.user_id, {"event": "token", "data": chunk})

    except LLMContextOverflow:
        accumulated.clear()
        await summarizer.emergency_summarize(user)
        try:
            async for chunk in llm.call_stream(
                user, ctx.raw_message,
                temperature_override = ctx.temperature_override,
                thinking             = ctx.thinking,
                extra_messages       = extra_messages,
                sampling             = sampling,
            ):
                accumulated.append(chunk)
                yield chunk
                broadcast.publish(user.user_id, {"event": "token", "data": chunk})
        except LLMContextOverflow:
            log.error("stream_overflow_after_summarize", user_id=user.user_id)
            turn.status = "overflow"
            yield "My memory is full. I've cleaned up what I can -- try again."
            return

    if user.flag_crit:
        asyncio.create_task(summarizer.summarize_if_needed(user))

    # --- responder + finalizer (silent, side effects only) ---
    ctx.response_text = "".join(accumulated)
    ctx.total_tokens  = user.last_total_tokens
    ctx.elapsed       = user.last_elapsed
    ctx.truncated     = user.last_truncated
    ctx = await svc.run_post_processor(ctx)
    turn.ctx = ctx

    broadcast.publish(user.user_id, {"event": "done", "data": ""})


# ==================================================
# Wire Routes
# ==================================================

create_routes(app, handle_message, handle_stream)
