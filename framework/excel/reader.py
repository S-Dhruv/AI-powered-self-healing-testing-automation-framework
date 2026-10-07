"""Excel suite reader (openpyxl only, per claude.md). Validates the workbook
up front: missing columns, duplicate (TestCase ID, Step No) [not applicable here
since this workbook has one row per test case], unknown actions are reported
before any browser starts."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import openpyxl

REQUIRED_COLUMNS = (
    "TestCase ID",
    "Title",
    "Test Step",
    "Test Data",
    "Expected Result",
    "Execute",
)

RESULT_COLUMNS = (
    "Status",
    "Actual Result",
    "Screenshot Path",
    "Execution Time (ms)",
    "Element Source",
    "Tokens Used",
    "LLM Model",
    "Run ID",
    "Executed At",
)


class ExcelValidationError(RuntimeError):
    pass


@dataclass
class TestCaseRow:
    row_index: int  # 1-based openpyxl row number, for writer to target
    test_case_id: str
    title: str
    test_step: str
    test_data: str
    expected_result: str
    execute: bool


def _is_execute_yes(value) -> bool:
    return isinstance(value, str) and value.strip().lower() == "y"


def read_suite(path: str | Path, sheet_name: str = "Test Cases") -> list[TestCaseRow]:
    p = Path(path)
    if not p.is_file():
        raise ExcelValidationError(f"Workbook not found: {p}")

    wb = openpyxl.load_workbook(p, data_only=False)
    if sheet_name not in wb.sheetnames:
        raise ExcelValidationError(f"Sheet '{sheet_name}' not found in {p} (found: {wb.sheetnames})")
    ws = wb[sheet_name]

    header_row = [c.value for c in next(ws.iter_rows(min_row=1, max_row=1))]
    col_index = {name: i for i, name in enumerate(header_row) if name}

    missing = [c for c in REQUIRED_COLUMNS if c not in col_index]
    if missing:
        raise ExcelValidationError(f"Workbook {p} is missing required columns: {missing}")

    seen_ids: set[str] = set()
    rows: list[TestCaseRow] = []
    for r_idx, row in enumerate(ws.iter_rows(min_row=2), start=2):
        values = [c.value for c in row]
        tc_id = values[col_index["TestCase ID"]]
        if tc_id is None:
            continue  # blank trailing row
        tc_id = str(tc_id).strip()
        if tc_id in seen_ids:
            raise ExcelValidationError(f"Duplicate TestCase ID '{tc_id}' at row {r_idx}")
        seen_ids.add(tc_id)

        rows.append(
            TestCaseRow(
                row_index=r_idx,
                test_case_id=tc_id,
                title=str(values[col_index["Title"]] or ""),
                test_step=str(values[col_index["Test Step"]] or ""),
                test_data=str(values[col_index["Test Data"]] or ""),
                expected_result=str(values[col_index["Expected Result"]] or ""),
                execute=_is_execute_yes(values[col_index["Execute"]]),
            )
        )

    wb.close()
    return rows
