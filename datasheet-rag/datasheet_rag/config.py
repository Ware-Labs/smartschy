"""Configuration helpers for the datasheet-rag CLI."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path


@dataclass(slots=True)
class AppConfig:
    """Shared command options for local paths."""

    db_path: Path = Path("./datasheets.db")
    output_dir: Path = Path("./out")
    log_level: str = "INFO"


def build_config(
    *,
    db_path: str | Path | None = None,
    output_dir: str | Path | None = None,
    log_level: str = "INFO",
) -> AppConfig:
    """Create a normalized application config from CLI input."""

    return AppConfig(
        db_path=Path(db_path) if db_path is not None else Path("./datasheets.db"),
        output_dir=Path(output_dir) if output_dir is not None else Path("./out"),
        log_level=log_level.upper(),
    )
