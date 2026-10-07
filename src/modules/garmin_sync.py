# modules/garmin_sync.py
#
# Author:  Logicish
# Company: Logic-Ish Designs
# Date:    4/18/2026
#
# ==================================================
# Daily Garmin Connect data sync.
# Pulls health metrics for today and upserts into
# the root user DB at 10:00 AM every day.
#
# Pulls today's date so sleep/HRV data (attributed
# to the wake-up date by Garmin) is always captured.
#
# Metrics collected:
#   hrv              — last night average (ms)
#   sleep_score      — Garmin overall sleep score
#   sleep_min        — total sleep in minutes
#   steps            — step count
#   stress           — average stress score
#   calories         — total kilocalories burned
#   resting_hr       — resting heart rate (bpm)
#   spo2             — average SpO2 (%)
#   body_battery_high — peak body battery for the day
#   body_battery_low  — lowest body battery for the day
#   active_min       — moderate + vigorous intensity minutes
#
# Auth: tokens stored at TOKEN_DIR. On first run (no
# tokens), falls back to email/password from secrets.
# After successful password login, tokens are saved
# for all future runs.
#
# Schedule: daily at 10:00 AM, idle-gate disabled.
#
# Knows about: core/scheduler (schedule),
#              core/secrets (get_secret),
#              providers (get_db("root")).
# ==================================================

# ==================================================
# Imports
# ==================================================
from datetime import date, timedelta

import structlog

import providers
from core.scheduler import schedule
from core.secrets import get_secret

log = structlog.get_logger()

_GARMIN_USER_ID = "root"
_TOKEN_DIR      = "/var/lib/p-lanes/garmin/"


# ==================================================
# Auth helper
# ==================================================

def _get_client():
    """Return an authenticated Garmin client.

    tokenstore is passed to login() — the library loads saved
    tokens if present, or saves them after a fresh credential
    login. Falls back to email/password if no tokens exist yet.
    """
    import garminconnect

    try:
        client = garminconnect.Garmin()
        client.login(tokenstore=_TOKEN_DIR)
        log.debug("garmin_auth_token")
        return client
    except Exception:
        pass

    email    = get_secret("garmin_email")
    password = get_secret("garmin_password")

    if not email or not password:
        log.error("garmin_credentials_missing")
        return None

    try:
        client = garminconnect.Garmin(email, password)
        client.login(tokenstore=_TOKEN_DIR)
        log.info("garmin_auth_password_ok_tokens_saved")
        return client
    except Exception as e:
        log.error("garmin_auth_failed", error=str(e))
        return None


# ==================================================
# Individual stat pullers
# ==================================================

def _hrv(client, d: str) -> float | None:
    try:
        data = client.get_hrv_data(d)
        return data.get("hrvSummary", {}).get("lastNightAvg")
    except Exception:
        return None


def _sleep(client, d: str) -> tuple[int | None, int | None]:
    """Returns (sleep_score, sleep_min)."""
    try:
        data = client.get_sleep_data(d)
        dto  = data.get("dailySleepDTO", {})
        score_val = dto.get("sleepScores", {}).get("overall", {})
        score = score_val.get("value") if isinstance(score_val, dict) else None
        secs  = dto.get("sleepTimeSeconds")
        mins  = round(secs / 60) if secs else None
        return score, mins
    except Exception:
        return None, None


def _stats(client, d: str) -> dict:
    """Returns steps, stress, calories, resting_hr, active_min."""
    out = {}
    try:
        data = client.get_stats(d)
        out["steps"]      = data.get("totalSteps")
        out["stress"]     = data.get("averageStressLevel")
        out["calories"]   = data.get("totalKilocalories")
        out["resting_hr"] = data.get("restingHeartRate")
        mod  = data.get("moderateIntensityMinutes") or 0
        vig  = data.get("vigorousIntensityMinutes") or 0
        out["active_min"] = (mod + vig) or None
    except Exception:
        pass
    return out


def _spo2(client, d: str) -> float | None:
    try:
        data = client.get_spo2_data(d)
        return data.get("averageSpO2")
    except Exception:
        return None


def _body_battery(client, d: str) -> tuple[int | None, int | None]:
    """Returns (high, low) battery levels for the day."""
    try:
        readings = client.get_body_battery(d, d)
        if not readings:
            return None, None
        values = []
        for r in readings:
            arr = r.get("bodyBatteryValuesArray") or []
            # each entry is [timestamp, level]; level is index 1
            for entry in arr:
                if isinstance(entry, (list, tuple)) and len(entry) >= 2 and entry[1] is not None:
                    values.append(entry[1])
        if not values:
            return None, None
        return max(values), min(values)
    except Exception:
        return None, None


# ==================================================
# Scheduled job
# ==================================================

@schedule(cron="0 10 * * *", requires_idle=False)
async def sync():
    db = providers.get_db(_GARMIN_USER_ID)
    if db is None or not db.is_ready:
        log.warning("garmin_sync_skip_no_db")
        return

    client = _get_client()
    if client is None:
        return

    today     = date.today()
    yesterday = today - timedelta(days=1)

    # sync both days — yesterday gets complete/final data, today gets current snapshot
    for d in [yesterday.strftime("%Y-%m-%d"), today.strftime("%Y-%m-%d")]:
        log.info("garmin_sync_start", date=d)

        hrv                    = _hrv(client, d)
        sleep_score, sleep_min = _sleep(client, d)
        daily                  = _stats(client, d)
        spo2                   = _spo2(client, d)
        bb_high, bb_low        = _body_battery(client, d)

        await db.execute(
            """
            INSERT INTO garmin_metrics
                (date, hrv, sleep_score, sleep_min, steps, stress, calories,
                 resting_hr, spo2, body_battery_high, body_battery_low, active_min)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(date) DO UPDATE SET
                hrv               = excluded.hrv,
                sleep_score       = excluded.sleep_score,
                sleep_min         = excluded.sleep_min,
                steps             = excluded.steps,
                stress            = excluded.stress,
                calories          = excluded.calories,
                resting_hr        = excluded.resting_hr,
                spo2              = excluded.spo2,
                body_battery_high = excluded.body_battery_high,
                body_battery_low  = excluded.body_battery_low,
                active_min        = excluded.active_min
            """,
            (
                d, hrv, sleep_score, sleep_min,
                daily.get("steps"), daily.get("stress"), daily.get("calories"),
                daily.get("resting_hr"), spo2, bb_high, bb_low,
                daily.get("active_min"),
            ),
        )

        log.info("garmin_sync_done",
                 date=d, hrv=hrv, sleep_score=sleep_score,
                 sleep_min=sleep_min, steps=daily.get("steps"),
                 resting_hr=daily.get("resting_hr"), spo2=spo2,
                 body_battery_high=bb_high)
