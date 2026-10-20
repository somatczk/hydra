"""Structured logging setup via structlog.

Provides JSON output for production and colored console output for
development, with context-binding support (strategy_id, symbol, etc.).
"""

from __future__ import annotations

import logging
import sys
from typing import Any

import structlog

_UVICORN_LOGGERS = ("uvicorn", "uvicorn.error", "uvicorn.access")


def setup_logging(
    level: str = "INFO",
    log_format: str = "json",
) -> None:
    """Configure structlog and the stdlib logging bridge.

    Parameters
    ----------
    level:
        Log level string (``"DEBUG"``, ``"INFO"``, ``"WARNING"``, etc.).
    log_format:
        ``"json"`` for production JSON lines, ``"colored"`` for human-readable
        coloured console output.
    """
    log_level = getattr(logging, level.upper(), logging.INFO)

    # Shared processors for both modes
    shared_processors: list[structlog.types.Processor] = [
        structlog.contextvars.merge_contextvars,
        structlog.stdlib.add_log_level,
        structlog.stdlib.add_logger_name,
        structlog.processors.TimeStamper(fmt="iso"),
        structlog.processors.StackInfoRenderer(),
        structlog.processors.UnicodeDecoder(),
    ]

    render_chain: list[structlog.types.Processor]
    if log_format == "colored":
        # Development: colored console output
        render_chain = [structlog.dev.ConsoleRenderer(colors=True)]
    else:
        # Production: JSON lines
        render_chain = [structlog.processors.format_exc_info, structlog.processors.JSONRenderer()]

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
        foreign_pre_chain=shared_processors,
        processors=[
            structlog.stdlib.ProcessorFormatter.remove_processors_meta,
            *render_chain,
        ],
    )

    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(formatter)

    root_logger = logging.getLogger()
    root_logger.handlers.clear()
    root_logger.addHandler(handler)
    root_logger.setLevel(log_level)

    for name in _UVICORN_LOGGERS:
        uvicorn_logger = logging.getLogger(name)
        uvicorn_logger.handlers.clear()
        uvicorn_logger.propagate = True


def get_logger(name: str, **initial_context: Any) -> structlog.stdlib.BoundLogger:
    """Get a bound structlog logger with optional initial context.

    Parameters
    ----------
    name:
        Logger name (typically the module ``__name__``).
    **initial_context:
        Key-value pairs to bind to every log message from this logger
        (e.g., ``strategy_id="momentum"``, ``symbol="BTCUSDT"``).
    """
    logger: structlog.stdlib.BoundLogger = structlog.get_logger(name)
    if initial_context:
        logger = logger.bind(**initial_context)
    return logger
