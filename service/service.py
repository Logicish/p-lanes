# service/service.py
#
# Author:  Logicish
# Company: Logic-Ish Designs
# Date:    2/26/2026
#
# ==================================================
# The pipeline dispatcher. Runs registered modules
# through phases in order:
#   classifier → enricher → [processor] → responder → finalizer
#
# Processor (LLM) is called by the kernel, not here.
# This module handles the phases before and after.
# Enforces GATE 2 — checks MODULE_PERMISSIONS before
# calling each module.
#
# Knows about: config (MODULE_PERMISSIONS, SecurityLevel),
#              events (phase registry), slots (permission),
#              pipeline (PipelineContext).
# ==================================================

# ==================================================
# Imports
# ==================================================
import structlog

from config import MODULE_PERMISSIONS, SecurityLevel
from core.events import get_phase, PHASES
from core.slots import check_permission
from core.pipeline import PipelineContext

log = structlog.get_logger()


# ==================================================
# Phase Runners
# ==================================================

async def run_phase(phase: str, ctx: PipelineContext) -> PipelineContext:
    # run all modules registered for a given phase
    # GATE 2: modules whose required permission exceeds
    # the user's security level are silently skipped
    if ctx.aborted:
        log.info("pipeline_aborted_skip_phase",
                 phase=phase, reason=ctx.abort_reason)
        return ctx

    modules = get_phase(phase)
    if not modules:
        return ctx

    for name, handler in modules.items():
        required = MODULE_PERMISSIONS.get(name, SecurityLevel.USER)

        if not check_permission(ctx.user, required):
            log.debug("gate2_skip",
                       module=name, phase=phase, required=required,
                       user_id=ctx.user.user_id, level=ctx.user.security_level)
            continue

        try:
            ctx = await handler(ctx)
        except Exception as e:
            log.error("module_failed",
                       module=name, phase=phase,
                       user_id=ctx.user.user_id, error=str(e))
            # module failure is non-fatal — continue pipeline

        if ctx.aborted:
            log.info("pipeline_aborted_by_module",
                     module=name, phase=phase, reason=ctx.abort_reason)
            break

    return ctx


async def run_pre_processor(ctx: PipelineContext) -> PipelineContext:
    # run classifier and enricher phases (before LLM)
    ctx = await run_phase("classifier", ctx)
    ctx = await run_phase("enricher", ctx)
    return ctx


async def run_post_processor(ctx: PipelineContext) -> PipelineContext:
    # run responder and finalizer phases (after LLM)
    ctx = await run_phase("responder", ctx)
    ctx = await run_phase("finalizer", ctx)
    return ctx