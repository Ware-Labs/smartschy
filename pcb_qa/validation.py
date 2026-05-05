from __future__ import annotations

from pathlib import Path

from .qa import answer_question
from .utils import write_json


VALIDATION_QUESTIONS = [
    {
        "id": "q1",
        "question": "did I connect the crystal correctly to the microcontroller?",
        "expected_focus": ["X1", "MOD1", "P2.00", "P2.01"],
    },
    {
        "id": "q2",
        "question": "which pins are on SWDIO and SWDCLK nets?",
        "expected_focus": ["SWDIO", "SWDCLK", "P3-2", "P3-4"],
    },
    {
        "id": "q3",
        "question": "what components are connected to 1V8_SW?",
        "expected_focus": ["1V8_SW", "U2", "SB8"],
    },
    {
        "id": "q4",
        "question": "is there evidence of pull resistors around reset?",
        "expected_focus": ["R\\E\\S\\E\\T\\", "R10", "SW3"],
    },
    {
        "id": "q5",
        "question": "how is the USB connector tied to power and switching?",
        "expected_focus": ["J4", "VBUS", "SW1"],
    },
    {
        "id": "q6",
        "question": "which nets connect the imu signals to the module?",
        "expected_focus": ["U3", "IMU_SPI", "MOD1"],
    },
    {
        "id": "q7",
        "question": "which test points map to module GPIO nets?",
        "expected_focus": ["TP", "P1.", "P0."],
    },
    {
        "id": "q8",
        "question": "what evidence exists for i2c connections in this board?",
        "expected_focus": ["I2C", "SCL", "SDA"],
    },
]


def _contains_focus(summary: dict, expected_tokens: list[str]) -> bool:
    blob = " ".join(
        [
            " ".join(summary.get("entities", {}).get("refdes", [])),
            " ".join(summary.get("entities", {}).get("nets", [])),
            " ".join(summary.get("entities", {}).get("symbols", [])),
            " ".join(summary.get("entities", {}).get("roles", [])),
        ]
    ).upper()
    return any(token.upper().replace("\\", "") in blob.replace("\\", "") for token in expected_tokens)


def run_validation(project_root: Path) -> dict:
    runs = []
    for row in VALIDATION_QUESTIONS:
        summary = answer_question(project_root, row["question"], net_walk_depth=1, top_k=6)
        runs.append(
            {
                "id": row["id"],
                "question": row["question"],
                "expected_focus": row["expected_focus"],
                "heuristic_pass": _contains_focus(summary, row["expected_focus"]),
                "summary": summary,
            }
        )
    passed = sum(1 for row in runs if row["heuristic_pass"])
    report = {
        "total": len(runs),
        "passed_heuristic": passed,
        "failed_heuristic": len(runs) - passed,
        "runs": runs,
        "note": "Heuristic pass only; final adjudication should be manual with citations.",
    }
    output_dir = project_root / "derived" / "validation"
    output_dir.mkdir(parents=True, exist_ok=True)
    write_json(output_dir / "validation_report.json", report)
    return report
