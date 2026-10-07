"""Per-run Markdown report, including healed-element diffs (old vs new locator
and fingerprint), per spec R4."""

from __future__ import annotations

from pathlib import Path

from framework.executor import RowExecutionResult
from framework.store.backend import ElementStore
from framework.store.models import ElementRecord


def _heal_diff_section(store: ElementStore, run_id: str) -> str:
    healed = [r for r in store.list_all() if r.run_id == run_id and r.source == "Healed"]
    if not healed:
        return "No heals occurred in this run.\n"

    lines = ["| Key | Version | Status | New Strategy | New Fingerprint |",
             "|---|---|---|---|---|"]
    for r in healed:
        lines.append(
            f"| {r.page}::{r.description} | v{r.version} | {r.status} | "
            f"{r.strategies[0].kind}={r.strategies[0].value} | "
            f"{r.fingerprint.tag}/{r.fingerprint.role}/{r.fingerprint.accessible_name!r} |"
        )
    return "\n".join(lines) + "\n"


def write_run_report(
    path: str | Path,
    *,
    run_id: str,
    suite: str,
    environment: str,
    row_results: list[RowExecutionResult],
    store: ElementStore,
) -> None:
    passed = sum(1 for r in row_results if r.status == "Pass")
    failed = sum(1 for r in row_results if r.status == "Fail")
    blocked = sum(1 for r in row_results if r.status == "Blocked")
    tokens = sum(r.tokens_used for r in row_results)

    lines = [
        f"# Run Report: {run_id}",
        "",
        f"- Suite: {suite}",
        f"- Environment: {environment}",
        f"- Total test cases: {len(row_results)}",
        f"- Passed: {passed}  Failed: {failed}  Blocked: {blocked}",
        f"- Total tokens used: {tokens}",
        "",
        "## Per-test-case results",
        "",
        "| TestCase ID | Status | Element Source | Tokens | Actual Result |",
        "|---|---|---|---|---|",
    ]
    for r in row_results:
        lines.append(
            f"| {r.test_case_id} | {r.status} | {r.element_source} | {r.tokens_used} | "
            f"{r.actual_result.replace('|', '/')[:300]} |"
        )

    lines += ["", "## Heals in this run", "", _heal_diff_section(store, run_id)]

    Path(path).write_text("\n".join(lines), encoding="utf-8")
