from __future__ import annotations

import json
from pathlib import Path

import fitz
from typer.testing import CliRunner

import datasheet_rag.cli as cli_module
from datasheet_rag import query_planner, query_reranker
from datasheet_rag.answering import AnswerResult
from datasheet_rag.cli import app
from datasheet_rag.entities import QueryResponse
from datasheet_rag.llm_provider import AnswerCitation
from datasheet_rag.storage import inspect_documents

runner = CliRunner()


def _create_sample_pdf(pdf_path: Path) -> None:
    document = fitz.open()
    page_one = document.new_page()
    page_one.insert_text(
        (72, 72),
        (
            "Nordic nRF54L15\n"
            "8.8.1 Pin configuration\n\n"
            "The GPIO block provides programmable digital input and output behavior."
        ),
    )
    page_two = document.new_page()
    page_two.insert_text(
        (72, 72),
        (
            "Peripheral routing overview\n\n"
            "P1.04 can be configured for GPIO and UARTE. "
            "This page also mentions SPI and timer routing for context."
        ),
    )
    document.set_metadata({"title": "Sample Datasheet", "author": "Codex"})
    document.save(pdf_path)
    document.close()


def _create_table_pdf(pdf_path: Path) -> None:
    document = fitz.open()

    page_one = document.new_page()
    page_one.insert_text(
        (72, 72),
        "10.1.3 Package pin assignments\nTable 1: Pin assignments",
    )

    page_two = document.new_page(width=595, height=842)
    shape = page_two.new_shape()
    left, top = 72, 120
    col_widths = [55, 110, 140, 170]
    row_heights = [28, 44, 44]
    xs = [left]
    for width in col_widths:
        xs.append(xs[-1] + width)
    ys = [top]
    for height in row_heights:
        ys.append(ys[-1] + height)
    for x in xs:
        shape.draw_line((x, ys[0]), (x, ys[-1]))
    for y in ys:
        shape.draw_line((xs[0], y), (xs[-1], y))
    shape.finish(width=1, color=(0, 0, 0))
    shape.commit()

    rows = [
        ["Pin", "Name", "Function", "Description"],
        ["5", "P1.04", "GPIO / UARTE TXD", "General purpose I/O"],
        ["6", "P1.05", "GPIO / UARTE RXD", "General purpose I/O"],
    ]
    for row_index, row in enumerate(rows):
        for column_index, cell in enumerate(row):
            page_two.insert_textbox(
                (xs[column_index] + 4, ys[row_index] + 4, xs[column_index + 1] - 4, ys[row_index + 1] - 4),
                cell,
                fontsize=10,
            )

    document.set_metadata({"title": "Table Datasheet", "author": "Codex"})
    document.save(pdf_path)
    document.close()


def _create_visual_tables_pdf(pdf_path: Path) -> None:
    document = fitz.open()
    page = document.new_page(width=595, height=842)

    blue = (0 / 255, 148 / 255, 196 / 255)
    dark = (53 / 255, 62 / 255, 72 / 255)
    teal = (0 / 255, 156 / 255, 188 / 255)
    black = (0, 0, 0)
    white = (1, 1, 1)

    page.insert_text((72, 72), "Power consumption highlights", fontsize=18, color=blue)

    def draw_table(
        top: float,
        headers: list[str],
        rows: list[tuple[str, list[str]]],
        col_widths: list[float],
    ) -> float:
        left = 72
        xs = [left]
        for width in col_widths:
            xs.append(xs[-1] + width)
        current_top = top
        row_height = 24

        def draw_row(y0: float, values: list[str], fill: tuple[float, float, float] | None, bold: bool = False) -> float:
            y1 = y0 + row_height
            shape = page.new_shape()
            for column_index in range(len(values)):
                rect = fitz.Rect(xs[column_index], y0, xs[column_index + 1], y1)
                if fill is not None:
                    shape.draw_rect(rect)
            if fill is not None:
                shape.finish(color=black, fill=fill, width=0.5)
                shape.commit()
            for x in xs:
                page.draw_line((x, y0), (x, y1), color=black, width=0.5)
            page.draw_line((xs[0], y0), (xs[-1], y0), color=black, width=0.5)
            page.draw_line((xs[0], y1), (xs[-1], y1), color=black, width=0.5)
            text_color = white if fill in {dark, teal} else black
            for column_index, value in enumerate(values):
                if not value:
                    continue
                page.insert_textbox(
                    fitz.Rect(xs[column_index] + 4, y0 + 4, xs[column_index + 1] - 4, y1 - 2),
                    value,
                    fontsize=10,
                    color=text_color,
                    fontname="helv",
                    align=0 if column_index == 0 else 2,
                )
            return y1

        current_top = draw_row(current_top, headers, dark, bold=True)
        for row_type, values in rows:
            fill = teal if row_type == "group" else None
            current_top = draw_row(current_top, values, fill, bold=row_type == "group")
        return current_top

    table1_rows = [
        ("group", ["Active with radio", ""]),
        ("data", ["Bluetooth LE TX 1 Mbps at 0 dBm", "4.8 mA"]),
        ("data", ["Bluetooth LE TX 1 Mbps at +4 dBm", "6.6 mA"]),
        ("data", ["Bluetooth LE TX 1 Mbps at +8 dBm", "9.8 mA"]),
        ("data", ["Bluetooth LE RX 1 Mbps", "3.4 mA"]),
        ("group", ["Active with processing", ""]),
        ("data", ["CPU CoreMark from RRAM with cache", "2.6 mA"]),
        ("group", ["Sleep", ""]),
        ("data", ["System ON IDLE with GRTC (XOSC) and 256 KB RAM", "2.9 μA"]),
        ("data", ["System ON IDLE with GRTC (XOSC) and 192 KB RAM", "2.6 μA"]),
        ("data", ["System ON IDLE with GRTC (XOSC) and 96 KB RAM", "1.7 μA"]),
        ("data", ["System OFF with GRTC wakeup", "0.9 μA"]),
        ("data", ["System OFF", "0.7 μA"]),
    ]
    bottom = draw_table(92, ["Power mode", "Current @ 3.0 V"], table1_rows, [340, 120])

    page.insert_text((72, bottom + 30), "Product variants", fontsize=18, color=blue)
    table2_rows = [
        ("data", ["nRF54L15", "1524 KB", "256 KB"]),
        ("data", ["nRF54L10", "1012 KB", "192 KB"]),
        ("data", ["nRF54L05", "500 KB", "96 KB"]),
    ]
    draw_table(bottom + 50, ["Part number", "NVM", "RAM"], table2_rows, [220, 120, 120])

    document.set_metadata({"title": "Visual Tables Datasheet", "author": "Codex"})
    document.save(pdf_path)
    document.close()


