"""Logging helpers for the datasheet-rag CLI."""

from __future__ import annotations

import logging


def configure_logging(level: str = "INFO") -> None:
    """Set up a minimal console logger for CLI commands."""

    logging.basicConfig(
        level=getattr(logging, level.upper(), logging.INFO),
        format="%(levelname)s %(name)s: %(message)s",
    )
