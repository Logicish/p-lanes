# modules/crawler.py
#
# Author:  Logicish
# Company: Logic-Ish Designs
# Date:    4/7/2026
#
# ==================================================
# Scheduled web crawler.
# Reads a URL list (urls.yaml), fetches each page
# via Jina Reader, writes raw output to the pre_process
# directory for a separate formatter to clean and
# route into RAG.
#
# Schedule: nightly at 2300 (configurable in config.yaml).
# Idle gate: skipped if any real user is active.
# Rate limit: configurable max fetches per minute.
# Duration cap: stops after max_duration seconds.
#
# URL list:   /var/lib/p-lanes/crawler/urls.yaml
# Output:     /var/lib/p-lanes/crawler/pre_process/
#
# Each output file is named {slug}.md where slug is
# derived from the URL label. Files are overwritten
# on each crawl so the formatter always sees fresh content.
#
# Knows about: core/scheduler (schedule).
# ==================================================

# ==================================================
# Imports
# ==================================================
import asyncio
import re
import time
from pathlib import Path

import httpx
import structlog
import yaml

from core.scheduler import schedule

log = structlog.get_logger()

_CONFIG_PATH = Path(__file__).parent.parent / "config.yaml"


# ==================================================
# Config
# ==================================================

def _load_config() -> dict:
    try:
        with open(_CONFIG_PATH) as f:
            cfg = yaml.safe_load(f) or {}
        return cfg.get("crawler", {})
    except Exception:
        return {}

_CFG = _load_config()

_ENABLED       = _CFG.get("enabled",      True)
_CRON          = _CFG.get("cron",         "0 23 * * *")
_MAX_DURATION  = _CFG.get("max_duration", 3600)
_REQUIRES_IDLE = _CFG.get("requires_idle", True)
_RATE_LIMIT    = _CFG.get("rate_limit",   10)    # fetches per minute
_URL_LIST      = Path(_CFG.get("url_list",   "/var/lib/p-lanes/crawler/urls.yaml"))
_OUTPUT_DIR    = Path(_CFG.get("output_dir", "/var/lib/p-lanes/rag/shared/web_cache"))
_JINA_URL      = _CFG.get("jina_url",    "https://r.jina.ai")
_JINA_TIMEOUT  = _CFG.get("jina_timeout", 15)
_JINA_MAX_CHARS = _CFG.get("jina_max_chars", 8000)

# Minimum seconds between fetches to honour rate_limit
_FETCH_INTERVAL = 60.0 / max(_RATE_LIMIT, 1)


# ==================================================
# Registration
# ==================================================

if _ENABLED:
    @schedule(cron=_CRON, requires_idle=_REQUIRES_IDLE, max_duration=_MAX_DURATION)
    async def run_crawler():
        await _crawl()
else:
    log.info("crawler_disabled")


# ==================================================
# Crawl logic
# ==================================================

async def _crawl():
    urls = _load_url_list()
    if not urls:
        log.info("crawler_no_urls")
        return

    _OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

    ingested = 0
    failed   = 0

    for entry in urls:
        if not entry.get("enabled", True):
            continue

        url        = entry.get("url", "").strip()
        label      = entry.get("label", url).strip()
        collection = entry.get("collection", "shared_general").strip()

        if not url:
            continue

        log.info("crawler_fetching", url=url[:80], label=label)
        t0 = time.monotonic()

        content = await _fetch_jina(url)
        if not content:
            log.warning("crawler_fetch_failed", url=url[:80])
            failed += 1
        else:
            _write_output(label, url, collection, content)
            ingested += 1
            log.info("crawler_page_written",
                     label=label, chars=len(content), collection=collection)

        # rate limiting — wait out the remainder of the fetch interval
        elapsed = time.monotonic() - t0
        wait    = max(0.0, _FETCH_INTERVAL - elapsed)
        if wait > 0:
            await asyncio.sleep(wait)

    log.info("crawler_complete", ingested=ingested, failed=failed,
             output_dir=str(_OUTPUT_DIR))


# ==================================================
# URL list loader
# ==================================================

def _load_url_list() -> list[dict]:
    if not _URL_LIST.exists():
        log.warning("crawler_url_list_missing", path=str(_URL_LIST))
        return []
    try:
        with open(_URL_LIST) as f:
            data = yaml.safe_load(f) or {}
        return data.get("urls", [])
    except Exception as e:
        log.error("crawler_url_list_load_failed", error=str(e))
        return []


# ==================================================
# Jina fetch
# ==================================================

async def _fetch_jina(url: str) -> str:
    """Fetch page content via Jina Reader. Returns cleaned text or empty string."""
    try:
        headers = {
            "Accept":           "text/plain",
            "X-Return-Format":  "text",
        }
        # use Jina API key if configured
        try:
            from core.secrets import get_secret
            key = get_secret("jina_api_key")
            if key:
                headers["Authorization"] = f"Bearer {key}"
        except Exception:
            pass

        jina_url = f"{_JINA_URL}/{url}"
        async with httpx.AsyncClient(timeout=_JINA_TIMEOUT) as client:
            resp = await client.get(jina_url, headers=headers, follow_redirects=True)
            resp.raise_for_status()
        return resp.text.strip()[:_JINA_MAX_CHARS]
    except Exception as e:
        log.debug("crawler_jina_error", url=url[:80], error=str(e))
        return ""


# ==================================================
# Output writer
# ==================================================

def _label_to_slug(label: str) -> str:
    """Convert a label to a safe filename slug."""
    slug = label.lower().strip()
    slug = re.sub(r"[^\w\s-]", "", slug)
    slug = re.sub(r"[\s_-]+", "_", slug)
    return slug[:80]


def _write_output(label: str, url: str, collection: str, content: str) -> None:
    slug     = _label_to_slug(label)
    out_path = _OUTPUT_DIR / f"{slug}.md"
    lines = [
        f"# {label}",
        "",
        f"> Source: {url}",
        f"> Collection: {collection}",
        "",
        content,
        "",
    ]
    out_path.write_text("\n".join(lines), encoding="utf-8")
    log.debug("crawler_file_written", path=str(out_path))


