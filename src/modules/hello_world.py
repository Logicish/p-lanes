# modules/hello_world.py
#
# Author:  Logicish
# Company: Logic-Ish Designs
# Date:    3/6/2026
#
# ==================================================
# Hello-world test module.
# Validates the full module chain: auto-discovery,
# @register decorator, Gate 2 security check,
# classifier phase execution, skip_processor flag,
# and the response output path.
#
# Test commands:
#   "ping"  → "pong — pipeline is alive."
#   "test"  → brief system status
#
# These bypass the LLM entirely. Normal messages
# pass through untouched.
#
# Knows about: core/events (register),
#              core/pipeline (PipelineContext).
# ==================================================

# ==================================================
# Imports
# ==================================================
from core.events import register
from core.pipeline import PipelineContext

# ==================================================
# Classifier — intercept test commands
# ==================================================

@register("hello_world", "classifier")
async def classify(ctx: PipelineContext) -> PipelineContext:
    command = ctx.raw_message.strip().lower()

    if command == "ping":
        ctx.intent = "hello_command"
        ctx.skip_processor = True
        ctx.response_text = "pong — pipeline is alive."

    elif command == "test":
        user = ctx.user
        ctx.intent = "hello_command"
        ctx.skip_processor = True
        ctx.response_text = (
            f"slot: {user.slot} | "
            f"security: {user.security_level} | "
            f"history: {len(user.conversation_history)} msgs | "
            f"summary: {'yes' if user.summary else 'no'} | "
            f"warn: {user.flag_warn} | "
            f"crit: {user.flag_crit}"
        )

    return ctx
