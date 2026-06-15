from __future__ import annotations

import json
from pathlib import Path

import fitz
from typer.testing import CliRunner

from datasheet_rag.cli import app
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
    assert "query" in result.stdout
    assert "eval" in result.stdout


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
    assert summary["metadata"]["title"] == "Sample Datasheet"

    documents = inspect_documents(db_path)
    assert len(documents) == 1
    assert documents[0].page_count == 2
    assert documents[0].chunk_count == len(chunk_records)
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
