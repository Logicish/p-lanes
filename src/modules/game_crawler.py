# modules/game_crawler.py
#
# Author:  Logicish
# Company: Logic-Ish Designs
# Date:    4/19/2026
#
# ==================================================
# Nightly game wiki crawler.
# Reads pending entries from game_crawl_queue (house.db),
# BFS-crawls each game's wiki, and writes raw markdown
# pages to /var/lib/p-lanes/crawler/games/{game_slug}/
# for game_intake.py to process into RAG + DB.
#
# Crawl behaviour:
#   - Seed URL auto-resolved: {slug}.fandom.com/wiki/Main_Page
#     with SearXNG fallback if that 404s.
#   - Domain-scoped BFS, depth 3, max 300 pages per game.
#   - Jina Reader (markdown format) for content + link discovery.
#   - 2 req/min ±5s jitter — respectful of community wikis.
#   - Robots.txt honoured; Fandom namespace URLs skipped.
#   - Delta mode: skips pages whose content hash is unchanged,
#     but still follows their links to discover new pages.
#   - force_delta=1 wipes visited state and does a full re-crawl
#     (use after DLC drops); reset to 0 on completion.
#
# Schedule: nightly at 2300, idle-gated, 2-hour cap.
#
# Knows about: core/scheduler (schedule),
#              core/secrets (get_secret),
#              providers (get_db("house")).
# ==================================================

# ==================================================
# Imports
# ==================================================
import asyncio
import hashlib
import random
import shutil
import re
from collections import deque
from pathlib import Path
from urllib.parse import urlparse, urlunparse
from urllib.robotparser import RobotFileParser

import httpx
import structlog
import yaml

import providers
from core.scheduler import schedule
from core.secrets import get_secret

log = structlog.get_logger()

_CONFIG_PATH = Path(__file__).parent.parent / "config.yaml"


# ==================================================
# Config
# ==================================================

def _load_config() -> dict:
    try:
        with open(_CONFIG_PATH) as f:
            cfg = yaml.safe_load(f) or {}
        return cfg.get("game_crawler", {})
    except Exception:
        return {}

_CFG = _load_config()

_ENABLED        = _CFG.get("enabled",        True)
_CRON           = _CFG.get("cron",           "0 23 * * *")
_MAX_DURATION   = _CFG.get("max_duration",   7200)
_REQUIRES_IDLE  = _CFG.get("requires_idle",  True)
_RATE_LIMIT     = _CFG.get("rate_limit",     2)
_JITTER_SEC     = _CFG.get("jitter_sec",     5)
_MAX_DEPTH      = _CFG.get("max_depth",      3)
_MAX_PAGES      = _CFG.get("max_pages",      300)
_JINA_URL       = _CFG.get("jina_url",       "https://r.jina.ai")
_JINA_TIMEOUT   = _CFG.get("jina_timeout",   20)
_JINA_MAX_CHARS = _CFG.get("jina_max_chars", 12000)
_OUTPUT_DIR     = Path(_CFG.get("output_dir", "/var/lib/p-lanes/crawler/games"))
_SEARXNG_URL    = _CFG.get("searxng_url",    "http://127.0.0.1:8888/search")

_DISK_FREE_MIN_GB     = _CFG.get("disk_free_min_gb",    8)
_FOLDER_MAX_GB        = _CFG.get("folder_max_gb",       3)
_DISK_CHECK_INTERVAL  = _CFG.get("disk_check_interval", 50)

_FETCH_INTERVAL = 60.0 / max(_RATE_LIMIT, 1)

# Fandom namespaces and URL query params that indicate non-article pages
_SKIP_PATTERNS = [
    "/wiki/Special:", "/wiki/File:", "/wiki/Category:", "/wiki/Talk:",
    "/wiki/User:", "/wiki/Template:", "/wiki/MediaWiki:", "/wiki/Help:",
    "?action=", "?diff=", "?oldid=", "?veaction=", "?curid=",
]

# Special: pages allowed through for link discovery (fetched but not written to disk)
_SKIP_ALLOWLIST = ["/wiki/Special:AllPages"]

# Pages fetched for link discovery only — content not written to output dir
_DISCOVERY_ONLY = ["/wiki/Special:AllPages"]

_GB = 1024 ** 3


# ==================================================
# Disk / folder helpers
# ==================================================

def _disk_free_gb() -> float:
    return shutil.disk_usage(_OUTPUT_DIR).free / _GB


def _folder_size_gb() -> float:
    total = sum(f.stat().st_size for f in _OUTPUT_DIR.rglob("*") if f.is_file())
    return total / _GB


