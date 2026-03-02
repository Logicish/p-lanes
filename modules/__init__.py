# modules/__init__.py
#
# Author:  Logicish
# Company: Logic-Ish Designs
# Date:    2/26/2026
#
# ==================================================
# Module auto-discovery entry point.
# Importing this package scans the modules/ directory
# and imports all discovered modules, triggering their
# @register decorators.
#
# Knows about: core/events (discover_modules).
# ==================================================

from core.events import discover_modules

discover_modules("modules")