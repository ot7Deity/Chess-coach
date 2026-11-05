"""Structured logging shared by every process.

Workers write a lot of near-identical lines ("claimed job", "evaluated
position"). Key-value output makes those greppable and lets the benchmark
scripts parse them instead of scraping prose.
"""

from __future__ import annotations

import logging

import structlog

_configured = False


def setup_logging(level: str = "INFO", json_output: bool = False) -> None:
    global _configured
    if _configured:
        return

    logging.basicConfig(format="%(message)s", level=getattr(logging, level, logging.INFO))

    renderer = (
        structlog.processors.JSONRenderer()
        if json_output
        else structlog.dev.ConsoleRenderer(colors=False)
    )

    structlog.configure(
        processors=[
            structlog.contextvars.merge_contextvars,
            structlog.processors.add_log_level,
            structlog.processors.TimeStamper(fmt="iso", utc=True),
            structlog.processors.StackInfoRenderer(),
            structlog.processors.format_exc_info,
            renderer,
        ],
        wrapper_class=structlog.make_filtering_bound_logger(getattr(logging, level, logging.INFO)),
        cache_logger_on_first_use=True,
    )
    _configured = True


def get_logger(name: str) -> structlog.stdlib.BoundLogger:
    return structlog.get_logger(name)