def _create_register_tables_pdf(pdf_path: Path) -> None:
    document = fitz.open()
    page = document.new_page(width=595, height=842)
    page.insert_text((410, 40), "Power and clock management", fontsize=12)

    def draw_register_block(top: float, heading: str, address: str, task_name: str) -> None:
        page.insert_text((72, top), heading, fontsize=14)
        page.insert_text((72, top + 20), address, fontsize=11)
        page.insert_text((72, top + 38), f"Subscribe configuration for task {task_name}", fontsize=11)
        table_top = top + 60
        page.insert_text((72, table_top), "Bit number", fontsize=10)
        page.insert_text((248, table_top), "31 30 29 28 27 26 25 24 23 22 21 20 19 18 17 16 15 14 13 12 11 10 9 8 7 6 5 4 3 2 1 0", fontsize=7)
        page.insert_text((72, table_top + 14), "ID", fontsize=10)
        page.insert_text((248, table_top + 14), "B", fontsize=10)
        page.insert_text((470, table_top + 14), "A A A A A A A A", fontsize=8)
        page.insert_text((72, table_top + 28), "Reset 0x00000000", fontsize=10)
        page.insert_text((248, table_top + 28), "0 0 0 0 0 0 0 0 0 0 0 0 0 0 0 0 0 0 0 0 0 0 0 0 0 0 0 0 0 0 0 0", fontsize=8)
        page.insert_text((72, table_top + 42), "ID", fontsize=10)
        page.insert_text((115, table_top + 42), "R/W", fontsize=10)
        page.insert_text((150, table_top + 42), "Field", fontsize=10)
        page.insert_text((220, table_top + 42), "Value ID", fontsize=10)
        page.insert_text((280, table_top + 42), "Value", fontsize=10)
        page.insert_text((360, table_top + 42), "Description", fontsize=10)
        page.insert_text((72, table_top + 56), "A", fontsize=10)
        page.insert_text((115, table_top + 56), "RW", fontsize=10)
        page.insert_text((150, table_top + 56), "CHIDX", fontsize=10)
        page.insert_text((280, table_top + 56), "[0..255]", fontsize=10)
        page.insert_text((360, table_top + 56), f"DPPI channel that task {task_name} will subscribe to", fontsize=9)
        page.insert_text((72, table_top + 70), "B", fontsize=10)
        page.insert_text((115, table_top + 70), "RW", fontsize=10)
        page.insert_text((150, table_top + 70), "EN", fontsize=10)
        page.insert_text((220, table_top + 84), "Disabled", fontsize=10)
        page.insert_text((280, table_top + 84), "0", fontsize=10)
        page.insert_text((360, table_top + 84), "Disable subscription", fontsize=10)
        page.insert_text((220, table_top + 98), "Enabled", fontsize=10)
        page.insert_text((280, table_top + 98), "1", fontsize=10)
        page.insert_text((360, table_top + 98), "Enable subscription", fontsize=10)

    draw_register_block(72, "5.4.3.11 SUBSCRIBE_XOSTOP", "Address offset: 0x084", "XOSTOP")
    draw_register_block(320, "5.4.3.12 SUBSCRIBE_PLLSTART", "Address offset: 0x088", "PLLSTART")
    document.set_metadata({"title": "Register Datasheet", "author": "Codex"})
    document.save(pdf_path)
    document.close()


def _create_multiline_pin_pdf(pdf_path: Path) -> None:
    document = fitz.open()
    page = document.new_page(width=720, height=842)
    page.insert_text((72, 72), "Hardware and layout", fontsize=14)

    shape = page.new_shape()
    left, top = 72, 110
    col_widths = [45, 70, 125, 135, 170, 95]
    row_heights = [28, 64, 78]
    xs = [left]
    for width in col_widths:
        xs.append(xs[-1] + width)
    ys = [top]
    for height in row_heights:
        ys.append(ys[-1] + height)
    for x in xs:
        shape.draw_line((x, ys[0]), (x, ys[-1]))
    for y in ys:
        shape.draw_line((xs[0], y), (xs[-1], y))
    shape.finish(width=1, color=(0, 0, 0))
    shape.commit()

    rows = [
        ["Pin", "Clock\npin", "Name", "Function", "Description", "Dedicated\nfunction"],
        ["5", "Yes", "P1.04\nASO[0]\nAIN0", "Digital I/O\nDigital I/O\nAnalog input", "General purpose I/O\nTAMPC active shield 0 output\nAnalog input", "TAMPC"],
        ["6", "", "P1.05\nASI[0]\nRADIO[6]\nAIN1", "Digital I/O\nDigital I/O\nDigital I/O\nAnalog input", "General purpose I/O\nTAMPC active shield 0 input\nRADIO DFEGPIO\nAnalog input", "TAMPC\nRADIO"],
    ]

    for row_index, row in enumerate(rows):
        for column_index, cell in enumerate(row):
            page.insert_textbox(
                (xs[column_index] + 4, ys[row_index] + 4, xs[column_index + 1] - 4, ys[row_index + 1] - 4),
                cell,
                fontsize=9,
            )

    document.set_metadata({"title": "Pin Datasheet", "author": "Codex"})
    document.save(pdf_path)
    document.close()


def _create_regulator_pdf(pdf_path: Path) -> None:
    document = fitz.open()
    page = document.new_page(width=595, height=842)
    page.insert_text((72, 72), "LDO Regulator XR1234", fontsize=16)
    page.insert_text((72, 96), "Product variants", fontsize=14)

    shape = page.new_shape()
    left, top = 72, 120
    col_widths = [160, 120, 120]
    row_heights = [28, 28, 28]
    xs = [left]
    for width in col_widths:
        xs.append(xs[-1] + width)
    ys = [top]
    for height in row_heights:
        ys.append(ys[-1] + height)
    for x in xs:
        shape.draw_line((x, ys[0]), (x, ys[-1]))
    for y in ys:
        shape.draw_line((xs[0], y), (xs[-1], y))
    shape.finish(width=1, color=(0, 0, 0))
    shape.commit()

    rows = [
        ["Part number", "Package", "Current limit"],
        ["XR1234A", "SOT-23-5", "300 mA"],
        ["XR1234B", "DFN-6", "500 mA"],
    ]
    for row_index, row in enumerate(rows):
        for column_index, cell in enumerate(row):
            page.insert_textbox(
                (xs[column_index] + 4, ys[row_index] + 4, xs[column_index + 1] - 4, ys[row_index + 1] - 4),
                cell,
                fontsize=10,
            )

    spec_top = 250
    page.insert_text((72, spec_top - 24), "Electrical characteristics", fontsize=14)
    shape = page.new_shape()
    spec_widths = [150, 90, 90, 90, 70]
    spec_heights = [28, 28, 28]
    spec_xs = [72]
    for width in spec_widths:
        spec_xs.append(spec_xs[-1] + width)
    spec_ys = [spec_top]
    for height in spec_heights:
        spec_ys.append(spec_ys[-1] + height)
    for x in spec_xs:
        shape.draw_line((x, spec_ys[0]), (x, spec_ys[-1]))
    for y in spec_ys:
        shape.draw_line((spec_xs[0], y), (spec_xs[-1], y))
    shape.finish(width=1, color=(0, 0, 0))
    shape.commit()
    spec_rows = [
        ["Parameter", "Min", "Typ", "Max", "Unit"],
        ["Output voltage", "1.8", "3.3", "5.0", "V"],
        ["Shutdown current", None, "1.0", "5.0", "uA"],
    ]
    for row_index, row in enumerate(spec_rows):
        for column_index, cell in enumerate(row):
            if not cell:
                continue
            page.insert_textbox(
                (spec_xs[column_index] + 4, spec_ys[row_index] + 4, spec_xs[column_index + 1] - 4, spec_ys[row_index + 1] - 4),
                cell,
                fontsize=10,
            )

    document.set_metadata({"title": "XR1234 Regulator Datasheet", "author": "Codex"})
    document.save(pdf_path)
    document.close()


def _create_logic_modes_pdf(pdf_path: Path) -> None:
    document = fitz.open()
    page = document.new_page(width=595, height=842)
    page.insert_text((72, 72), "Quad buffer logic IC", fontsize=16)
    page.insert_text((72, 96), "Operating modes", fontsize=14)

    shape = page.new_shape()
    left, top = 72, 130
    col_widths = [100, 90, 260]
    row_heights = [28, 28, 28, 28]
    xs = [left]
    for width in col_widths:
        xs.append(xs[-1] + width)
    ys = [top]
    for height in row_heights:
        ys.append(ys[-1] + height)
    for x in xs:
        shape.draw_line((x, ys[0]), (x, ys[-1]))
    for y in ys:
        shape.draw_line((xs[0], y), (xs[-1], y))
    shape.finish(width=1, color=(0, 0, 0))
    shape.commit()

    rows = [
        ["Mode", "EN", "Description"],
        ["Normal", "High", "Outputs follow the input state"],
        ["Three-state", "Low", "Outputs enter a high-impedance state"],
        ["Partial-power-down", "Floating", "I/Os are isolated during VCC loss"],
    ]
    for row_index, row in enumerate(rows):
        for column_index, cell in enumerate(row):
            page.insert_textbox(
                (xs[column_index] + 4, ys[row_index] + 4, xs[column_index + 1] - 4, ys[row_index + 1] - 4),
                cell,
                fontsize=10,
            )

    page_two = document.new_page(width=595, height=842)
    page_two.insert_text((72, 72), "Mechanical data", fontsize=14)
    page_two.insert_text((72, 96), "Package dimensions are shown in millimeters.", fontsize=11)
    document.set_metadata({"title": "Logic Modes Datasheet", "author": "Codex"})
    document.save(pdf_path)
    document.close()


