# modules/plant_checkin.py
#
# Author:  Logicish
# Company: Logic-Ish Designs
# Date:    4/18/2026
#
# ==================================================
# Plant health check-in pipeline.
# Processes a plant photo and stores a health snapshot
# in house.db for trend tracking.
#
# Plant identity comes from the filename — the LLM
# does not guess species. If the plant name doesn't
# exist in the plants table yet it is created.
#
# Health metrics extracted (all 0-100 unless noted):
#   leaf_color_health — overall leaf colour health
#   wilting_score     — wilting / drooping severity
#   disease_spots     — visible spot/lesion coverage %
#   new_growth        — new growth visible (0/1)
#   overall_vigor     — general plant vitality
#   condition         — short label (healthy / stressed / etc.)
#   notes             — free-text observations
#
# The model is instructed to normalise for lighting
# and assess true plant health, not apparent colour.
# Image is deleted after successful DB insert.
#
# Entry point: process_image(image_path) → dict | None
#   image_path filename (stem) is used as the plant name.
#   Underscores are converted to spaces.
#
# Knows about: core/llm (call_vision),
#              providers (get_db("house")).
# ==================================================

import json
import os
from datetime import datetime, timezone
from pathlib import Path

import structlog

import providers
from core import llm

log = structlog.get_logger()

_PROMPT_TEMPLATE = """\
This is a photo of a {species}. Assess its health.

Ignore lighting conditions — evaluate true plant health based on morphology, \
leaf structure, and visible symptoms, not apparent brightness or colour temperature.

Return ONLY valid JSON — no markdown, no explanation:
{{
  "leaf_color_health": <0-100, 100=perfectly green/healthy>,
  "wilting_score":     <0-100, 0=fully turgid, 100=severely wilted>,
  "disease_spots":     <0-100, percentage of foliage with spots or lesions>,
  "new_growth":        <0 or 1, 1 if new shoots or leaves are visible>,
  "overall_vigor":     <0-100, 100=thriving>,
  "condition":         "<one of: healthy | good | stressed | needs_water | disease_present | poor>",
  "notes":             "<one sentence of the most notable observation, or empty string>"
}}
"""


def _plant_name_from_path(image_path: str) -> str:
    return Path(image_path).stem.replace("_", " ").strip()


async def _ensure_plant(db, name: str) -> int:
    """Return plant_id for name, creating the record if needed."""
    row = await db.fetchone(
        "SELECT id FROM plants WHERE name = ? COLLATE NOCASE", (name,)
    )
    if row:
        return row["id"]
    plant_id = await db.execute(
        "INSERT INTO plants (name) VALUES (?)", (name,)
    )
    log.info("plant_created", name=name, id=plant_id)
    return plant_id


async def process_image(image_path: str) -> dict | None:
    """Run the plant health pipeline on an image.

    Returns the checkin dict on success, None on failure.
    Deletes the image after successful DB insert.
    """
    db = providers.get_db("house")
    if db is None or not db.is_ready:
        log.error("plant_checkin_no_db")
        return None

    plant_name = _plant_name_from_path(image_path)
    prompt     = _PROMPT_TEMPLATE.format(species=plant_name)

    log.info("plant_checkin_start", plant=plant_name, image=image_path)

    raw = await llm.call_vision(image_path, prompt, temperature=0.2, max_tokens=256)
    if not raw:
        log.error("plant_checkin_vision_failed", image=image_path)
        return None

    try:
        text   = raw.strip().lstrip("```json").lstrip("```").rstrip("```").strip()
        result = json.loads(text)
    except Exception as e:
        log.error("plant_checkin_parse_failed", error=str(e), raw=raw[:200])
        return None

    plant_id   = await _ensure_plant(db, plant_name)
    checked_at = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")

    checkin_id = await db.execute(
        """
        INSERT INTO plant_checkins
            (plant_id, checked_at, leaf_color_health, wilting_score,
             disease_spots, new_growth, overall_vigor, condition, notes)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            plant_id, checked_at,
            result.get("leaf_color_health"),
            result.get("wilting_score"),
            result.get("disease_spots"),
            result.get("new_growth"),
            result.get("overall_vigor"),
            result.get("condition", "").strip(),
            result.get("notes", "").strip(),
        ),
    )

    log.info("plant_checkin_stored",
             id=checkin_id, plant=plant_name,
             vigor=result.get("overall_vigor"),
             condition=result.get("condition"))

    try:
        os.remove(image_path)
        log.debug("plant_checkin_image_deleted", path=image_path)
    except Exception as e:
        log.warning("plant_checkin_image_delete_failed", path=image_path, error=str(e))

    return {
        "id":               checkin_id,
        "plant":            plant_name,
        "checked_at":       checked_at,
        "leaf_color_health": result.get("leaf_color_health"),
        "wilting_score":    result.get("wilting_score"),
        "disease_spots":    result.get("disease_spots"),
        "new_growth":       bool(result.get("new_growth")),
        "overall_vigor":    result.get("overall_vigor"),
        "condition":        result.get("condition"),
        "notes":            result.get("notes"),
    }
