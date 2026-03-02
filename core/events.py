# core/events.py
#
# Author:  Logicish
# Company: Logic-Ish Designs
# Date:    2/26/2026
#
# ==================================================
# Lightweight event bus and module registry.
# Modules self-register via @register decorator with
# a pipeline phase. Auto-discovery scans modules/.
# Core never imports modules directly.
#
# Pipeline phases (in execution order):
#   classifier → enricher → responder → finalizer
#
# Channel, Transporter, and Processor are handled by
# core (transport.py, llm.py) — not modules.
#
# Knows about: nothing — modules depend on this,
#              not the other way around.
# ==================================================

# ==================================================
# Imports
# ==================================================
import importlib
import pkgutil
from pathlib import Path

import structlog

log = structlog.get_logger()

# ==================================================
# Pipeline Phases
# ==================================================

PHASES = ("classifier", "enricher", "responder", "finalizer")

# ==================================================
# Registry
# ==================================================

# phase → { module_name → handler }
_registry: dict[str, dict[str, callable]] = {phase: {} for phase in PHASES}


def register(module_name: str, phase: str):
    # decorator for modules to self-register into a pipeline phase
    if phase not in PHASES:
        raise ValueError(
            f"Invalid phase '{phase}' for module '{module_name}'. "
            f"Valid phases: {PHASES}"
        )

    def decorator(func):
        if module_name in _registry[phase]:
            log.warning("module_overwrite", module=module_name, phase=phase)
        _registry[phase][module_name] = func
        log.info("module_registered", module=module_name, phase=phase)
        return func
    return decorator


def get_phase(phase: str) -> dict[str, callable]:
    return _registry.get(phase, {})


def get_registry() -> dict[str, dict[str, callable]]:
    return _registry


def is_registered(module_name: str) -> bool:
    for phase_modules in _registry.values():
        if module_name in phase_modules:
            return True
    return False


# ==================================================
# Auto-Discovery
# ==================================================

def discover_modules(package_name: str = "modules"):
    try:
        package = importlib.import_module(package_name)
    except ModuleNotFoundError:
        log.warning("modules_package_not_found", package=package_name)
        return

    package_path = Path(package.__file__).parent

    for importer, modname, ispkg in pkgutil.iter_modules([str(package_path)]):
        full_name = f"{package_name}.{modname}"
        try:
            importlib.import_module(full_name)
            log.info("module_discovered", module=full_name)
        except Exception as e:
            log.error("module_discovery_failed", module=full_name, error=str(e))