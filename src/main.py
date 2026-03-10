# main.py
#
# Author:  Logicish
# Company: Logic-Ish Designs
# Date:    2/26/2026
#
# ==================================================
# Microkernel entry point.
# Knows about: slots, llm, service, transport,
#              summarizer, events, log, pipeline,
#              broadcast.
# Wires them at startup. Orchestrates the pipeline:
#   Channel -> Transporter -> Classifier -> Enricher
#   -> Processor -> Responder -> Finalizer -> Channel
# Must never contain business logic.
#
# Streaming post-processor:
#   After the LLM stream completes, accumulated text
#   is placed into ctx.response_text and the post-
#   processor (responder + finalizer) runs silently
#   for side effects. This unblocks responder modules
#   like TTS that need the complete response text.
#
# Run with:
#   uvicorn main:app --host 0.0.0.0 --port 7860
# ==================================================

# ==================================================
# Imports
# ==================================================
import asyncio
from contextlib import asynccontextmanager
from typing import AsyncIterator

import aiohttp
import structlog
from fastapi import FastAPI

import config
from config import LLM_TIMEOUT
from core.log import setup_logging
from core import llm, slots, summarizer, broadcast
from core.llm import LLMContextOverflow
from core.pipeline import PipelineContext
from core.transport import create_routes
from service import service as svc
import providers
from providers.whisper import WhisperProvider
from providers.kokoro import KokoroProvider

log = structlog.get_logger()

# ==================================================
# Lifespan
# ==================================================

@asynccontextmanager
async def lifespan(app: FastAPI):
    setup_logging()
    log.info("p_lanes_starting")

    # initialize all user slots
    slots.init_all_users()

    # discover and register modules
    import modules  # noqa: F401

    # register providers (conditional on config)
    if config.STT_ENABLED:
        providers.register_provider(WhisperProvider())
    if config.TTS_ENABLED:
        providers.register_provider(KokoroProvider())

    # start LLM with a shared session for the entire app lifetime
    async with aiohttp.ClientSession(
        timeout=aiohttp.ClientTimeout(total=LLM_TIMEOUT)
    ) as session:
        ready = await llm.start(session)
        if not ready:
            log.error("llm_start_failed")

        # start providers (non-fatal — degraded mode if unavailable)
        await providers.start_all()

        # start background tasks (scheduler + idle/health loop)
        await summarizer.start_scheduler()
        await summarizer.start_background_loop()

        log.info("p_lanes_ready")
        yield
        log.info("p_lanes_shutting_down")

        # stop background tasks
        await summarizer.stop_scheduler()
        await summarizer.stop_background_loop()

        # stop providers
        await providers.stop_all()

    slots.shutdown_all()
    await llm.stop()
    log.info("p_lanes_stopped")


# ==================================================
# App
# ==================================================

app = FastAPI(title="p-lanes", lifespan=lifespan)


# ==================================================
# Pipeline -- Blocking
# ==================================================

async def handle_message(
    user_id: str,
    message: str,
    conversation_id: str | None = None,
) -> str:
    user = slots.get_user(user_id)

    # build pipeline context
    ctx = PipelineContext(
        user=user,
        raw_message=message,
        conversation_id=conversation_id,
    )

    # --- classifier + enricher ---
    ctx = await svc.run_pre_processor(ctx)

    if ctx.aborted:
        return ctx.abort_reason or "Request cancelled."

    # --- check slot lock (in-place summarization in progress) ---
    if user.slot_lock.locked():
        released = await summarizer.wait_for_lock(user)
        if not released:
            return "Give me just a second..."

    # --- processor (LLM) ---
    if not ctx.skip_processor:
        prompt = ctx.build_enriched_prompt()

        try:
            response = await llm.call(user, prompt)
        except LLMContextOverflow:
            # context full -- summarize and retry once
            await summarizer.emergency_summarize(user)
            try:
                response = await llm.call(user, prompt)
            except LLMContextOverflow:
                # still overflowing after summarization -- something
                # is seriously wrong, but don't brick the user
                log.error("context_overflow_after_summarize",
                           user_id=user.user_id)
                return "My memory is full. I've cleaned up what I can -- try again."

        ctx.response_text  = response.content
        ctx.total_tokens   = response.total_tokens
        ctx.truncated      = response.truncated
        ctx.elapsed        = response.elapsed

        # fire background summarization if critical
        if user.flag_crit:
            asyncio.create_task(summarizer.summarize_if_needed(user))

    # --- responder + finalizer ---
    ctx = await svc.run_post_processor(ctx)

    result = ctx.final_output or ctx.response_text

    # broadcast to any listeners (no-op if disabled)
    broadcast.publish(user_id, {"event": "response", "data": result})

    return result


# ==================================================
# Pipeline -- Streaming
# ==================================================

async def handle_stream(
    user_id: str,
    message: str,
    conversation_id: str | None = None,
) -> AsyncIterator[str]:
    user = slots.get_user(user_id)

    # build pipeline context
    ctx = PipelineContext(
        user=user,
        raw_message=message,
        conversation_id=conversation_id,
    )

    # --- classifier + enricher ---
    ctx = await svc.run_pre_processor(ctx)

    if ctx.aborted:
        yield ctx.abort_reason or "Request cancelled."
        return

    # --- check slot lock ---
    if user.slot_lock.locked():
        released = await summarizer.wait_for_lock(user)
        if not released:
            yield "Give me just a second..."
            return

    # --- processor (LLM) -- streaming ---
    accumulated = []

    if not ctx.skip_processor:
        prompt = ctx.build_enriched_prompt()

        try:
            async for chunk in llm.call_stream(user, prompt):
                accumulated.append(chunk)
                yield chunk
                broadcast.publish(user_id, {"event": "token", "data": chunk})
        except LLMContextOverflow:
            # context full -- summarize and retry once
            accumulated.clear()
            await summarizer.emergency_summarize(user)
            try:
                async for chunk in llm.call_stream(user, prompt):
                    accumulated.append(chunk)
                    yield chunk
                    broadcast.publish(user_id, {"event": "token", "data": chunk})
            except LLMContextOverflow:
                log.error("stream_context_overflow_after_summarize",
                           user_id=user.user_id)
                yield "My memory is full. I've cleaned up what I can -- try again."
                return

        # fire background summarization if critical
        if user.flag_crit:
            asyncio.create_task(summarizer.summarize_if_needed(user))

    elif ctx.response_text:
        accumulated.append(ctx.response_text)
        yield ctx.response_text

    # --- responder + finalizer (silent, side effects only) ---
    # populate response_text and metadata so post-processor
    # modules have access to the complete response (e.g. TTS, logging)
    ctx.response_text  = "".join(accumulated)
    ctx.total_tokens   = user.last_total_tokens
    ctx.elapsed        = user.last_elapsed
    ctx.truncated      = user.last_truncated
    ctx = await svc.run_post_processor(ctx)

    # broadcast done to any listeners
    broadcast.publish(user_id, {"event": "done", "data": ""})


# ==================================================
# Wire Routes
# ==================================================

create_routes(app, handle_message, handle_stream)