def _create_qspi_pin_pdf(pdf_path: Path) -> None:
    document = fitz.open()
    page_one = document.new_page(width=720, height=842)
    page_one.insert_text((72, 72), "10.1.3 Package pin assignments", fontsize=14)
    page_one.insert_text((72, 92), "Table 4: Pin assignments", fontsize=12)

    shape = page_one.new_shape()
    left, top = 72, 130
    col_widths = [55, 120, 160, 190]
    row_heights = [28, 28, 28, 28, 28, 28, 28]
    xs = [left]
    for width in col_widths:
        xs.append(xs[-1] + width)
    ys = [top]
    for height in row_heights:
        ys.append(ys[-1] + height)
    for x in xs:
        shape.draw_line((x, ys[0]), (x, ys[-1]))
    for y in ys:
        shape.draw_line((xs[0], y), (xs[-1], y))
    shape.finish(width=1, color=(0, 0, 0))
    shape.commit()

    rows = [
        ["Pin", "Name", "Function", "Description"],
        ["40", "P2.03", "QSPI D2", "Quad SPI data line 2"],
        ["41", "P2.04", "QSPI D1", "Quad SPI data line 1"],
        ["39", "P2.02", "QSPI D0", "Quad SPI data line 0"],
        ["42", "P2.05", "QSPI CSN", "Quad SPI chip select"],
        ["37", "P2.00", "QSPI D3", "Quad SPI data line 3"],
        ["38", "P2.01", "QSPI SCK", "Quad SPI serial clock"],
    ]
    for row_index, row in enumerate(rows):
        for column_index, cell in enumerate(row):
            page_one.insert_textbox(
                (xs[column_index] + 4, ys[row_index] + 4, xs[column_index + 1] - 4, ys[row_index + 1] - 4),
                cell,
                fontsize=10,
            )

    page_two = document.new_page(width=595, height=842)
    page_two.insert_text(
        (72, 72),
        (
            "QSPI note\n\n"
            "It is also possible that the event is not generated, or not generated before the ADDRESS event."
        ),
        fontsize=11,
    )

    document.set_metadata({"title": "QSPI Pin Datasheet", "author": "Codex"})
    document.save(pdf_path)
    document.close()


def _create_crystal_pin_pdf(pdf_path: Path) -> None:
    document = fitz.open()
    page = document.new_page(width=720, height=842)
    page.insert_text((72, 72), "10.1.3 Package pin assignments", fontsize=14)
    page.insert_text((72, 92), "Clock pins", fontsize=12)

    shape = page.new_shape()
    left, top = 72, 130
    col_widths = [55, 110, 170, 220]
    row_heights = [28, 28, 28, 28, 28]
    xs = [left]
    for width in col_widths:
        xs.append(xs[-1] + width)
    ys = [top]
    for height in row_heights:
        ys.append(ys[-1] + height)
    for x in xs:
        shape.draw_line((x, ys[0]), (x, ys[-1]))
    for y in ys:
        shape.draw_line((xs[0], y), (xs[-1], y))
    shape.finish(width=1, color=(0, 0, 0))
    shape.commit()

    rows = [
        ["Pin", "Name", "Function", "Description"],
        ["10", "P1.00", "XL1 / LFXO", "Connection for 32.768 kHz crystal input"],
        ["11", "P1.01", "XL2 / LFXO", "Connection for 32.768 kHz crystal output"],
        ["12", "P0.00", "XC1 / HFXO", "Connection for high frequency crystal input"],
        ["13", "P0.01", "XC2 / HFXO", "Connection for high frequency crystal output"],
    ]
    for row_index, row in enumerate(rows):
        for column_index, cell in enumerate(row):
            page.insert_textbox(
                (xs[column_index] + 4, ys[row_index] + 4, xs[column_index + 1] - 4, ys[row_index + 1] - 4),
                cell,
                fontsize=10,
            )

    page_two = document.new_page(width=595, height=842)
    page_two.insert_text((72, 72), "Product overview", fontsize=14)
    page_two.insert_text((72, 96), "Table 7: Package variants", fontsize=12)
    shape = page_two.new_shape()
    left, top = 72, 130
    col_widths = [150, 90, 90, 90]
    row_heights = [28, 28, 28]
    xs = [left]
    for width in col_widths:
        xs.append(xs[-1] + width)
    ys = [top]
    for height in row_heights:
        ys.append(ys[-1] + height)
    for x in xs:
        shape.draw_line((x, ys[0]), (x, ys[-1]))
    for y in ys:
        shape.draw_line((xs[0], y), (xs[-1], y))
    shape.finish(width=1, color=(0, 0, 0))
    shape.commit()
    rows = [
        ["Feature", "Package A", "Package B", "Package C"],
        ["Pins", "24", "31", "35"],
        ["Wakeup-pins", "15", "20", "24"],
    ]
    for row_index, row in enumerate(rows):
        for column_index, cell in enumerate(row):
            page_two.insert_textbox(
                (xs[column_index] + 4, ys[row_index] + 4, xs[column_index + 1] - 4, ys[row_index + 1] - 4),
                cell,
                fontsize=10,
            )

    document.set_metadata({"title": "Crystal Pin Datasheet", "author": "Codex"})
    document.save(pdf_path)
    document.close()


def _parse_cli_kv(output: str) -> dict[str, str]:
    values: dict[str, str] = {}
    for line in output.splitlines():
        if ": " not in line:
            continue
        key, value = line.split(": ", 1)
        values[key.strip()] = value.strip()
    return values


def test_top_level_help_lists_commands() -> None:
    result = runner.invoke(app, ["--help"])

    assert result.exit_code == 0
    assert "ingest" in result.stdout
    assert "inspect" in result.stdout
    assert "search" in result.stdout
    assert "evidence" in result.stdout
    assert "answer" in result.stdout
    assert "eval" in result.stdout
    assert "query" not in result.stdout


