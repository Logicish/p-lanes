# modules/system_status.py
#
# Author:  Logicish
# Company: Logic-Ish Designs
# Date:    4/19/2026
#
# ==================================================
# Handles the 'system_status' intent.
# Queries the last 24 hours of system_health and
# gpu_health, checks each metric against thresholds,
# and assigns a grade: GREEN / YELLOW / ORANGE / RED.
#
# Injects a structured status report into ctx.enrichments
# so the LLM can narrate it in the user's persona voice.
# Does NOT set skip_processor.
#
# Grades:
#   RED    — container unexpectedly down, OR critical
#            threshold breached (mem > 97%, gpu_temp > 90°C)
#   ORANGE — 3+ rows above any warning threshold (sustained)
#   YELLOW — 1-2 rows above any warning threshold (spike)
#   GREEN  — all metrics within normal range
#
# Warning thresholds:
#   cpu_pct    > 85%   (per container)
#   mem_pct    > 90%   (per container)
#   gpu_temp_c > 80°C
#   gpu_util   > 95%
#   vram used  > 95% of total
#
# artist is excluded from container-down alerts — it is
# an on-demand container that is expected to be stopped.
#
# Security: USER (1) — jj and above.
#
# Knows about: core/events (register),
#              core/pipeline (PipelineContext),
#              providers (get_db).
# ==================================================

# ==================================================
# Imports
# ==================================================
import structlog

import providers
from core.pipeline import PipelineContext
from core.tool_registry import tool

log = structlog.get_logger()

# warning thresholds
_CPU_WARN      = 85.0
_MEM_WARN      = 90.0
_GPU_TEMP_WARN = 80
_GPU_UTIL_WARN = 95
_VRAM_WARN_PCT = 95.0

# critical thresholds (trigger RED)
_MEM_CRIT      = 97.0
_GPU_TEMP_CRIT = 90

_NORMAL_STATUSES = {"running", "online"}
_EXPECTED_DOWN   = {"artist"}   # on-demand, not a problem if stopped

_HOSTS = ["brain", "ears", "voice", "omen", "artist"]

_SPIKE_MAX     = 2   # 1-2 occurrences → YELLOW
_SUSTAINED_MIN = 3   # 3+ occurrences  → ORANGE


def _grade(violations: list[tuple[str, int]], container_down: list[str], critical: bool) -> str:
    if critical or container_down:
        return "RED"
    if not violations:
        return "GREEN"
    max_count = max(count for _, count in violations)
    return "ORANGE" if max_count >= _SUSTAINED_MIN else "YELLOW"