async def _set_flag(db, key: str, value: str) -> None:
    await db.execute(
        """
        INSERT INTO crawler_status (key, value, updated_at)
        VALUES (?, ?, datetime('now'))
        ON CONFLICT(key) DO UPDATE SET value = excluded.value, updated_at = excluded.updated_at
        """,
        (key, value),
    )


async def _clear_flag(db, key: str) -> None:
    await db.execute("DELETE FROM crawler_status WHERE key = ?", (key,))


# ==================================================
# Registration
# ==================================================

if _ENABLED:
    @schedule(cron=_CRON, requires_idle=_REQUIRES_IDLE, max_duration=_MAX_DURATION)
    async def run_game_crawler():
        await _crawl_all()
else:
    log.info("game_crawler_disabled")


# ==================================================
# Robots.txt cache
# ==================================================

_robots_cache: dict[str, RobotFileParser] = {}


async def _get_robots(domain: str) -> RobotFileParser:
    if domain in _robots_cache:
        return _robots_cache[domain]
    rf = RobotFileParser()
    try:
        robots_url = f"https://{domain}/robots.txt"
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.get(robots_url, follow_redirects=True)
            rf.parse(resp.text.splitlines())
    except Exception:
        pass  # permissive if robots.txt unavailable
    _robots_cache[domain] = rf
    return rf


def _robots_ok(rf: RobotFileParser, url: str) -> bool:
    try:
        return rf.can_fetch("*", url)
    except Exception:
        return True


# ==================================================
# URL helpers
# ==================================================

def _normalize_url(url: str) -> str:
    parsed = urlparse(url)
    return urlunparse(parsed._replace(fragment=""))


def _same_domain(url: str, seed: str) -> bool:
    return urlparse(url).netloc == urlparse(seed).netloc


def _should_skip(url: str) -> bool:
    for allowed in _SKIP_ALLOWLIST:
        if allowed in url:
            return False
    for pat in _SKIP_PATTERNS:
        if pat in url:
            return True
    return False


def _is_discovery_only(url: str) -> bool:
    return any(pat in url for pat in _DISCOVERY_ONLY)


def _url_to_slug(url: str) -> str:
    parsed = urlparse(url)
    path = parsed.path.strip("/").replace("/", "_")
    path = re.sub(r"[^\w_-]", "_", path)
    return path[:120] or "index"


def _game_name_to_slug(name: str) -> str:
    slug = name.lower().strip()
    slug = re.sub(r"[^\w\s-]", "", slug)
    slug = re.sub(r"[\s_-]+", "_", slug)
    return slug


# ==================================================
# Link + title extraction
# ==================================================

_LINK_RE = re.compile(r'\[([^\]]*)\]\((https?://[^\s\)\]]+)\)')


def _extract_links(content: str) -> list[str]:
    return [_normalize_url(m.group(2)) for m in _LINK_RE.finditer(content)]


def _extract_title(content: str) -> str | None:
    for line in content.splitlines():
        if line.startswith("# "):
            return line[2:].strip()
    return None


# ==================================================
# Content hash
# ==================================================

def _content_hash(content: str) -> str:
    return hashlib.sha256(content.encode()).hexdigest()[:32]


# ==================================================
# Seed URL resolution
# ==================================================

def _fandom_slug(game_name: str) -> str:
    return re.sub(r"[^a-z0-9]", "", game_name.lower())


async def _resolve_seed(game_name: str) -> str | None:
    slug = _fandom_slug(game_name)

    # Prefer Special:AllPages — flat alphabetical article index, yields the
    # densest possible link frontier and avoids the nav-heavy Main_Page trap.
    for candidate in [
        f"https://{slug}.fandom.com/wiki/Special:AllPages",
        f"https://{slug}.fandom.com/wiki/Main_Page",
    ]:
        try:
            async with httpx.AsyncClient(timeout=10, follow_redirects=True) as client:
                resp = await client.head(candidate)
                if resp.status_code < 400:
                    log.info("game_crawler_seed_fandom", game=game_name, url=candidate)
                    return candidate
        except Exception:
            pass

    log.info("game_crawler_seed_fandom_miss", game=game_name, slug=slug)
    return await _searxng_seed(game_name)


async def _searxng_seed(game_name: str) -> str | None:
    try:
        async with httpx.AsyncClient(timeout=8) as client:
            resp = await client.get(
                _SEARXNG_URL,
                params={"q": f'"{game_name}" wiki site:fandom.com', "format": "json"},
            )
            data    = resp.json()
            results = data.get("results", [])
            for r in results:
                url    = r.get("url", "")
                parsed = urlparse(url)
                if "fandom.com" in parsed.netloc:
                    seed = f"{parsed.scheme}://{parsed.netloc}/wiki/Main_Page"
                    log.info("game_crawler_seed_searxng", game=game_name, seed=seed)
                    return seed
    except Exception as e:
        log.error("game_crawler_seed_searxng_failed", game=game_name, error=str(e))
    return None


