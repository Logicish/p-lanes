# core/gemini.py
#
# Author:  Logicish
# Company: Logic-Ish Designs
# Date:    4/6/2026
#
# ==================================================
# Gemini Flash external inference backend.
#
# Role: silent background validator — user messages
# never reach Gemini. Only locally-generated response
# text is sent, after passing a local PII gate first.
#
# Activated when:
#   config.yaml:  gemini.enabled = true
#   secrets.yaml: gemini_api_key = "..."
#
# Rate limiting (proactive + reactive):
#   - Local RPM sliding window (deque of timestamps)
#   - Local RPD counter (resets at midnight UTC)
#   - Both caps configurable in config.yaml
#   - 429 RESOURCE_EXHAUSTED: RPM → wait + retry once
#                              RPD → disable for today
#
# Entry points:
#   is_configured()  — ready to use
#   is_available()   — configured AND within rate limits
#   call(messages)   — raw API call (OpenAI-format input)
#   verify(text)     — fact-check a response string
#
# Knows about: config (gemini section),
#              core/secrets (get_secret).
# ==================================================

# ==================================================
# Imports
# ==================================================
import asyncio
import time
from collections import deque
from datetime import datetime, timezone
from pathlib import Path

import structlog
import yaml

from core.secrets import get_secret

log = structlog.get_logger()

_CONFIG_PATH = Path(__file__).parent.parent / "config.yaml"

# ==================================================
# Prompts
# ==================================================

_VERIFY_SYSTEM = """\
You are a fact-checker. Examine the response below for factual errors or hallucinations.
Reply OK if the response appears accurate.
If there is a specific factual error, reply with one concise sentence describing the issue.
Do not add information. Do not explain correct facts. Only flag clear errors."""


# ==================================================
# Rate Limiter
# ==================================================

class _RateLimiter:
    """Proactive local rate limiter.
    RPM: sliding window over the last 60 seconds.
    RPD: daily counter, resets at midnight UTC.
    """

    def __init__(self, rpm_cap: int, rpd_cap: int):
        self.rpm_cap          = rpm_cap
        self.rpd_cap          = rpd_cap
        self._rpm_window: deque = deque()
        self._rpd_count:  int   = 0
        self._rpd_date:   str   = ""
        self._daily_exhausted:  bool = False  # set on 429 RPD hit

    def _refresh_rpd(self) -> None:
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        if today != self._rpd_date:
            self._rpd_date        = today
            self._rpd_count       = 0
            self._daily_exhausted = False

    def can_call(self) -> bool:
        self._refresh_rpd()
        if self._daily_exhausted:
            return False
        if self._rpd_count >= self.rpd_cap:
            return False
        now = time.monotonic()
        while self._rpm_window and now - self._rpm_window[0] > 60:
            self._rpm_window.popleft()
        return len(self._rpm_window) < self.rpm_cap

    def record(self) -> None:
        self._refresh_rpd()
        self._rpm_window.append(time.monotonic())
        self._rpd_count += 1

    def mark_daily_exhausted(self) -> None:
        self._daily_exhausted = True
        log.warning("gemini_daily_quota_exhausted",
                    rpd_count=self._rpd_count, rpd_cap=self.rpd_cap)

    @property
    def rpm_remaining(self) -> int:
        now = time.monotonic()
        active = sum(1 for t in self._rpm_window if now - t <= 60)
        return max(0, self.rpm_cap - active)

    @property
    def rpd_remaining(self) -> int:
        self._refresh_rpd()
        return max(0, self.rpd_cap - self._rpd_count)


# ==================================================
# Module State
# ==================================================

_enabled:       bool          = False
_model:         str           = "gemini-2.5-flash"
_api_key:       str | None    = None
_rate_limiter:  _RateLimiter  = _RateLimiter(rpm_cap=8, rpd_cap=200)
_client                       = None   # google.genai.Client, lazy-init


def _load() -> None:
    global _enabled, _model, _api_key, _rate_limiter
    try:
        with open(_CONFIG_PATH) as f:
            cfg = yaml.safe_load(f) or {}
        gem      = cfg.get("gemini", {})
        _enabled = gem.get("enabled", False)
        _model   = gem.get("model", "gemini-2.5-flash")
        _api_key = get_secret("gemini_api_key") if _enabled else None

        rl = gem.get("rate_limits", {})
        _rate_limiter = _RateLimiter(
            rpm_cap=rl.get("rpm_cap", 8),
            rpd_cap=rl.get("rpd_cap", 200),
        )

        if _enabled and not _api_key:
            log.warning("gemini_enabled_but_no_api_key",
                        hint="add gemini_api_key to secrets.yaml")
        elif _enabled:
            log.info("gemini_configured", model=_model,
                     rpm_cap=_rate_limiter.rpm_cap,
                     rpd_cap=_rate_limiter.rpd_cap)
    except Exception as e:
        log.warning("gemini_config_load_failed", error=str(e))
        _enabled = False


