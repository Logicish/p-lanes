# main.py
#
# Author:  Logicish
# Company: Logic-Ish Designs
# Date:    2/26/2026
#
# ==================================================
# Microkernel entry point.
# Knows about: slots, llm, service, transport,
#              summarizer, events, log, pipeline.
# Wires them at startup. Orchestrates the pipeline:
#   Channel → Transporter → Classifier → Enricher
#   → Processor → Responder → Finalizer → Channel
# Must never contain business logic.
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

from config import LLM_TIMEOUT
from core.log import setup_logging
from core import llm, slots, summarizer
from core.pipeline import PipelineContext
from core.transport import create_routes
from service import service as svc

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

    # start LLM with a shared session for the entire app lifetime
    async with aiohttp.ClientSession(
        timeout=aiohttp.ClientTimeout(total=LLM_TIMEOUT)
    ) as session:
        ready = await llm.start(session)
        if not ready:
            log.error("llm_start_failed")

        # start background tasks (scheduler + idle/health loop)
        await summarizer.start_scheduler()
        await summarizer.start_background_loop()

        log.info("p_lanes_ready")
        yield
        log.info("p_lanes_shutting_down")

        # stop background tasks
        await summarizer.stop_scheduler()
        await summarizer.stop_background_loop()

    slots.shutdown_all()
    await llm.stop()
    log.info("p_lanes_stopped")


# ==================================================
# App
# ==================================================

app = FastAPI(title="p-lanes", lifespan=lifespan)


# ==================================================
# Pipeline — Blocking
# ==================================================

async def handle_message(user_id: str, message: str) -> str:
    user = slots.get_user(user_id)

    # build pipeline context
    ctx = PipelineContext(user=user, raw_message=message)

    # --- classifier + enricher ---
    ctx = await svc.run_pre_processor(ctx)

    if ctx.aborted:
        return ctx.abort_reason or "Request cancelled."

    # --- check slot lock (summarization in progress) ---
    if user.slot_lock.locked():
        released = await summarizer.wait_for_lock(user)
        if not released:
            return "Give me just a second..."

    # --- processor (LLM) ---
    if not ctx.skip_processor:
        prompt = ctx.build_enriched_prompt()
        response = await llm.call(user, prompt)
        ctx.response_text  = response.content
        ctx.total_tokens   = response.total_tokens
        ctx.truncated      = response.truncated
        ctx.elapsed        = response.elapsed

        # fire background summarization if critical
        if user.flag_crit:
            asyncio.create_task(summarizer.summarize_if_needed(user))

    # --- responder + finalizer ---
    ctx = await svc.run_post_processor(ctx)

    # return finalizer output if set, otherwise response_text
    return ctx.final_output or ctx.response_text


# ==================================================
# Pipeline — Streaming
# ==================================================

async def handle_stream(user_id: str, message: str) -> AsyncIterator[str]:
    user = slots.get_user(user_id)

    # build pipeline context
    ctx = PipelineContext(user=user, raw_message=message)

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

    # --- processor (LLM) — streaming ---
    if not ctx.skip_processor:
        prompt = ctx.build_enriched_prompt()
        async for chunk in llm.call_stream(user, prompt):
            yield chunk

        # fire background summarization if critical
        if user.flag_crit:
            asyncio.create_task(summarizer.summarize_if_needed(user))

    elif ctx.response_text:
        # classifier/enricher provided a response without LLM
        yield ctx.response_text

    # note: responder/finalizer run post-stream
    # the complete response is in user.conversation_history
    # finalizer can modify output for next request or log


# ==================================================
# Wire Routes
# ==================================================

create_routes(app, handle_message, handle_stream)