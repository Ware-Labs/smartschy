#!/usr/bin/env python3
"""Deterministic PDF extraction helpers for datasheet ingestion."""

from __future__ import annotations

import io
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

try:
    import fitz
except ImportError:  # pragma: no cover - environment dependent
    fitz = None


def _safe_float(value: Any) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


def _rect_to_bbox(rect_like: Any) -> List[float]:
    rect = fitz.Rect(rect_like)
    return [
        round(float(rect.x0), 3),
        round(float(rect.y0), 3),
        round(float(rect.x1), 3),
        round(float(rect.y1), 3),
    ]


def _clean_text(value: str) -> str:
    return " ".join(value.replace("\u00a0", " ").split())


def _span_is_bold(span: Dict[str, Any]) -> bool:
    font_name = str(span.get("font", "")).lower()
    flags = int(span.get("flags", 0))
    return "bold" in font_name or bool(flags & (1 << 4))


def _extract_text_blocks(page: fitz.Page) -> List[Dict[str, Any]]:
    text_dict = page.get_text("dict")
    blocks: List[Dict[str, Any]] = []
    for raw_block in text_dict.get("blocks", []):
        if int(raw_block.get("type", -1)) != 0:
            continue

        lines = raw_block.get("lines", [])
        block_lines: List[str] = []
        font_sizes: List[float] = []
        font_names: List[str] = []
        is_bold = False
        span_count = 0

        for line in lines:
            line_parts: List[str] = []
            for span in line.get("spans", []):
                text = str(span.get("text", "")).strip()
                if not text:
                    continue
                line_parts.append(text)
                font_sizes.append(_safe_float(span.get("size")))
                font_names.append(str(span.get("font", "")))
                is_bold = is_bold or _span_is_bold(span)
                span_count += 1
            if line_parts:
                block_lines.append(" ".join(line_parts))

        merged_text = _clean_text("\n".join(block_lines))
        if not merged_text:
            continue

        bbox = _rect_to_bbox(raw_block.get("bbox"))
        blocks.append(
            {
                "text": merged_text,
                "bbox": bbox,
                "line_count": len(block_lines),
                "span_count": span_count,
                "font_size_max": round(max(font_sizes), 3) if font_sizes else 0.0,
                "font_size_avg": round(sum(font_sizes) / len(font_sizes), 3) if font_sizes else 0.0,
                "font_names": sorted({name for name in font_names if name}),
                "is_bold": is_bold,
            }
        )

    blocks.sort(key=lambda item: (item["bbox"][1], item["bbox"][0]))
    return blocks


def _extract_links(page: fitz.Page) -> List[Dict[str, Any]]:
    items: List[Dict[str, Any]] = []
    for entry in page.get_links():
        target = ""
        if entry.get("uri"):
            target = str(entry["uri"])
        elif entry.get("file"):
            target = str(entry["file"])
        elif entry.get("page") is not None:
            target = f"page:{int(entry['page']) + 1}"

        items.append(
            {
                "bbox": _rect_to_bbox(entry.get("from", page.rect)),
                "target": target,
                "kind": int(entry.get("kind", 0)),
                "xref": int(entry.get("xref", 0)),
            }
        )
    return items


def _image_filename(page_number: int, index: int, extension: str) -> str:
    ext = (extension or "png").lower().strip(".")
    return f"page_{page_number:03d}_img_{index:03d}.{ext}"