# ==================================================
# Jina fetch (markdown format for link preservation)
# ==================================================

async def _fetch_jina(url: str) -> str:
    try:
        headers: dict[str, str] = {
            "Accept":          "text/markdown",
            "X-Return-Format": "markdown",
        }
        try:
            key = get_secret("jina_api_key")
            if key:
                headers["Authorization"] = f"Bearer {key}"
        except Exception:
            pass

        jina_target = f"{_JINA_URL}/{url}"
        async with httpx.AsyncClient(timeout=_JINA_TIMEOUT) as client:
            resp = await client.get(jina_target, headers=headers, follow_redirects=True)
            resp.raise_for_status()
        return resp.text.strip()[:_JINA_MAX_CHARS]
    except Exception as e:
        log.debug("game_crawler_jina_error", url=url[:80], error=str(e))
        return ""


# ==================================================
# Output writer / cache reader
# ==================================================

def _read_cached(game_slug: str, url: str) -> str:
    path = _OUTPUT_DIR / game_slug / f"{_url_to_slug(url)}.md"
    try:
        return path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return ""


def _write_page(game_slug: str, url: str, content: str) -> None:
    game_dir = _OUTPUT_DIR / game_slug
    game_dir.mkdir(parents=True, exist_ok=True)
    slug = _url_to_slug(url)
    path = game_dir / f"{slug}.md"
    path.write_text(
        f"> Source: {url}\n> Game: {game_slug}\n\n{content}\n",
        encoding="utf-8",
    )


# ==================================================
# Per-game BFS crawl
# ==================================================

async def _crawl_game(db, game: dict) -> int:
    game_name   = game["game_name"]
    game_slug   = _game_name_to_slug(game_name)
    force_delta = bool(game["force_delta"])
    seed_url    = (game.get("seed_url") or "").strip()

    log.info("game_crawler_game_start", game=game_name, force_delta=force_delta)

    if not seed_url:
        seed_url = await _resolve_seed(game_name)
        if not seed_url:
            log.error("game_crawler_no_seed", game=game_name)
            return 0
        await db.execute(
            "UPDATE game_crawl_queue SET seed_url = ? WHERE game_name = ?",
            (seed_url, game_name),
        )

    if force_delta:
        await db.execute(
            "DELETE FROM game_crawl_visited WHERE game_name = ?",
            (game_name,),
        )
        known_visited: dict[str, str] = {}
    else:
        rows = await db.fetchall(
            "SELECT url, content_hash FROM game_crawl_visited WHERE game_name = ?",
            (game_name,),
        )
        known_visited = {r["url"]: r["content_hash"] for r in rows}

    robots           = await _get_robots(urlparse(seed_url).netloc)
    frontier: deque[tuple[str, int]] = deque([(seed_url, 0)])
    visited_this_run: set[str]       = set()
    pages_fetched                    = 0

    while frontier and pages_fetched < _MAX_PAGES:
        url, depth = frontier.popleft()
        url        = _normalize_url(url)

        if url in visited_this_run:
            continue
        if not _same_domain(url, seed_url):
            continue
        if _should_skip(url):
            continue
        if not _robots_ok(robots, url):
            continue

        visited_this_run.add(url)

        # Fast path: page already visited and not discovery-only — read from disk,
        # extract links, skip HTTP fetch and rate-limit sleep entirely.
        if not force_delta and url in known_visited and not _is_discovery_only(url):
            cached = _read_cached(game_slug, url)
            if cached:
                if depth < _MAX_DEPTH:
                    for link in _extract_links(cached):
                        norm = _normalize_url(link)
                        if norm not in visited_this_run and _same_domain(norm, seed_url):
                            frontier.append((norm, depth + 1))
                continue
            # file missing on disk — fall through to re-fetch

        content = await _fetch_jina(url)

        # rate limit with jitter regardless of outcome
        wait = max(_FETCH_INTERVAL + random.uniform(-_JITTER_SEC, _JITTER_SEC), 1.0)
        await asyncio.sleep(wait)

        if not content:
            log.warning("game_crawler_fetch_failed", url=url[:80])
            continue

        new_hash      = _content_hash(content)
        content_changed = known_visited.get(url) != new_hash

        if content_changed:
            title = _extract_title(content)
            if _is_discovery_only(url):
                log.debug("game_crawler_discovery_only", url=url[:80])
            else:
                _write_page(game_slug, url, content)
                await db.execute(
                    """
                    INSERT INTO game_crawl_visited (game_name, url, content_hash, page_title)
                    VALUES (?, ?, ?, ?)
                    ON CONFLICT(game_name, url) DO UPDATE SET
                        content_hash = excluded.content_hash,
                        page_title   = excluded.page_title,
                        fetched_at   = datetime('now')
                    """,
                    (game_name, url, new_hash, title),
                )
                pages_fetched += 1
                log.info("game_crawler_page_written",
                         game=game_name, url=url[:80], depth=depth, title=title)

                # mid-crawl disk check every N pages
                if pages_fetched % _DISK_CHECK_INTERVAL == 0:
                    free_gb = _disk_free_gb()
                    if free_gb < _DISK_FREE_MIN_GB:
                        await _set_flag(db, "disk_full",
                                        f"{free_gb:.2f}GB free < {_DISK_FREE_MIN_GB}GB minimum")
                        log.warning("game_crawler_midcrawl_disk_full",
                                    game=game_name, pages_fetched=pages_fetched,
                                    free_gb=round(free_gb, 2))
                        return pages_fetched
        else:
            log.debug("game_crawler_page_unchanged", url=url[:80])

        # always follow links to catch new pages even when content is unchanged
        if depth < _MAX_DEPTH:
            for link in _extract_links(content):
                norm = _normalize_url(link)
                if norm not in visited_this_run and _same_domain(norm, seed_url):
                    frontier.append((norm, depth + 1))

    log.info("game_crawler_game_done",
             game=game_name, pages_fetched=pages_fetched,
             frontier_remaining=len(frontier))
    return pages_fetched