_load()


def _get_client():
    global _client
    if _client is None:
        from google import genai as _genai
        _client = _genai.Client(api_key=_api_key)
    return _client


# ==================================================
# Public API
# ==================================================

def is_configured() -> bool:
    """True when enabled and API key is present."""
    return _enabled and bool(_api_key)


def is_available() -> bool:
    """True when configured and within rate limits."""
    return is_configured() and _rate_limiter.can_call()


async def call(
    messages:    list[dict],
    temperature: float = 0.3,
    max_tokens:  int   = 512,
) -> "LLMResponse":  # noqa: F821 — imported at call time
    """Call Gemini with OpenAI-format messages.
    Raises LLMCallError on failure so callers can fall back to local.
    """
    from core.llm import LLMCallError, LLMResponse

    if not is_configured():
        raise LLMCallError("Gemini not configured")

    if not _rate_limiter.can_call():
        raise LLMCallError(
            f"Gemini rate limit reached — "
            f"RPM remaining: {_rate_limiter.rpm_remaining}, "
            f"RPD remaining: {_rate_limiter.rpd_remaining}"
        )

    # extract system instruction and build prompt text
    system = next(
        (m["content"] for m in messages if m["role"] == "system"), ""
    )
    user_parts = [m["content"] for m in messages if m["role"] != "system"]
    prompt = "\n\n".join(user_parts)

    t0 = time.perf_counter()
    try:
        from google import genai as _genai
        client = _get_client()

        cfg_kwargs = dict(
            temperature=temperature,
            max_output_tokens=max_tokens,
        )
        if system:
            cfg_kwargs["system_instruction"] = system

        response = await client.aio.models.generate_content(
            model=_model,
            contents=prompt,
            config=_genai.types.GenerateContentConfig(**cfg_kwargs),
        )

        _rate_limiter.record()
        elapsed = time.perf_counter() - t0
        text = response.text.strip() if response.text else ""

        log.info("gemini_response",
                 model=_model,
                 elapsed=f"{elapsed:.2f}s",
                 chars=len(text),
                 rpd_remaining=_rate_limiter.rpd_remaining)

        return LLMResponse(
            content=text,
            elapsed=elapsed,
            total_tokens=0,   # Gemini free tier doesn't expose token counts
            truncated=False,
        )

    except Exception as e:
        err_str = str(e)

        # 429 — parse whether it's RPM or RPD exhaustion
        if "429" in err_str or "RESOURCE_EXHAUSTED" in err_str:
            # try to extract retryDelay from error details
            retry_delay = _parse_retry_delay(err_str)

            if retry_delay and retry_delay < 30:
                # short delay → RPM limit, retry once after waiting
                log.warning("gemini_rpm_limit_hit",
                            retry_delay=retry_delay)
                await asyncio.sleep(retry_delay + 1)
                try:
                    return await call(messages, temperature, max_tokens)
                except Exception as retry_err:
                    raise LLMCallError(f"Gemini retry failed: {retry_err}") from retry_err
            else:
                # long delay or no delay info → assume daily quota
                _rate_limiter.mark_daily_exhausted()
                raise LLMCallError("Gemini daily quota exhausted") from e

        log.warning("gemini_call_failed", error=err_str)
        raise LLMCallError(f"Gemini call failed: {e}") from e


async def verify(response_text: str) -> str | None:
    """Fact-check a locally-generated response string.
    Returns None if unavailable, 'OK' if accurate,
    or a brief description of the issue if not.
    """
    if not is_available():
        return None

    messages = [
        {"role": "system", "content": _VERIFY_SYSTEM},
        {"role": "user",   "content": response_text},
    ]
    try:
        result = await call(messages, temperature=0.1, max_tokens=128)
        return result.content.strip() or None
    except Exception as e:
        log.debug("gemini_verify_skipped", reason=str(e))
        return None


# ==================================================
# Helpers
# ==================================================

def _parse_retry_delay(err_str: str) -> float | None:
    """Extract retryDelay seconds from a 429 error string if present."""
    import re
    match = re.search(r'retryDelay["\s:]+([0-9.]+)s', err_str)
    if match:
        return float(match.group(1))
    return None