def _extract_images(
    doc: fitz.Document,
    page: fitz.Page,
    page_number: int,
    images_dir: Path,
) -> Tuple[List[Dict[str, Any]], List[str]]:
    figures: List[Dict[str, Any]] = []
    warnings: List[str] = []
    page_images = page.get_images(full=True)
    for idx, image_info in enumerate(page_images, start=1):
        xref = int(image_info[0])
        try:
            extracted = doc.extract_image(xref)
        except RuntimeError as exc:
            warnings.append(f"page {page_number}: failed image xref {xref}: {exc}")
            continue

        image_bytes = extracted.get("image")
        if not image_bytes:
            warnings.append(f"page {page_number}: empty image bytes for xref {xref}")
            continue

        extension = str(extracted.get("ext", "png"))
        filename = _image_filename(page_number, idx, extension)
        output_path = images_dir / filename
        output_path.write_bytes(image_bytes)
        bbox = _rect_to_bbox(page.rect)

        figures.append(
            {
                "page": page_number,
                "image_index": idx,
                "xref": xref,
                "path": f"images/{filename}",
                "bbox": bbox,
                "width": int(extracted.get("width", 0)),
                "height": int(extracted.get("height", 0)),
                "colorspace": int(extracted.get("colorspace", 0)),
            }
        )
    return figures, warnings


def _ocr_page(page: fitz.Page) -> Tuple[Optional[str], Optional[str]]:
    try:
        from PIL import Image
        import pytesseract
    except ImportError:
        return None, "OCR dependencies missing (install pillow and pytesseract)"

    pixmap = page.get_pixmap(dpi=250, alpha=False)
    image = Image.open(io.BytesIO(pixmap.tobytes("png")))
    text = pytesseract.image_to_string(image)
    text = _clean_text(text)
    if not text:
        return None, "OCR returned no text"
    return text, None


def extract_pdf(
    pdf_path: Path,
    images_dir: Path,
    *,
    use_ocr: bool = False,
    ocr_char_threshold: int = 40,
) -> Dict[str, Any]:
    """Extract deterministic PDF signals and optional OCR fallback."""
    if fitz is None:
        raise RuntimeError("PyMuPDF is not installed. Run: python -m pip install pymupdf")
    pdf_path = pdf_path.resolve()
    images_dir.mkdir(parents=True, exist_ok=True)
    warnings: List[str] = []
    pages: List[Dict[str, Any]] = []
    ocr_pages: List[int] = []

    with fitz.open(pdf_path) as doc:
        metadata = dict(doc.metadata or {})
        metadata["page_count"] = doc.page_count
        metadata["title"] = metadata.get("title") or pdf_path.stem

        for page_index in range(doc.page_count):
            page = doc.load_page(page_index)
            page_number = page_index + 1
            text_blocks = _extract_text_blocks(page)
            links = _extract_links(page)
            figures, image_warnings = _extract_images(doc, page, page_number, images_dir)
            warnings.extend(image_warnings)

            char_count = sum(len(block["text"]) for block in text_blocks)
            page_warnings: List[str] = []
            ocr_used = False

            if use_ocr and char_count < ocr_char_threshold:
                ocr_text, ocr_warning = _ocr_page(page)
                if ocr_text:
                    text_blocks.append(
                        {
                            "text": ocr_text,
                            "bbox": _rect_to_bbox(page.rect),
                            "line_count": len(ocr_text.splitlines()),
                            "span_count": 0,
                            "font_size_max": 0.0,
                            "font_size_avg": 0.0,
                            "font_names": [],
                            "is_bold": False,
                            "confidence": "ocr",
                        }
                    )
                    ocr_used = True
                    ocr_pages.append(page_number)
                    char_count += len(ocr_text)
                elif ocr_warning:
                    page_warnings.append(ocr_warning)

            if char_count < ocr_char_threshold:
                page_warnings.append("very low extracted text")

            pages.append(
                {
                    "page_number": page_number,
                    "width": round(float(page.rect.width), 3),
                    "height": round(float(page.rect.height), 3),
                    "char_count": char_count,
                    "text_blocks_raw": text_blocks,
                    "links": links,
                    "figures": figures,
                    "ocr_used": ocr_used,
                    "warnings": page_warnings,
                }
            )

    return {
        "source_pdf": str(pdf_path),
        "metadata": metadata,
        "extraction_method": "pymupdf",
        "ocr_enabled": use_ocr,
        "ocr_pages": ocr_pages,
        "pages": pages,
        "warnings": warnings,
    }