# ==================================================
# Main crawl loop
# ==================================================

async def _crawl_all():
    db = providers.get_db("games")
    if db is None or not db.is_ready:
        log.warning("game_crawler_skip_no_db")
        return

    # Pre-flight: free disk space
    free_gb = _disk_free_gb()
    if free_gb < _DISK_FREE_MIN_GB:
        await _set_flag(db, "disk_full", f"{free_gb:.2f}GB free < {_DISK_FREE_MIN_GB}GB minimum")
        log.warning("game_crawler_suspended_disk_full", free_gb=round(free_gb, 2),
                    min_gb=_DISK_FREE_MIN_GB)
        return
    await _clear_flag(db, "disk_full")

    # Pre-flight: crawler output folder size
    folder_gb = _folder_size_gb()
    if folder_gb > _FOLDER_MAX_GB:
        await _set_flag(db, "folder_full", f"{folder_gb:.2f}GB used > {_FOLDER_MAX_GB}GB limit")
        log.warning("game_crawler_suspended_folder_full", folder_gb=round(folder_gb, 2),
                    max_gb=_FOLDER_MAX_GB)
        return
    await _clear_flag(db, "folder_full")

    pending = await db.fetchall(
        "SELECT * FROM game_crawl_queue WHERE status = 'pending' ORDER BY requested_at ASC"
    )

    if not pending:
        log.info("game_crawler_no_pending")
        return

    log.info("game_crawler_start", count=len(pending))

    for game in pending:
        game_name = game["game_name"]

        await db.execute(
            """
            UPDATE game_crawl_queue
            SET status = 'running', started_at = datetime('now')
            WHERE game_name = ?
            """,
            (game_name,),
        )

        pages = 0
        try:
            pages = await _crawl_game(db, game)
            await db.execute(
                """
                UPDATE game_crawl_queue
                SET status = 'done', completed_at = datetime('now'),
                    pages_fetched = ?, force_delta = 0, error = NULL
                WHERE game_name = ?
                """,
                (pages, game_name),
            )
            log.info("game_crawler_game_complete", game=game_name, pages=pages)
        except asyncio.CancelledError:
            # Service shutdown or max_duration timeout — reset to pending so next
            # nightly run resumes. visited table is intact so delta mode skips
            # unchanged pages and picks up from where we left off.
            await db.execute(
                """
                UPDATE game_crawl_queue
                SET status = 'pending', pages_fetched = ?, error = 'interrupted'
                WHERE game_name = ?
                """,
                (pages, game_name),
            )
            log.info("game_crawler_interrupted", game=game_name, pages_partial=pages)
            raise
        except Exception as e:
            log.error("game_crawler_game_failed", game=game_name, error=str(e))
            await db.execute(
                """
                UPDATE game_crawl_queue
                SET status = 'failed', error = ?
                WHERE game_name = ?
                """,
                (str(e), game_name),
            )
