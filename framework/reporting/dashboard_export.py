"""Documents and emits the Assignment-2-dashboard-compatible usage log schema.

The usage log itself is written incrementally by framework/llm/usage_logger.py
as JSONL (one UsageLogEntry per LLM call). This module only re-exports that
same file as CSV on request and writes the documented schema description next
to it, so the dashboard can load either format.
"""

from __future__ import annotations

import csv
import json
from pathlib import Path

USAGE_LOG_SCHEMA = {
    "format": "JSONL, one JSON object per line (one line per LLM call, success or error)",
    "fields": {
        "timestamp": "ISO-8601 UTC timestamp of the call",
        "run_id": "Run identifier this call belongs to",
        "test_case_id": "Excel TestCase ID the call was made for",
        "step_no": "1-based sub-step number within that test case",
        "user_id": "Identifier of the user who started the run",
        "agent_name": "Internal component name that issued the call (e.g. ai_finder)",
        "reason": "'first_learning' or 'healing'",
        "model": "LLM model name used",
        "input_tokens": "Prompt tokens, from the API response usage field only",
        "output_tokens": "Completion tokens, from the API response usage field only",
        "latency_ms": "Wall-clock call latency in milliseconds",
        "status": "'success' or 'error'",
        "estimated_cost": "USD, computed from a documented price table; 0.0 if the model has no listed price",
    },
}


def export_schema(path: str | Path) -> None:
    Path(path).write_text(json.dumps(USAGE_LOG_SCHEMA, indent=2), encoding="utf-8")


def jsonl_to_csv(jsonl_path: str | Path, csv_path: str | Path) -> None:
    jsonl_path, csv_path = Path(jsonl_path), Path(csv_path)
    rows = []
    if jsonl_path.is_file():
        with jsonl_path.open(encoding="utf-8") as f:
            rows = [json.loads(line) for line in f if line.strip()]

    fieldnames = list(USAGE_LOG_SCHEMA["fields"].keys())
    with csv_path.open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        for row in rows:
            writer.writerow({k: row.get(k) for k in fieldnames})
