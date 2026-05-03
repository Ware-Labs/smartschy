#!/usr/bin/env python3
"""Generate derived review artifacts from normalized DSN + BOM."""

from __future__ import annotations

import argparse
from pathlib import Path
from typing import List

from bom_ingest import parse_bom_csv
from parse_dsn import normalize_dsn
from review_artifacts import build_artifact_pack, write_artifact_pack


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Build review artifacts consumed by build_llm_summary.py")
    parser.add_argument("dsn_files", nargs="+", help="Input DSN or normalized JSON files")
    parser.add_argument("--bom", required=True, help="BOM CSV path (strict schema)")
    parser.add_argument("--out", default="derived_onecmd", help="Output root directory")
    return parser.parse_args()


def _board_name(normalized: dict, dsn_path: Path) -> str:
    stem = dsn_path.stem
    # Keep output folder stable to the input filename.
    if stem.endswith(".normalized"):
        stem = stem[: -len(".normalized")]
    return stem.replace(" ", "_")


def main() -> None:
    args = parse_args()
    bom_model = parse_bom_csv(Path(args.bom))
    out_root = Path(args.out)
    for item in args.dsn_files:
        dsn_path = Path(item)
        normalized = normalize_dsn(dsn_path)
        board_name = _board_name(normalized, dsn_path)
        pack = build_artifact_pack(normalized, bom_model)
        write_artifact_pack(pack, out_root / board_name)


if __name__ == "__main__":
    main()