def test_ingest_creates_db_and_debug_artifacts(tmp_path: Path) -> None:
    pdf_path = tmp_path / "sample.pdf"
    db_path = tmp_path / "datasheets.db"
    out_dir = tmp_path / "out"
    _create_sample_pdf(pdf_path)

    result = runner.invoke(
        app,
        ["ingest", str(pdf_path), "--db", str(db_path), "--out", str(out_dir)],
    )

    assert result.exit_code == 0
    values = _parse_cli_kv(result.stdout)
    document_id = values["document_id"]

    assert values["page_count"] == "2"
    assert int(values["chunk_count"]) >= 2
    assert values["table_count"] == "0"
    assert values["table_row_count"] == "0"
    assert values["table_candidate_count"] == "0"
    assert int(values["entity_count"]) >= 1
    assert values["reingested"] == "no"
    assert values["skipped"] == "no"
    assert db_path.exists()

    artifact_dir = out_dir / document_id
    pages_path = artifact_dir / "pages.jsonl"
    chunks_path = artifact_dir / "chunks.jsonl"
    summary_path = artifact_dir / "document_summary.json"

    assert pages_path.exists()
    assert chunks_path.exists()
    assert summary_path.exists()

    page_records = [
        json.loads(line)
        for line in pages_path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    assert [record["page_number"] for record in page_records] == [1, 2]
    assert "P1.04" in page_records[1]["text"]

    chunk_records = [
        json.loads(line)
        for line in chunks_path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    assert all(record["chunk_type"] == "text_chunk" for record in chunk_records)
    assert any("P1.04" in record["source_text"] for record in chunk_records)

    summary = json.loads(summary_path.read_text(encoding="utf-8"))
    assert summary["document_id"] == document_id
    assert summary["page_count"] == 2
    assert summary["chunk_count"] == len(chunk_records)
    assert summary["entity_count"] >= 1
    assert summary["metadata"]["title"] == "Sample Datasheet"

    documents = inspect_documents(db_path)
    assert len(documents) == 1
    assert documents[0].page_count == 2
    assert documents[0].chunk_count == len(chunk_records)
    assert documents[0].entity_count >= 1
    assert documents[0].pages[1].page_number == 2


def test_reingest_same_pdf_is_a_true_noop_when_artifacts_exist(tmp_path: Path) -> None:
    pdf_path = tmp_path / "sample.pdf"
    db_path = tmp_path / "datasheets.db"
    out_dir = tmp_path / "out"
    _create_sample_pdf(pdf_path)

    first = runner.invoke(
        app,
        ["ingest", str(pdf_path), "--db", str(db_path), "--out", str(out_dir)],
    )
    second = runner.invoke(
        app,
        ["ingest", str(pdf_path), "--db", str(db_path), "--out", str(out_dir)],
    )

    assert first.exit_code == 0
    assert second.exit_code == 0
    second_values = _parse_cli_kv(second.stdout)
    assert second_values["reingested"] == "no"
    assert second_values["skipped"] == "yes"

    documents = inspect_documents(db_path)
    assert len(documents) == 1
    assert len(documents[0].pages) == 2
    assert documents[0].pages[0].text.startswith("Nordic nRF54L15")


def test_force_reingest_reprocesses_existing_pdf(tmp_path: Path) -> None:
    pdf_path = tmp_path / "sample.pdf"
    db_path = tmp_path / "datasheets.db"
    out_dir = tmp_path / "out"
    _create_sample_pdf(pdf_path)

    runner.invoke(
        app,
        ["ingest", str(pdf_path), "--db", str(db_path), "--out", str(out_dir)],
    )
    forced = runner.invoke(
        app,
        ["ingest", str(pdf_path), "--db", str(db_path), "--out", str(out_dir), "--force"],
    )

    assert forced.exit_code == 0
    forced_values = _parse_cli_kv(forced.stdout)
    assert forced_values["reingested"] == "yes"
    assert forced_values["skipped"] == "no"


def test_inspect_shows_document_metadata_and_page_count(tmp_path: Path) -> None:
    pdf_path = tmp_path / "sample.pdf"
    db_path = tmp_path / "datasheets.db"
    out_dir = tmp_path / "out"
    _create_sample_pdf(pdf_path)

    ingest_result = runner.invoke(
        app,
        ["ingest", str(pdf_path), "--db", str(db_path), "--out", str(out_dir)],
    )
    document_id = _parse_cli_kv(ingest_result.stdout)["document_id"]

    result = runner.invoke(
        app,
        ["inspect", "--db", str(db_path), "--doc", document_id],
    )

    assert result.exit_code == 0
    assert f"document_id: {document_id}" in result.stdout
    assert "page_count: 2" in result.stdout
    assert "chunk_count:" in result.stdout
    assert "table_count:" in result.stdout
    assert "table_candidate_count:" in result.stdout
    assert "entity_count:" in result.stdout
    assert "title: Sample Datasheet" in result.stdout
    assert "page 2" in result.stdout


def test_search_returns_chunk_results_with_metadata(tmp_path: Path) -> None:
    pdf_path = tmp_path / "sample.pdf"
    db_path = tmp_path / "datasheets.db"
    out_dir = tmp_path / "out"
    _create_sample_pdf(pdf_path)

    runner.invoke(
        app,
        ["ingest", str(pdf_path), "--db", str(db_path), "--out", str(out_dir)],
    )
    result = runner.invoke(
        app,
        ["search", "P1.04 UARTE", "--db", str(db_path), "--limit", "3"],
    )

    assert result.exit_code == 0
    assert "document_id:" in result.stdout
    assert "page_number: 2" in result.stdout
    assert "chunk_type: text_chunk" in result.stdout
    assert "P1.04 can be configured for GPIO and UARTE." in result.stdout


def test_ingest_writes_table_artifacts_and_counts(tmp_path: Path) -> None:
    pdf_path = tmp_path / "table_sample.pdf"
    db_path = tmp_path / "datasheets.db"
    out_dir = tmp_path / "out"
    _create_table_pdf(pdf_path)

    result = runner.invoke(
        app,
        ["ingest", str(pdf_path), "--db", str(db_path), "--out", str(out_dir)],
    )

    assert result.exit_code == 0
    values = _parse_cli_kv(result.stdout)
    document_id = values["document_id"]
    assert values["table_count"] == "1"
    assert values["table_row_count"] == "2"
    assert values["table_candidate_count"] == "1"
    assert values["skipped"] == "no"

    artifact_dir = out_dir / document_id
    tables_path = artifact_dir / "tables.jsonl"
    table_rows_path = artifact_dir / "table_rows.jsonl"
    assert tables_path.exists()
    assert table_rows_path.exists()

    tables = [
        json.loads(line)
        for line in tables_path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    table_rows = [
        json.loads(line)
        for line in table_rows_path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]

    assert tables[0]["headers"] == ["Pin", "Name", "Function", "Description"]
    assert table_rows[0]["chunk_type"] == "table_row"
    assert table_rows[0]["cells"][1] == "P1.04"
    assert "Function: GPIO / UARTE TXD." in table_rows[0]["text_rendering"]

    documents = inspect_documents(db_path)
    assert documents[0].table_count == 1
    assert documents[0].table_row_count == 2
    assert documents[0].table_candidate_count == 1


def test_visual_first_table_pipeline_preserves_structure_and_metadata(tmp_path: Path) -> None:
    pdf_path = tmp_path / "visual_tables.pdf"
    db_path = tmp_path / "datasheets.db"
    out_dir = tmp_path / "out"
    _create_visual_tables_pdf(pdf_path)

    result = runner.invoke(
        app,
        ["ingest", str(pdf_path), "--db", str(db_path), "--out", str(out_dir)],
    )

    assert result.exit_code == 0
    values = _parse_cli_kv(result.stdout)
    document_id = values["document_id"]
    assert values["table_count"] == "2"
    assert values["table_candidate_count"] == "2"

    artifact_dir = out_dir / document_id
    tables = [
        json.loads(line)
        for line in (artifact_dir / "tables.jsonl").read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    table_rows = [
        json.loads(line)
        for line in (artifact_dir / "table_rows.jsonl").read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]

    assert tables[0]["section_title"] == "Power consumption highlights"
    assert tables[0]["table_title"] is None
    assert tables[0]["detection_source"] == "pymupdf_find_tables"
    assert tables[0]["parser_mode"] in {"text_first", "visual_fallback"}
    if tables[0]["crop_path"]:
        assert Path(tables[0]["crop_path"]).exists()

    first_table_rows = [row for row in table_rows if row["table_index"] == 0]
    group_rows = [row for row in first_table_rows if row["row_type"] == "group_header"]
    assert [row["cells"][0] for row in group_rows] == [
        "Active with radio",
        "Active with processing",
        "Sleep",
    ]
    assert any(row["cells"] == ["Bluetooth LE TX 1 Mbps at +4 dBm", "6.6 mA"] for row in first_table_rows)
    assert any(row["cells"] == ["Bluetooth LE TX 1 Mbps at +8 dBm", "9.8 mA"] for row in first_table_rows)

    second_table_rows = [row for row in table_rows if row["table_index"] == 1]
    assert second_table_rows[0]["section_title"] == "Product variants"
    assert second_table_rows[0]["cells"] == ["nRF54L15", "1524 KB", "256 KB"]


def test_register_text_first_pipeline_stores_field_rows(tmp_path: Path) -> None:
    pdf_path = tmp_path / "register_tables.pdf"
    db_path = tmp_path / "datasheets.db"
    out_dir = tmp_path / "out"
    _create_register_tables_pdf(pdf_path)

    result = runner.invoke(
        app,
        ["ingest", str(pdf_path), "--db", str(db_path), "--out", str(out_dir)],
    )

    assert result.exit_code == 0
    values = _parse_cli_kv(result.stdout)
    document_id = values["document_id"]
    assert values["table_count"] == "2"
    assert values["table_row_count"] == "6"
    assert values["table_candidate_count"] == "2"

    artifact_dir = out_dir / document_id
    tables = [
        json.loads(line)
        for line in (artifact_dir / "tables.jsonl").read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    table_rows = [
        json.loads(line)
        for line in (artifact_dir / "table_rows.jsonl").read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]

    assert all(table["table_kind"] == "register_table" for table in tables)
    assert all(table["parser_mode"] == "text_first" for table in tables)
    assert tables[0]["section_title"] == "5.4.3.11 SUBSCRIBE_XOSTOP"
    assert tables[0]["table_title"] == "Address offset: 0x084"
    assert any(row["cells"] == ["A", "RW", "CHIDX", None, "[0..255]", "DPPI channel that task XOSTOP will subscribe to"] for row in table_rows)
    assert any(row["row_type"] == "value_row" and row["cells"] == ["B", "RW", "EN", "Disabled", "0", "Disable subscription"] for row in table_rows)
    assert any("SUBSCRIBE_XOSTOP" in row["text_rendering"] for row in table_rows)


def test_multiline_pin_table_is_searchable_as_table_rows(tmp_path: Path) -> None:
    pdf_path = tmp_path / "pin_tables.pdf"
    db_path = tmp_path / "datasheets.db"
    out_dir = tmp_path / "out"
    _create_multiline_pin_pdf(pdf_path)

    ingest_result = runner.invoke(
        app,
        ["ingest", str(pdf_path), "--db", str(db_path), "--out", str(out_dir)],
    )
    assert ingest_result.exit_code == 0
    values = _parse_cli_kv(ingest_result.stdout)
    document_id = values["document_id"]
    assert values["table_count"] == "1"

    artifact_dir = out_dir / document_id
    table_rows = [
        json.loads(line)
        for line in (artifact_dir / "table_rows.jsonl").read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    assert table_rows[0]["cells"] == [
        "5",
        "Yes",
        "P1.04 ASO[0] AIN0",
        "Digital I/O Digital I/O Analog input",
        "General purpose I/O TAMPC active shield 0 output Analog input",
        "TAMPC",
    ]

    result = runner.invoke(
        app,
        ["search", "P1.04", "--db", str(db_path), "--limit", "3"],
    )
    assert result.exit_code == 0
    first_result_block = result.stdout.split("2. document_id:", 1)[0]
    assert "chunk_type: table_row" in first_result_block
    assert "P1.04 ASO[0] AIN0" in first_result_block
    assert "TAMPC active shield 0 output" in first_result_block


def test_search_prioritizes_table_row_for_exact_pin_lookup(tmp_path: Path) -> None:
    pdf_path = tmp_path / "table_sample.pdf"
    db_path = tmp_path / "datasheets.db"
    out_dir = tmp_path / "out"
    _create_table_pdf(pdf_path)

    runner.invoke(
        app,
        ["ingest", str(pdf_path), "--db", str(db_path), "--out", str(out_dir)],
    )
    result = runner.invoke(
        app,
        ["search", "P1.04", "--db", str(db_path), "--limit", "3"],
    )

    assert result.exit_code == 0
    first_result_block = result.stdout.split("2. document_id:", 1)[0]
    assert "chunk_type: table_row" in first_result_block
    assert "table_index: 0" in first_result_block
    assert "row_index: 0" in first_result_block
    assert "row_type: data_row" in first_result_block
    assert '"P1.04"' in first_result_block
    assert "GPIO / UARTE TXD" in first_result_block


def test_search_prioritizes_exact_heading_phrase(tmp_path: Path) -> None:
    pdf_path = tmp_path / "sample.pdf"
    db_path = tmp_path / "datasheets.db"
    out_dir = tmp_path / "out"
    _create_sample_pdf(pdf_path)

    runner.invoke(
        app,
        ["ingest", str(pdf_path), "--db", str(db_path), "--out", str(out_dir)],
    )
    result = runner.invoke(
        app,
        ["search", "Pin configuration", "--db", str(db_path), "--limit", "3"],
    )

    assert result.exit_code == 0
    first_result_block = result.stdout.split("2. document_id:", 1)[0]
    assert "page_number: 1" in first_result_block
    assert "8.8.1 Pin configuration" in first_result_block


def test_search_handles_simple_plural_variant(tmp_path: Path) -> None:
    pdf_path = tmp_path / "sample.pdf"
    db_path = tmp_path / "datasheets.db"
    out_dir = tmp_path / "out"
    _create_sample_pdf(pdf_path)

    runner.invoke(
        app,
        ["ingest", str(pdf_path), "--db", str(db_path), "--out", str(out_dir)],
    )
    result = runner.invoke(
        app,
        ["search", "Pin configurations", "--db", str(db_path), "--limit", "3"],
    )

    assert result.exit_code == 0
    assert "8.8.1 Pin configuration" in result.stdout


def test_search_help_describes_query_argument() -> None:
    result = runner.invoke(app, ["search", "--help"])

    assert result.exit_code == 0
    assert "Search query for lexical retrieval across prose" in result.stdout
    assert "chunks and table rows." in result.stdout


def test_ingest_writes_entity_artifacts_for_pin_tables(tmp_path: Path) -> None:
    pdf_path = tmp_path / "pin_tables.pdf"
    db_path = tmp_path / "datasheets.db"
    out_dir = tmp_path / "out"
    _create_multiline_pin_pdf(pdf_path)

    result = runner.invoke(
        app,
        ["ingest", str(pdf_path), "--db", str(db_path), "--out", str(out_dir)],
    )

    assert result.exit_code == 0
    values = _parse_cli_kv(result.stdout)
    document_id = values["document_id"]
    assert int(values["entity_count"]) >= 4
    assert int(values["entity_relation_count"]) >= 3

    artifact_dir = out_dir / document_id
    entities = [
        json.loads(line)
        for line in (artifact_dir / "entities.jsonl").read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    relations = [
        json.loads(line)
        for line in (artifact_dir / "entity_relations.jsonl").read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]

    assert any(entity["entity_family"] == "signal_or_terminal" and entity["display_text"] == "P1.04" for entity in entities)
    assert any("AIN0" in entity["aliases"] for entity in entities if entity["display_text"] == "P1.04")
    assert any(
        relation["relation_type"] == "has_terminal"
        and relation["target_entity_key"] == "signal_or_terminal:p104"
        for relation in relations
    )


def test_entity_aware_search_surfaces_non_mcu_variant_rows(tmp_path: Path) -> None:
    pdf_path = tmp_path / "regulator.pdf"
    db_path = tmp_path / "datasheets.db"
    out_dir = tmp_path / "out"
    _create_regulator_pdf(pdf_path)

    ingest_result = runner.invoke(
        app,
        ["ingest", str(pdf_path), "--db", str(db_path), "--out", str(out_dir)],
    )
    assert ingest_result.exit_code == 0

    result = runner.invoke(
        app,
        ["search", "XR1234B package", "--db", str(db_path), "--limit", "3"],
    )
    assert result.exit_code == 0
    first_result_block = result.stdout.split("2. document_id:", 1)[0]
    assert "chunk_type: table_row" in first_result_block
    assert "entity_family: component" in first_result_block
    assert "XR1234B" in first_result_block
    assert "DFN-6" in first_result_block


def test_evidence_returns_structured_evidence_pack_for_exact_pin_lookup(tmp_path: Path) -> None:
    pdf_path = tmp_path / "table_sample.pdf"
    db_path = tmp_path / "datasheets.db"
    out_dir = tmp_path / "out"
    _create_table_pdf(pdf_path)

    runner.invoke(
        app,
        ["ingest", str(pdf_path), "--db", str(db_path), "--out", str(out_dir)],
    )
    result = runner.invoke(
        app,
        ["evidence", "What function does P1.04 provide?", "--db", str(db_path)],
    )

    assert result.exit_code == 0
    assert "question: What function does P1.04 provide?" in result.stdout
    assert "planner_mode: local_fallback" in result.stdout
    assert "rerank_mode: local_fallback" in result.stdout
    assert "intent: terminal_lookup" in result.stdout
    assert 'identifier_terms: ["P1.04"]' in result.stdout
    assert 'preferred_evidence_families: ["terminal_mapping"]' in result.stdout
    assert "structured_evidence_groups:" in result.stdout
    assert "prose_evidence_groups:" in result.stdout
    assert "evidence_family: terminal_mapping" in result.stdout
    assert "entity_family: signal_or_terminal" in result.stdout
    assert "GPIO / UARTE TXD" in result.stdout
    assert "answer:" not in result.stdout
    assert "weak_evidence:" not in result.stdout


def test_evidence_feature_to_terminal_prefers_pin_table_evidence(tmp_path: Path) -> None:
    pdf_path = tmp_path / "qspi_pins.pdf"
    db_path = tmp_path / "datasheets.db"
    out_dir = tmp_path / "out"
    _create_qspi_pin_pdf(pdf_path)

    runner.invoke(
        app,
        ["ingest", str(pdf_path), "--db", str(db_path), "--out", str(out_dir)],
    )
    result = runner.invoke(
        app,
        ["evidence", "What are the canonical pins for QSPI?", "--db", str(db_path)],
    )

    assert result.exit_code == 0
    assert "planner_mode: local_fallback" in result.stdout
    assert "rerank_mode: local_fallback" in result.stdout
    assert "intent: feature_to_terminal" in result.stdout
    assert "evidence_family: terminal_mapping" in result.stdout
    assert "QSPI D0" in result.stdout
    assert "QSPI D1" in result.stdout
    assert "QSPI D2" in result.stdout
    assert "QSPI D3" in result.stdout
    assert "QSPI CSN" in result.stdout
    assert "QSPI SCK" in result.stdout
    assert "structured_evidence_groups:" in result.stdout
    assert "prose_evidence_groups:" in result.stdout


def test_evidence_limit_flag_controls_evidence_group_count(monkeypatch) -> None:
    captured: dict[str, int] = {}

    def fake_answer_query(*, db_path: Path, question: str, limit: int) -> QueryResponse:
        del db_path, question
        captured["limit"] = limit
        return QueryResponse(
            question="What are the canonical pins for QSPI?",
            intent="feature_to_terminal",
            planner_mode="local_fallback",
            rerank_mode="local_fallback",
            primary_subject="QSPI pins",
            must_include_terms=["qspi"],
            should_include_terms=[],
            identifier_terms=[],
            table_family_preferences=["pin_table"],
            preferred_evidence_families=["terminal_mapping"],
            subquestions=[],
            section_hints=[],
            negative_terms=[],
            retrieval_summary="summary",
            candidate_family_summary={"terminal_mapping": 1},
            structured_evidence_groups=[],
            prose_evidence_groups=[],
            coverage_notes=[],
        )

    monkeypatch.setattr(cli_module, "answer_query", fake_answer_query)
    result = runner.invoke(
        app,
        ["evidence", "What are the canonical pins for QSPI?", "--limit", "4"],
    )

    assert result.exit_code == 0
    assert captured["limit"] == 4


def test_evidence_command_can_save_rendered_package_to_output_file(monkeypatch, tmp_path: Path) -> None:
    output_path = tmp_path / "evidence-pack.txt"

    def fake_answer_query(*, db_path: Path, question: str, limit: int) -> QueryResponse:
        del db_path, limit
        return QueryResponse(
            question=question,
            intent="feature_to_terminal",
            planner_mode="local_fallback",
            rerank_mode="local_fallback",
            primary_subject="QSPI pins",
            must_include_terms=["qspi"],
            should_include_terms=[],
            identifier_terms=["QSPI"],
            table_family_preferences=["pin_table"],
            preferred_evidence_families=["terminal_mapping"],
            subquestions=[],
            section_hints=["pin assignments"],
            negative_terms=[],
            retrieval_summary="summary",
            candidate_family_summary={"terminal_mapping": 1},
            structured_evidence_groups=[
                {
                    "page_number": 864,
                    "table_index": 0,
                    "section_title": "Hardware and layout",
                    "table_title": "Pin assignments",
                    "evidence_family": "terminal_mapping",
                    "quality_score": 0.82,
                    "summary": "terminal mapping",
                    "group_score": 320.0,
                    "items": [
                        {
                            "chunk_type": "table_row",
                            "chunk_index": 0,
                            "row_index": 3,
                            "row_type": "data_row",
                            "entity_family": "signal_or_terminal",
                            "entity_display_text": "P2.01",
                            "headers": ["Pin", "Name", "Function", "Description"],
                            "cells": ["38", "P2.01", "QSPI SCK", "Quad SPI serial clock"],
                            "score": 180.0,
                            "text": "Pin: 38. Name: P2.01. Function: QSPI SCK.",
                        }
                    ],
                }
            ],
            prose_evidence_groups=[],
            coverage_notes=[],
        )

    monkeypatch.setattr(cli_module, "answer_query", fake_answer_query)
    result = runner.invoke(
        app,
        ["evidence", "What are the canonical pins for QSPI?", "--output", str(output_path)],
    )

    assert result.exit_code == 0
    assert f"saved_output: {output_path}" in result.stdout
    saved_text = output_path.read_text(encoding="utf-8")
    assert "question: What are the canonical pins for QSPI?" in saved_text
    assert "structured_evidence_groups:" in saved_text
    assert "QSPI SCK" in saved_text


def test_evidence_crystal_pins_uses_clock_planner_terms_and_surfaces_pin_rows(tmp_path: Path) -> None:
    pdf_path = tmp_path / "crystal_pins.pdf"
    db_path = tmp_path / "datasheets.db"
    out_dir = tmp_path / "out"
    _create_crystal_pin_pdf(pdf_path)

    runner.invoke(
        app,
        ["ingest", str(pdf_path), "--db", str(db_path), "--out", str(out_dir)],
    )
    result = runner.invoke(
        app,
        ["evidence", "which pins are meant for the LF and HF crystals?", "--db", str(db_path)],
    )

    assert result.exit_code == 0
    assert "planner_mode: local_fallback" in result.stdout
    assert "rerank_mode: local_fallback" in result.stdout
    assert "intent: feature_to_terminal" in result.stdout
    assert '"lf crystal"' in result.stdout
    assert '"hf crystal"' in result.stdout
    assert '"XL1"' in result.stdout
    assert '"XC2"' in result.stdout
    assert "P1.00" in result.stdout
    assert "P1.01" in result.stdout
    assert "P0.00" in result.stdout
    assert "P0.01" in result.stdout
    assert "Clock pins" in result.stdout
    assert "Package variants" not in result.stdout
    assert "evidence_family: terminal_mapping" in result.stdout
    assert "structured_evidence_groups:" in result.stdout
    assert "prose_evidence_groups:" in result.stdout


def test_evidence_crystal_pins_openai_plan_is_enriched_enough_to_surface_clean_rows(tmp_path: Path, monkeypatch) -> None:
    pdf_path = tmp_path / "crystal_pins.pdf"
    db_path = tmp_path / "datasheets.db"
    out_dir = tmp_path / "out"
    _create_crystal_pin_pdf(pdf_path)

    runner.invoke(
        app,
        ["ingest", str(pdf_path), "--db", str(db_path), "--out", str(out_dir)],
    )
    monkeypatch.setenv("OPENAI_API_KEY", "test-key")
    monkeypatch.setenv("DATASHEET_RAG_QUERY_MODEL", "gpt-test")

    def fake_openai_plan(*, question: str, api_key: str, model: str) -> dict[str, object]:
        del api_key, model
        assert question == "which pins are meant for the LF and HF crystals?"
        return {
            "intent": "terminal_lookup",
            "primary_subject": "LF and HF crystal pins",
            "must_include_terms": ["lf crystal", "hf crystal", "pins"],
            "should_include_terms": ["oscillator", "oscillator pins", "crystal pins", "clock pins", "xtal"],
            "identifier_terms": [],
            "table_family_preferences": ["pin_table", "generic_table"],
            "preferred_evidence_families": ["terminal_mapping"],
            "section_hints": ["pin assignments", "pin description", "clock source"],
            "negative_terms": ["ordering information", "package dimensions"],
            "evidence_goal": "find crystal pin rows",
            "subquestions": [],
        }

    def fake_openai_rerank(**kwargs) -> dict[str, object]:
        groups = kwargs["candidate_groups"]
        lf_groups = [
            group["group_id"]
            for group in groups
            if any(text for text in group["sample_texts"] if "XL1" in text or "XL2" in text)
        ]
        hf_groups = [
            group["group_id"]
            for group in groups
            if any(text for text in group["sample_texts"] if "XC1" in text or "XC2" in text)
        ]
        other_groups = [
            group["group_id"]
            for group in groups
            if group["group_id"] not in lf_groups and group["group_id"] not in hf_groups
        ]
        ranked = [*lf_groups, *other_groups, *hf_groups]
        return {
            "ranked_group_ids": ranked,
            "reason_codes": [
                {"group_id": group_id, "reason_code": "LF_CRYSTAL_PIN_MAPPING"}
                for group_id in lf_groups
            ] + [
                {"group_id": group_id, "reason_code": "HF_CRYSTAL_PIN_MAPPING"}
                for group_id in hf_groups
            ],
        }

    monkeypatch.setattr(query_planner, "_plan_query_via_openai", fake_openai_plan)
    monkeypatch.setattr(query_reranker, "_rerank_query_groups_via_openai", fake_openai_rerank)
    result = runner.invoke(
        app,
        ["evidence", "which pins are meant for the LF and HF crystals?", "--db", str(db_path)],
    )

    assert result.exit_code == 0
    assert "planner_mode: openai" in result.stdout
    assert "rerank_mode: openai" in result.stdout
    assert "intent: feature_to_terminal" in result.stdout
    assert "XL1 / LFXO" in result.stdout
    assert "XL2 / LFXO" in result.stdout
    assert "XC1 / HFXO" in result.stdout
    assert "XC2 / HFXO" in result.stdout
    assert "evidence_family: terminal_mapping" in result.stdout
    assert "structured_evidence_groups:" in result.stdout
    assert "prose_evidence_groups:" in result.stdout


def test_evidence_non_mcu_spec_prefers_electrical_spec_group(tmp_path: Path) -> None:
    pdf_path = tmp_path / "regulator.pdf"
    db_path = tmp_path / "datasheets.db"
    out_dir = tmp_path / "out"
    _create_regulator_pdf(pdf_path)

    runner.invoke(
        app,
        ["ingest", str(pdf_path), "--db", str(db_path), "--out", str(out_dir)],
    )
    result = runner.invoke(
        app,
        ["evidence", "What is the shutdown current?", "--db", str(db_path)],
    )

    assert result.exit_code == 0
    assert "intent: spec_lookup" in result.stdout
    assert 'preferred_evidence_families: ["electrical_spec", "timing_spec"]' in result.stdout
    assert "evidence_family: electrical_spec" in result.stdout
    assert "Shutdown current" in result.stdout
    assert "structured_evidence_groups:" in result.stdout
    assert "prose_evidence_groups:" in result.stdout


def test_evidence_non_mcu_feature_summary_prefers_modes_table(tmp_path: Path) -> None:
    pdf_path = tmp_path / "logic_modes.pdf"
    db_path = tmp_path / "datasheets.db"
    out_dir = tmp_path / "out"
    _create_logic_modes_pdf(pdf_path)

    runner.invoke(
        app,
        ["ingest", str(pdf_path), "--db", str(db_path), "--out", str(out_dir)],
    )
    result = runner.invoke(
        app,
        ["evidence", "What operating modes are available?", "--db", str(db_path)],
    )

    assert result.exit_code == 0
    assert "evidence_family: feature_summary" in result.stdout
    assert "Operating modes" in result.stdout
    assert "Three-state" in result.stdout
    assert "Mechanical data" not in result.stdout


def test_evidence_qspi_module_work_includes_both_structured_and_prose_packages(tmp_path: Path) -> None:
    pdf_path = tmp_path / "qspi_pins.pdf"
    db_path = tmp_path / "datasheets.db"
    out_dir = tmp_path / "out"
    _create_qspi_pin_pdf(pdf_path)

    runner.invoke(
        app,
        ["ingest", str(pdf_path), "--db", str(db_path), "--out", str(out_dir)],
    )
    result = runner.invoke(
        app,
        ["evidence", "How does the QSPI module work?", "--db", str(db_path)],
    )

    assert result.exit_code == 0
    assert "intent: generic" in result.stdout
    assert "structured_evidence_groups:" in result.stdout
    assert "prose_evidence_groups:" in result.stdout
    assert "QSPI" in result.stdout
    assert "It is also possible that the event is not generated" in result.stdout
    assert "evidence_family: terminal_mapping" in result.stdout
    assert "8.20 SPIS" not in result.stdout
    assert "11.18 SPIM Electrical specification" not in result.stdout


def test_eval_reports_hits_and_evidence_usefulness_metrics(tmp_path: Path) -> None:
    pdf_path = tmp_path / "regulator.pdf"
    db_path = tmp_path / "datasheets.db"
    out_dir = tmp_path / "out"
    eval_path = tmp_path / "eval.json"
    _create_regulator_pdf(pdf_path)

    runner.invoke(
        app,
        ["ingest", str(pdf_path), "--db", str(db_path), "--out", str(out_dir)],
    )
    eval_path.write_text(
        json.dumps(
            {
                "cases": [
                    {
                        "query": "XR1234B package",
                        "expected_page": 1,
                        "expected_entity": "XR1234B",
                        "expected_top_result_family": "component",
                        "expected_evidence_substring": ["XR1234B", "DFN-6"],
                        "expected_planner_terms": ["package", "ordering information"],
                    }
                ]
            },
            indent=2,
        ),
        encoding="utf-8",
    )

    result = runner.invoke(
        app,
        ["eval", str(eval_path), "--db", str(db_path)],
    )
    assert result.exit_code == 0
    report = json.loads(result.stdout)
    assert report["total_cases"] == 1
    assert report["hit_at_1"] == 1
    assert report["family_match_rate"] == 1.0
    assert report["evidence_match_rate"] == 1.0
    assert report["planner_trace_match_rate"] == 1.0


def test_query_alias_still_works_for_evidence(monkeypatch) -> None:
    captured: dict[str, str] = {}

    def fake_answer_query(*, db_path: Path, question: str, limit: int) -> QueryResponse:
        del db_path, limit
        captured["question"] = question
        return QueryResponse(
            question=question,
            intent="generic",
            planner_mode="local_fallback",
            rerank_mode="local_fallback",
            primary_subject="alias smoke test",
            must_include_terms=[],
            should_include_terms=[],
            identifier_terms=[],
            table_family_preferences=[],
            preferred_evidence_families=[],
            subquestions=[],
            section_hints=[],
            negative_terms=[],
            retrieval_summary="summary",
            candidate_family_summary={},
            structured_evidence_groups=[],
            prose_evidence_groups=[],
            coverage_notes=[],
        )

    monkeypatch.setattr(cli_module, "answer_query", fake_answer_query)
    result = runner.invoke(app, ["query", "alias question"])

    assert result.exit_code == 0
    assert captured["question"] == "alias question"
    assert "question: alias question" in result.stdout
    assert "structured_evidence_groups:" in result.stdout
    assert "prose_evidence_groups:" in result.stdout


def test_answer_command_emits_grounded_answer(monkeypatch) -> None:
    def fake_answer_query(*, db_path: Path, question: str, limit: int) -> QueryResponse:
        del db_path, limit
        return QueryResponse(
            question=question,
            intent="terminal_lookup",
            planner_mode="local_fallback",
            rerank_mode="local_fallback",
            primary_subject="P1.04",
            must_include_terms=["p1.04"],
            should_include_terms=[],
            identifier_terms=["P1.04"],
            table_family_preferences=["pin_table"],
            preferred_evidence_families=["terminal_mapping"],
            subquestions=[],
            section_hints=["pin assignments"],
            negative_terms=[],
            retrieval_summary="summary",
            candidate_family_summary={"terminal_mapping": 1},
            structured_evidence_groups=[
                {
                    "page_number": 2,
                    "table_index": 0,
                    "section_title": "Pin assignments",
                    "table_title": "Table 1: Pin assignments",
                    "evidence_family": "terminal_mapping",
                    "quality_score": 0.88,
                    "summary": "terminal mapping",
                    "group_score": 320.0,
                    "items": [
                        {
                            "chunk_type": "table_row",
                            "chunk_index": 0,
                            "row_index": 2,
                            "row_type": "data_row",
                            "entity_family": "signal_or_terminal",
                            "entity_display_text": "P1.04",
                            "headers": ["Pin", "Name", "Function", "Description"],
                            "cells": ["12", "P1.04", "GPIO / UARTE TXD", "GPIO or TXD"],
                            "text": "Pin: 12. Name: P1.04. Function: GPIO / UARTE TXD.",
                        }
                    ],
                }
            ],
            prose_evidence_groups=[],
            coverage_notes=[],
        )

    monkeypatch.setattr(cli_module, "answer_query", fake_answer_query)
    monkeypatch.setattr(
        cli_module,
        "generate_grounded_answer",
        lambda response: AnswerResult(
            question=response.question,
            answer="P1.04 provides GPIO / UARTE TXD.",
            evidence_summary="Pin-assignment table evidence on page 2.",
            sources=[
                AnswerCitation(
                    page_number=2,
                    section_title="Pin assignments",
                    table_title="Table 1: Pin assignments",
                    row_index=2,
                    chunk_type="table_row",
                    source_note="P1.04",
                )
            ],
            uncertainty=None,
            insufficient_evidence=False,
            provider_mode="openai",
        ),
    )
    result = runner.invoke(app, ["answer", "future answer question"])

    assert result.exit_code == 0
    assert "provider_mode: openai" in result.stdout
    assert "answer: P1.04 provides GPIO / UARTE TXD." in result.stdout
    assert "sources:" in result.stdout
    assert "row_index: 2" in result.stdout
    assert "question: future answer question" in result.stdout
    assert "evidence_context:" in result.stdout
    assert "structured_evidence_groups:" in result.stdout


def test_answer_command_can_save_rendered_package_to_output_file(monkeypatch, tmp_path: Path) -> None:
    output_path = tmp_path / "answer-pack.txt"

    def fake_answer_query(*, db_path: Path, question: str, limit: int) -> QueryResponse:
        del db_path, limit
        return QueryResponse(
            question=question,
            intent="terminal_lookup",
            planner_mode="local_fallback",
            rerank_mode="local_fallback",
            primary_subject="P1.04",
            must_include_terms=["p1.04"],
            should_include_terms=[],
            identifier_terms=["P1.04"],
            table_family_preferences=["pin_table"],
            preferred_evidence_families=["terminal_mapping"],
            subquestions=[],
            section_hints=["pin assignments"],
            negative_terms=[],
            retrieval_summary="summary",
            candidate_family_summary={"terminal_mapping": 1},
            structured_evidence_groups=[
                {
                    "page_number": 2,
                    "table_index": 0,
                    "section_title": "Pin assignments",
                    "table_title": "Table 1: Pin assignments",
                    "evidence_family": "terminal_mapping",
                    "quality_score": 0.88,
                    "summary": "terminal mapping",
                    "group_score": 320.0,
                    "items": [
                        {
                            "chunk_type": "table_row",
                            "chunk_index": 0,
                            "row_index": 2,
                            "row_type": "data_row",
                            "entity_family": "signal_or_terminal",
                            "entity_display_text": "P1.04",
                            "headers": ["Pin", "Name", "Function", "Description"],
                            "cells": ["12", "P1.04", "GPIO / UARTE TXD", "GPIO or TXD"],
                            "score": 180.0,
                            "text": "Pin: 12. Name: P1.04. Function: GPIO / UARTE TXD.",
                        }
                    ],
                }
            ],
            prose_evidence_groups=[],
            coverage_notes=[],
        )

    monkeypatch.setattr(cli_module, "answer_query", fake_answer_query)
    monkeypatch.setattr(
        cli_module,
        "generate_grounded_answer",
        lambda response: AnswerResult(
            question=response.question,
            answer="P1.04 provides GPIO / UARTE TXD.",
            evidence_summary="Pin-assignment evidence on page 2.",
            sources=[
                AnswerCitation(
                    page_number=2,
                    section_title="Pin assignments",
                    table_title="Table 1: Pin assignments",
                    row_index=2,
                    chunk_type="table_row",
                    source_note="P1.04",
                )
            ],
            uncertainty=None,
            insufficient_evidence=False,
            provider_mode="openai",
        ),
    )
    result = runner.invoke(
        app,
        ["answer", "What function does P1.04 provide?", "--output", str(output_path)],
    )

    assert result.exit_code == 0
    assert f"saved_output: {output_path}" in result.stdout
    saved_text = output_path.read_text(encoding="utf-8")
    assert "question: What function does P1.04 provide?" in saved_text
    assert "answer: P1.04 provides GPIO / UARTE TXD." in saved_text
    assert "sources:" in saved_text
    assert "evidence_context:" in saved_text
    assert "structured_evidence_groups:" in saved_text


def test_answer_command_returns_grounded_non_answer_for_insufficient_evidence(monkeypatch) -> None:
    def fake_answer_query(*, db_path: Path, question: str, limit: int) -> QueryResponse:
        del db_path, limit
        return QueryResponse(
            question=question,
            intent="generic",
            planner_mode="local_fallback",
            rerank_mode="local_fallback",
            primary_subject="unsupported lookup",
            must_include_terms=[],
            should_include_terms=[],
            identifier_terms=[],
            table_family_preferences=[],
            preferred_evidence_families=[],
            subquestions=[],
            section_hints=[],
            negative_terms=[],
            retrieval_summary="summary",
            candidate_family_summary={},
            structured_evidence_groups=[],
            prose_evidence_groups=[],
            coverage_notes=[],
        )

    monkeypatch.setattr(cli_module, "answer_query", fake_answer_query)
    monkeypatch.setattr(
        cli_module,
        "generate_grounded_answer",
        lambda response: AnswerResult(
            question=response.question,
            answer="I could not answer from the retrieved evidence alone. Evidence was insufficient: no evidence groups were retrieved",
            evidence_summary="No evidence groups were retrieved.",
            sources=[],
            uncertainty="no evidence groups were retrieved",
            insufficient_evidence=True,
            provider_mode="local_insufficient",
        ),
    )
    result = runner.invoke(app, ["answer", "What is the moon phase?"])

    assert result.exit_code == 0
    assert "insufficient_evidence: yes" in result.stdout
    assert "local_insufficient" in result.stdout
    assert "No evidence groups were retrieved." in result.stdout


def test_answer_command_errors_clearly_when_provider_is_not_configured(monkeypatch) -> None:
    def fake_answer_query(*, db_path: Path, question: str, limit: int) -> QueryResponse:
        del db_path, limit
        return QueryResponse(
            question=question,
            intent="terminal_lookup",
            planner_mode="local_fallback",
            rerank_mode="local_fallback",
            primary_subject="P1.04",
            must_include_terms=["p1.04"],
            should_include_terms=[],
            identifier_terms=["P1.04"],
            table_family_preferences=["pin_table"],
            preferred_evidence_families=["terminal_mapping"],
            subquestions=[],
            section_hints=["pin assignments"],
            negative_terms=[],
            retrieval_summary="summary",
            candidate_family_summary={"terminal_mapping": 1},
            structured_evidence_groups=[
                {
                    "page_number": 2,
                    "table_index": 0,
                    "section_title": "Pin assignments",
                    "table_title": "Table 1: Pin assignments",
                    "evidence_family": "terminal_mapping",
                    "quality_score": 0.88,
                    "summary": "terminal mapping",
                    "group_score": 320.0,
                    "items": [
                        {
                            "chunk_type": "table_row",
                            "chunk_index": 0,
                            "row_index": 2,
                            "row_type": "data_row",
                            "entity_family": "signal_or_terminal",
                            "entity_display_text": "P1.04",
                            "headers": ["Pin", "Name", "Function", "Description"],
                            "cells": ["12", "P1.04", "GPIO / UARTE TXD", "GPIO or TXD"],
                            "text": "Pin: 12. Name: P1.04. Function: GPIO / UARTE TXD.",
                        }
                    ],
                }
            ],
            prose_evidence_groups=[],
            coverage_notes=[],
        )

    monkeypatch.setattr(cli_module, "answer_query", fake_answer_query)

    def raise_config_error(response):
        del response
        raise cli_module.AnswerProviderConfigError("Answer provider not configured (DATASHEET_RAG_ANSWER_PROVIDER missing).")

    monkeypatch.setattr(cli_module, "generate_grounded_answer", raise_config_error)
    result = runner.invoke(app, ["answer", "What function does P1.04 provide?"])

    assert result.exit_code == 1
    assert "answer_error: Answer provider not configured" in result.stdout
    assert "evidence_context:" in result.stdout
    assert "structured_evidence_groups:" in result.stdout
