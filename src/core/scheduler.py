# core/scheduler.py
#
# Author:  Logicish
# Company: Logic-Ish Designs
# Date:    4/7/2026
#
# ==================================================
# Generalized job scheduler for background tasks.
#
# Jobs self-register via @schedule decorator — core
# never imports job files directly. Auto-discovery
# scans the modules/ directory at startup, same as
# core/events.py.
#
# Job options:
#   cron          — "M H * * *" format (minute hour)
#   requires_idle — skip if any non-utility user active
#   max_duration  — cancel job after N seconds (0 = unlimited)
#
# Each registered job runs in its own asyncio.Task,
# controlled by a per-job scheduler loop. Jobs that
# raise are logged and retried on the next cron tick.
#
# Public helpers (importable by any module):
#   is_system_idle() — True when all real users are idle
#   schedule(...)    — decorator to register a job
#
# Knows about: core/slots (get_all_users, is_idle).
# ==================================================

# ==================================================
# Imports
# ==================================================
import asyncio
import importlib
import pkgutil
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path

import structlog

log = structlog.get_logger()

# ==================================================
# Registry
# ==================================================

@dataclass
class _Job:
    name:          str
    cron:          str
    requires_idle: bool
    max_duration:  int        # seconds; 0 = unlimited
    handler:       callable
    task:          asyncio.Task | None = field(default=None, repr=False)


_registry: dict[str, _Job] = {}


def schedule(
    cron:          str  = "0 0 * * *",
    requires_idle: bool = True,
    max_duration:  int  = 3600,
):
    """Decorator — register an async function as a scheduled job.

    Usage::

        from core.scheduler import schedule

        @schedule(cron="0 23 * * *", requires_idle=True, max_duration=3600)
        async def my_job():
            ...
    """
    def decorator(func):
        name = func.__name__
        if name in _registry:
            log.warning("scheduler_job_overwrite", job=name)
        _registry[name] = _Job(
            name=name,
            cron=cron,
            requires_idle=requires_idle,
            max_duration=max_duration,
            handler=func,
        )
        log.info("scheduler_job_registered", job=name, cron=cron,
                 requires_idle=requires_idle, max_duration=max_duration)
        return func
    return decorator


# ==================================================
# Public helpers
# ==================================================

def is_system_idle() -> bool:
    """True when every non-utility real user is idle (or has never been active)."""
    from core.slots import get_all_users
    users = get_all_users()
    for uid, user in users.items():
        if uid in ("utility", "guest"):
            continue
        if not user.is_idle():
            return False
    return True


# ==================================================
# Lifecycle
# ==================================================

_tasks: list[asyncio.Task] = []


async def start():
    """Start a scheduler loop for each registered job. Called from main.py lifespan."""
    if not _registry:
        log.info("scheduler_no_jobs")
        return
    for job in _registry.values():
        task = asyncio.create_task(_job_loop(job), name=f"scheduler:{job.name}")
        job.task = task
        _tasks.append(task)
    log.info("scheduler_started", jobs=list(_registry.keys()))


async def stop():
    """Cancel all running scheduler loops. Called from main.py lifespan."""
    for task in _tasks:
        if not task.done():
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
    _tasks.clear()
    log.info("scheduler_stopped")


# ==================================================
# Auto-discovery
# ==================================================

def discover_jobs(package_name: str = "modules"):
    """Import every module in package_name so @schedule decorators fire.
    Safe to call after events.discover_modules() — imports are cached.
    """
    try:
        package = importlib.import_module(package_name)
    except ModuleNotFoundError:
        log.warning("scheduler_package_not_found", package=package_name)
        return

    package_path = Path(package.__file__).parent
    for _, modname, _ in pkgutil.iter_modules([str(package_path)]):
        full_name = f"{package_name}.{modname}"
        try:
            importlib.import_module(full_name)
        except Exception as e:
            log.error("scheduler_discovery_failed", module=full_name, error=str(e))


# ==================================================
# Scheduler loop
# ==================================================

def _next_run(cron: str) -> datetime:
    """Return the next datetime a cron expression should fire.
    Supports 'M H * * *' format (minute and hour only).
    """
    parts = cron.split()
    target_minute = int(parts[0]) if parts[0] != "*" else 0
    target_hour   = int(parts[1]) if parts[1] != "*" else 0

    now    = datetime.now()
    target = now.replace(hour=target_hour, minute=target_minute,
                         second=0, microsecond=0)
    if target <= now:
        target += timedelta(days=1)
    return target


async def _job_loop(job: _Job):
    while True:
        try:
            target       = _next_run(job.cron)
            wait_seconds = (target - datetime.now()).total_seconds()
            log.info("scheduler_job_next_run",
                     job=job.name,
                     target=target.isoformat(),
                     wait_seconds=int(wait_seconds))
            await asyncio.sleep(max(wait_seconds, 0))

            # idle gate
            if job.requires_idle and not is_system_idle():
                log.info("scheduler_job_skipped_not_idle", job=job.name)
                continue

            log.info("scheduler_job_starting", job=job.name)
            await _run_with_timeout(job)
            log.info("scheduler_job_complete", job=job.name)

        except asyncio.CancelledError:
            raise
        except Exception as e:
            log.error("scheduler_job_loop_error", job=job.name, error=str(e))
            await asyncio.sleep(60)   # back off before retry


async def _run_with_timeout(job: _Job):
    if job.max_duration > 0:
        try:
            await asyncio.wait_for(job.handler(), timeout=job.max_duration)
        except asyncio.TimeoutError:
            log.warning("scheduler_job_timeout",
                        job=job.name, max_duration=job.max_duration)
    else:
        await job.handler()
