# core/log.py
#
# Author:  Logicish
# Company: Logic-Ish Designs
# Date:    2/26/2026
#
# ==================================================
# Structured logging setup using structlog.
# Call setup_logging() once at startup.
# All other modules: import structlog; log = structlog.get_logger()
#
# Knows about: config (log paths, level, format).
# ==================================================

# ==================================================
# Imports
# ==================================================
import logging
import sys

import structlog

from config import LOG_FILE, LOG_LEVEL, LOG_FORMAT

# ==================================================
# Setup
# ==================================================

def setup_logging():
    LOG_FILE.parent.mkdir(parents=True, exist_ok=True)

    # configure stdlib logging as structlog's sink
    handler_file = logging.FileHandler(LOG_FILE, encoding="utf-8")
    handler_stream = logging.StreamHandler(sys.stdout)

    level = getattr(logging, LOG_LEVEL.upper(), logging.INFO)

    logging.basicConfig(
        format="%(message)s",
        level=level,
        handlers=[handler_file, handler_stream],
        force=True,
    )

    # structlog processors
    shared_processors = [
        structlog.contextvars.merge_contextvars,
        structlog.stdlib.add_log_level,
        structlog.stdlib.add_logger_name,
        structlog.processors.TimeStamper(fmt="iso"),
        structlog.processors.StackInfoRenderer(),
        structlog.processors.UnicodeDecoder(),
    ]

    if LOG_FORMAT == "json":
        renderer = structlog.processors.JSONRenderer()
    else:
        renderer = structlog.dev.ConsoleRenderer(colors=True)

    structlog.configure(
        processors=[
            *shared_processors,
            structlog.stdlib.ProcessorFormatter.wrap_for_formatter,
        ],
        logger_factory=structlog.stdlib.LoggerFactory(),
        wrapper_class=structlog.stdlib.BoundLogger,
        cache_logger_on_first_use=True,
    )

    formatter = structlog.stdlib.ProcessorFormatter(
        processors=[
            structlog.stdlib.ProcessorFormatter.remove_processors_meta,
            renderer,
        ],
    )

    for handler in logging.root.handlers:
        handler.setFormatter(formatter)