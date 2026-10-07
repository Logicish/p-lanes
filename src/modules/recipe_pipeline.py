# modules/recipe_pipeline.py
#
# Author:  Logicish
# Company: Logic-Ish Designs
# Date:    4/18/2026
#
# ==================================================
# Two-pass recipe ingestion pipeline.
# Converts a recipe image to structured DB record.
#
# Pass 1 — Vision: image → raw transcription
# Pass 2 — Structure: raw text → title, ingredients,
#           instructions, servings, calories_per_serving,
#           prep_min, cook_min (LLM estimates nulls)
#
# needs_llm is set TRUE if any estimated field is null,
# so the nightly gap-fill job can target those rows.
# Image is deleted after successful ingestion.
#
# Entry point: process_image(image_path) → dict | None
#
# Knows about: core/llm (call_vision, call_internal),
#              providers (get_db("recipes")).
# ==================================================

import json
import os

import structlog

import providers
from core import llm

log = structlog.get_logger()

_VISION_PROMPT = (
    "Transcribe this handwritten recipe exactly as written. "
    "Output only the transcribed text, nothing else."
)

_STRUCTURE_PROMPT = """\
You are given raw transcribed recipe text. Extract and structure it.

Return ONLY valid JSON — no markdown, no explanation:
{
  "title": "string",
  "ingredients": ["string", ...],
  "instructions": ["string", ...],
  "servings": integer or null,
  "calories_per_serving": integer or null,
  "prep_min": integer or null,
  "cook_min": integer or null
}

Rules:
- Compress instructions to clear, concise steps
- Estimate servings, calories_per_serving, prep_min, cook_min from context if not stated
- Use null only if you genuinely cannot estimate
- calories_per_serving is per serving, not total
- prep_min and cook_min are integers in minutes

Recipe text:
"""


async def process_image(image_path: str) -> dict | None:
    """Run the two-pass pipeline on a recipe image.

    Returns the inserted recipe dict on success, None on failure.
    Deletes the image after successful DB insert.
    """
    db = providers.get_db("recipes")
    if db is None or not db.is_ready:
        log.error("recipe_pipeline_no_db")
        return None

    # --- Pass 1: vision transcription ---
    log.info("recipe_pipeline_pass1", image=image_path)
    raw_text = await llm.call_vision(image_path, _VISION_PROMPT, temperature=0.2, max_tokens=512)
    if not raw_text:
        log.error("recipe_pipeline_vision_failed", image=image_path)
        return None
    log.debug("recipe_pipeline_transcription", chars=len(raw_text))

    # --- Pass 2: structure + estimate ---
    log.info("recipe_pipeline_pass2")
    response = await llm.call_internal(
        messages=[{"role": "user", "content": _STRUCTURE_PROMPT + raw_text}],
        temperature=0.2,
        max_tokens=768,
    )
    try:
        # strip markdown fences if the model wraps output anyway
        text = response.content.strip().lstrip("```json").lstrip("```").rstrip("```").strip()
        recipe = json.loads(text)
    except Exception as e:
        log.error("recipe_pipeline_parse_failed", error=str(e), raw=response.content[:200])
        return None

    title        = recipe.get("title", "").strip()
    ingredients  = recipe.get("ingredients", [])
    instructions = recipe.get("instructions", [])

    if not title or not ingredients or not instructions:
        log.error("recipe_pipeline_incomplete", title=title)
        return None

    servings      = recipe.get("servings")
    calories      = recipe.get("calories_per_serving")
    prep_min      = recipe.get("prep_min")
    cook_min      = recipe.get("cook_min")
    needs_llm     = int(any(v is None for v in [servings, calories, prep_min, cook_min]))

    # --- Store ---
    row_id = await db.execute(
        """
        INSERT INTO recipes
            (title, ingredients, instructions, servings,
             calories_per_serving, prep_min, cook_min, needs_llm)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            title,
            json.dumps(ingredients),
            json.dumps(instructions),
            servings, calories, prep_min, cook_min,
            needs_llm,
        ),
    )

    log.info("recipe_pipeline_stored",
             id=row_id, title=title, needs_llm=bool(needs_llm))

    # --- Delete image ---
    try:
        os.remove(image_path)
        log.debug("recipe_pipeline_image_deleted", path=image_path)
    except Exception as e:
        log.warning("recipe_pipeline_image_delete_failed", path=image_path, error=str(e))

    return {
        "id": row_id, "title": title,
        "ingredients": ingredients, "instructions": instructions,
        "servings": servings, "calories_per_serving": calories,
        "prep_min": prep_min, "cook_min": cook_min,
        "needs_llm": bool(needs_llm),
    }