async def _analyze(db) -> dict:
    """Query last 24h and return analysis dict."""
    health_rows = await db.fetchall(
        "SELECT * FROM system_health WHERE ts >= datetime('now', '-24 hours') ORDER BY ts",
        (),
    )
    gpu_rows = await db.fetchall(
        "SELECT * FROM gpu_health WHERE ts >= datetime('now', '-24 hours') ORDER BY ts",
        (),
    )

    violations: list[tuple[str, int]] = []   # (label, count)
    critical     = False
    container_down: list[str] = []
    host_summaries: list[str] = []

    # --- per-host metric analysis ---
    for host in _HOSTS:
        if not health_rows:
            break

        cpu_vals  = [r[f"{host}_cpu_pct"]  for r in health_rows if r[f"{host}_cpu_pct"]  is not None]
        mem_vals  = [r[f"{host}_mem_pct"]  for r in health_rows if r[f"{host}_mem_pct"]  is not None]
        statuses  = [r[f"{host}_status"]   for r in health_rows if r[f"{host}_status"]   is not None]

        # container-down check (skip artist)
        if host not in _EXPECTED_DOWN and statuses:
            down_count = sum(1 for s in statuses if s not in _NORMAL_STATUSES)
            if down_count > 0:
                container_down.append(host)

        # cpu spikes
        cpu_over = sum(1 for v in cpu_vals if v > _CPU_WARN)
        if cpu_over:
            violations.append((f"{host} CPU > {_CPU_WARN}%", cpu_over))

        # mem spikes + critical check
        mem_over = sum(1 for v in mem_vals if v > _MEM_WARN)
        if mem_over:
            violations.append((f"{host} memory > {_MEM_WARN}%", mem_over))
        if any(v > _MEM_CRIT for v in mem_vals):
            critical = True

        # build host summary line
        if cpu_vals and mem_vals:
            avg_cpu = round(sum(cpu_vals) / len(cpu_vals), 1)
            max_cpu = round(max(cpu_vals), 1)
            avg_mem = round(sum(mem_vals) / len(mem_vals), 1)
            max_mem = round(max(mem_vals), 1)
            last_status = statuses[-1] if statuses else "unknown"
            if host in _EXPECTED_DOWN and last_status not in _NORMAL_STATUSES:
                host_summaries.append(f"{host}: offline (expected)")
            else:
                host_summaries.append(
                    f"{host}: status={last_status}, "
                    f"cpu avg {avg_cpu}% peak {max_cpu}%, "
                    f"mem avg {avg_mem}% peak {max_mem}%"
                )
        elif host in _EXPECTED_DOWN:
            host_summaries.append(f"{host}: offline (expected)")
        else:
            host_summaries.append(f"{host}: no data")

    # --- GPU analysis ---
    gpu_summary = "no data"
    if gpu_rows:
        temp_vals  = [r["gpu_temp_c"]   for r in gpu_rows if r["gpu_temp_c"]   is not None]
        util_vals  = [r["gpu_util_pct"] for r in gpu_rows if r["gpu_util_pct"] is not None]
        vram_used  = [r["vram_used_mb"]  for r in gpu_rows if r["vram_used_mb"]  is not None]
        vram_total = [r["vram_total_mb"] for r in gpu_rows if r["vram_total_mb"] is not None]

        if temp_vals:
            temp_over = sum(1 for v in temp_vals if v > _GPU_TEMP_WARN)
            if temp_over:
                violations.append((f"GPU temp > {_GPU_TEMP_WARN}°C", temp_over))
            if any(v > _GPU_TEMP_CRIT for v in temp_vals):
                critical = True

        if util_vals:
            util_over = sum(1 for v in util_vals if v > _GPU_UTIL_WARN)
            if util_over:
                violations.append((f"GPU utilization > {_GPU_UTIL_WARN}%", util_over))

        vram_pcts = []
        if vram_used and vram_total:
            vram_pcts = [
                u / t * 100
                for u, t in zip(vram_used, vram_total)
                if t > 0
            ]
            vram_over = sum(1 for v in vram_pcts if v > _VRAM_WARN_PCT)
            if vram_over:
                violations.append((f"VRAM usage > {_VRAM_WARN_PCT}%", vram_over))

        avg_temp  = round(sum(temp_vals) / len(temp_vals), 1)  if temp_vals  else None
        peak_temp = max(temp_vals)                               if temp_vals  else None
        avg_util  = round(sum(util_vals) / len(util_vals), 1)  if util_vals  else None
        last_vram = round(vram_used[-1] / 1024, 1)             if vram_used  else None
        total_vram = round(vram_total[-1] / 1024, 1)           if vram_total else None
        avg_vram_pct = round(sum(vram_pcts) / len(vram_pcts), 1) if vram_pcts else None

        parts = []
        if avg_temp is not None:
            parts.append(f"temp avg {avg_temp}°C peak {peak_temp}°C")
        if avg_util is not None:
            parts.append(f"util avg {avg_util}%")
        if last_vram is not None:
            parts.append(f"VRAM {last_vram}/{total_vram} GB ({avg_vram_pct}% avg)")
        gpu_summary = ", ".join(parts) if parts else "no data"

    sample_count = len(health_rows)
    grade = _grade(violations, container_down, critical)

    return {
        "grade":          grade,
        "sample_count":   sample_count,
        "violations":     violations,
        "container_down": container_down,
        "host_summaries": host_summaries,
        "gpu_summary":    gpu_summary,
        "critical":       critical,
    }


def _build_report(analysis: dict) -> str:
    grade   = analysis["grade"]
    samples = analysis["sample_count"]
    lines   = [f"SYSTEM STATUS REPORT — last 24 hours ({samples} hourly snapshots)\n"]
    lines.append(f"Overall grade: {grade}")

    if analysis["container_down"]:
        lines.append(f"ALERT — containers unexpectedly offline: {', '.join(analysis['container_down'])}")

    if analysis["critical"]:
        lines.append("ALERT — critical threshold breached (see details below)")

    if analysis["violations"]:
        lines.append("\nThreshold breaches:")
        for label, count in analysis["violations"]:
            sustained = count >= _SUSTAINED_MIN
            qualifier = "SUSTAINED" if sustained else "spike"
            lines.append(f"  {label} — {count} occurrence(s) [{qualifier}]")
    else:
        lines.append("\nNo threshold breaches detected.")

    lines.append("\nPer-container summary:")
    for summary in analysis["host_summaries"]:
        lines.append(f"  {summary}")

    lines.append(f"\nGPU: {analysis['gpu_summary']}")

    return "\n".join(lines)



@tool("system_status")
async def _execute(args: dict, ctx: PipelineContext) -> str:
    db = providers.get_db("system")
    if db is None or not db.is_ready:
        return "System health database is unavailable right now."

    try:
        analysis = await _analyze(db)
    except Exception as e:
        log.error("system_status_tool_failed", error=str(e))
        return "System health check failed — couldn't read health data."

    if analysis["sample_count"] == 0:
        return (
            "No health snapshots recorded yet in the last 24 hours. "
            "The system_health collector runs hourly."
        )

    log.info("system_status_tool_ok",
             grade=analysis["grade"],
             violations=len(analysis["violations"]))
    return _build_report(analysis)
