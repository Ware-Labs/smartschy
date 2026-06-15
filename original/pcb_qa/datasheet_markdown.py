from __future__ import annotations

from pathlib import Path

try:
    from pypdf import PdfReader
except ImportError:  # pragma: no cover
    PdfReader = None

from .utils import write_json


def _extract_pages(pdf_path: Path) -> list[str]:
    if PdfReader is None:
        raise RuntimeError("pypdf is required for datasheet markdown extraction.")
    reader = PdfReader(str(pdf_path))
    return [page.extract_text() or "" for page in reader.pages]


def _pdf_to_markdown(pdf_path: Path) -> str:
    lines: list[str] = [f"# Datasheet: {pdf_path.name}", ""]
    for idx, page in enumerate(_extract_pages(pdf_path), start=1):
        lines.append(f"## Page {idx}")
        lines.append("")
        text = page.strip()
        lines.append(text if text else "_No extractable text on this page._")
        lines.append("")
    return "\n".join(lines).strip() + "\n"


def build_datasheet_markdown(resources_dir: Path, output_dir: Path) -> dict[str, object]:
    resources_dir = resources_dir.resolve()
    output_dir = output_dir.resolve()
    output_dir.mkdir(parents=True, exist_ok=True)
    md_dir = output_dir / "markdown"
    md_dir.mkdir(parents=True, exist_ok=True)
    rows: list[dict[str, object]] = []
    for pdf_path in sorted(resources_dir.glob("*.pdf")):
        md_name = f"{pdf_path.stem}.md"
        md_path = md_dir / md_name
        body = _pdf_to_markdown(pdf_path)
        md_path.write_text(body, encoding="utf-8")
        rows.append(
            {
                "pdf_name": pdf_path.name,
                "markdown_name": md_name,
                "markdown_path": str(md_path),
            }
        )
    manifest = {
        "resources_dir": str(resources_dir),
        "markdown_dir": str(md_dir),
        "datasheet_count": len(rows),
        "items": rows,
    }
    write_json(output_dir / "datasheet_markdown_manifest.json", manifest)
    return manifest

