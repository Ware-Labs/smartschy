"""Stub handlers for later phases."""

from __future__ import annotations

import typer


def announce_stub(command_name: str, details: list[tuple[str, object]]) -> None:
    """Print a consistent placeholder message for unfinished commands."""

    typer.echo(f"[phase-0 stub] `{command_name}` is wired up but not implemented yet.")
    for label, value in details:
        typer.echo(f"- {label}: {value}")
