"""CLI entry point: `shtaf run|store|review ...`"""

from __future__ import annotations

import argparse
import sys
from datetime import datetime, timezone
from pathlib import Path

from playwright.sync_api import sync_playwright

from framework.config.allowlist import AllowListGuard
from framework.config.loader import ConfigError, load_app_config, load_settings
from framework.excel.reader import ExcelValidationError, read_suite
from framework.excel.writer import RowResult, RunTotals, append_run_summary, write_results
from framework.executor import execute_test_case
from framework.llm.client import LlmClient
from framework.llm.usage_logger import UsageLogger
from framework.reporting.console import ConsoleNarrator
from framework.reporting.run_history import RunHistoryEntry, RunHistoryStore
from framework.reporting.run_report import write_run_report
from framework.store.backend import ElementStore
from framework.store.review import ReviewQueue


def _new_run_id() -> str:
    return "run-" + datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")


def cmd_run(args: argparse.Namespace) -> int:
    console = ConsoleNarrator(enabled=True)
    try:
        settings = load_settings(args.env_file)
        app_config = load_app_config(args.app_config)
    except ConfigError as exc:
        console.line(f"CONFIG ERROR: {exc}")
        return 2

    env_name = args.env or settings.framework_env
    if env_name not in app_config.environments:
        console.line(f"CONFIG ERROR: environment '{env_name}' not defined in {args.app_config}")
        return 2
    env_cfg = app_config.environments[env_name]
    guard = AllowListGuard(env_cfg.allowed_hosts)

    try:
        rows = read_suite(args.suite, sheet_name=args.sheet)
    except ExcelValidationError as exc:
        console.line(f"SUITE VALIDATION ERROR: {exc}")
        return 2

    run_id = _new_run_id()
    console.line(f"=== Starting run {run_id} | suite={args.suite} | app={app_config.app_name} | env={env_name} ===")

    store = ElementStore(settings.database_url.replace("sqlite:///", ""))
    review_queue = ReviewQueue(store)
    history_store = RunHistoryStore(settings.run_history_db.replace("sqlite:///", ""))
    usage_logger = UsageLogger(Path(settings.log_dir) / "llm_usage.jsonl")

    llm_client: LlmClient | None = None
    if settings.llm_provider == "openai" and settings.openai_api_key:
        llm_client = LlmClient(
            api_key=settings.openai_api_key, model=args.model, request_timeout=settings.llm_request_timeout
        )
    else:
        console.line("WARNING: no usable LLM provider configured; Tier 3 will be unavailable this run.")

    headless = settings.browser_headless
    started_iso = datetime.now(timezone.utc).isoformat()
    row_results = []

    with sync_playwright() as pw:
        browser = getattr(pw, settings.browser_engine).launch(headless=headless)
        page = browser.new_page()
        try:
            for row in rows:
                if not row.execute:
                    console.line(f"[{row.test_case_id}] Execute != Y -> Skipped")
                    row_results.append(_skipped_result(row.test_case_id))
                    continue

                result = execute_test_case(
                    row, page=page, store=store, review_queue=review_queue, llm_client=llm_client,
                    allow_guard=guard, application=app_config.app_name, environment=env_name,
                    page_name=args.page_name, base_url=env_cfg.base_url, run_id=run_id, user_id=args.user_id,
                    min_similarity=settings.fingerprint_cache_threshold,
                    min_confidence=settings.llm_min_confidence,
                    healing_auto_approve=settings.healing_auto_approve,
                    action_timeout_ms=settings.action_timeout_ms,
                    navigation_timeout_ms=settings.navigation_timeout_ms,
                    console=console, usage_logger=usage_logger, llm_trace=settings.llm_trace,
                )
                row_results.append(result)

                if result.status in ("Fail", "Blocked") and settings.stop_on_failure:
                    console.line(f"[{row.test_case_id}] stop_on_failure=true and status={result.status}; skipping remaining rows")
                    remaining = rows[rows.index(row) + 1 :]
                    for skipped_row in remaining:
                        row_results.append(_skipped_result(skipped_row.test_case_id))
                    break
        finally:
            browser.close()

    ended_iso = datetime.now(timezone.utc).isoformat()
    _write_outputs(
        args, run_id, app_config, env_name, row_results, store, history_store,
        started_iso, ended_iso, console, settings.log_dir,
    )

    store.close()
    history_store.close()

    any_bad = any(r.status in ("Fail", "Blocked") for r in row_results)
    return 1 if any_bad else 0


def _skipped_result(test_case_id: str):
    from framework.executor import RowExecutionResult

    return RowExecutionResult(
        test_case_id=test_case_id, status="Skipped", actual_result="Execute != Y",
        element_source="None", tokens_used=0, llm_model=None, execution_time_ms=0,
        executed_at=datetime.now(timezone.utc).isoformat(),
    )


def _log_llm_calls_for_row(usage_logger: UsageLogger, result) -> None:
    # Per-call entries are already logged inside ai_finder.try_ai_finder via the
    # resolver; this hook exists so cli.py can be extended to append summary-only
    # entries without duplicating the per-call log. Intentionally a no-op today.
    return None


def _sum_usage_for_run(log_dir: str, run_id: str) -> tuple[int, int, float]:
    """Reads the real per-call JSONL usage log and sums input/output tokens and
    estimated cost for this run_id. Never estimates -- only aggregates what the
    usage logger already recorded from real API response usage fields."""
    import json

    path = Path(log_dir) / "llm_usage.jsonl"
    input_tokens = output_tokens = 0
    cost = 0.0
    if not path.is_file():
        return 0, 0, 0.0
    with path.open(encoding="utf-8") as f:
        for line in f:
            if not line.strip():
                continue
            entry = json.loads(line)
            if entry.get("run_id") != run_id:
                continue
            input_tokens += entry.get("input_tokens", 0)
            output_tokens += entry.get("output_tokens", 0)
            cost += entry.get("estimated_cost", 0.0)
    return input_tokens, output_tokens, cost


def _write_outputs(args, run_id, app_config, env_name, row_results, store, history_store, started_iso, ended_iso, console, log_dir) -> None:
    console.excel_write(args.suite)
    excel_rows = [
        RowResult(
            row_index=_row_index_for(args.suite, args.sheet, r.test_case_id),
            status=r.status, actual_result=r.actual_result, screenshot_path="",
            execution_time_ms=r.execution_time_ms, element_source=r.element_source,
            tokens_used=r.tokens_used, llm_model=r.llm_model, run_id=run_id, executed_at=r.executed_at,
        )
        for r in row_results
    ]
    write_results(args.suite, args.sheet, excel_rows)

    total = len(row_results)
    passed = sum(1 for r in row_results if r.status == "Pass")
    failed = sum(1 for r in row_results if r.status == "Fail")
    blocked = sum(1 for r in row_results if r.status == "Blocked")
    skipped = sum(1 for r in row_results if r.status == "Skipped")
    cache_hits = sum(1 for r in row_results if r.element_source == "Cache")
    fallback_hits = sum(1 for r in row_results if "Fallback" in r.element_source)
    ai_lookups = sum(1 for r in row_results if "AI" in r.element_source or "Healed" in r.element_source)
    input_tokens, output_tokens, cost = _sum_usage_for_run(log_dir, run_id)

    run_records = [r for r in store.list_all() if r.run_id == run_id]
    heals_pending = sum(1 for r in run_records if r.source == "Healed" and r.status == "pending")
    heals_approved = sum(1 for r in run_records if r.source == "Healed" and r.status == "active")
    heals_rejected = sum(1 for r in run_records if r.source == "Healed" and r.status == "rejected")

    totals = RunTotals(
        run_id=run_id, suite=str(args.suite), environment=env_name, start_time=started_iso,
        end_time=ended_iso, total=total, passed=passed, failed=failed, blocked=blocked, skipped=skipped,
        total_input_tokens=input_tokens, total_output_tokens=output_tokens,
        estimated_cost=cost, cache_hit_rate=(cache_hits / total if total else 0.0),
        fallback_rate=(fallback_hits / total if total else 0.0), ai_lookups=ai_lookups,
        heals_pending=heals_pending, heals_approved=heals_approved, heals_rejected=heals_rejected,
        duration_ms=sum(r.execution_time_ms for r in row_results),
    )
    append_run_summary(args.suite, totals)

    history_store.record(RunHistoryEntry(
        run_id=run_id, suite=str(args.suite), environment=env_name, start_time=started_iso, end_time=ended_iso,
        total=total, passed=passed, failed=failed, blocked=blocked, skipped=skipped,
        total_input_tokens=input_tokens, total_output_tokens=output_tokens, estimated_cost=cost,
        cache_hit_rate=totals.cache_hit_rate, fallback_rate=totals.fallback_rate, ai_lookups=ai_lookups,
        heals_pending=heals_pending, heals_approved=heals_approved, heals_rejected=heals_rejected,
        duration_ms=totals.duration_ms,
    ))

    report_path = Path("artifacts/reports") / f"{run_id}.md"
    write_run_report(report_path, run_id=run_id, suite=str(args.suite), environment=env_name, row_results=row_results, store=store)
    console.line(f"Run report written to {report_path}")
    console.run_summary(run_id, {"total": total, "passed": passed, "failed": failed, "blocked": blocked, "skipped": skipped})


def _row_index_for(suite_path, sheet_name, test_case_id) -> int:
    rows = read_suite(suite_path, sheet_name=sheet_name)
    for r in rows:
        if r.test_case_id == test_case_id:
            return r.row_index
    raise ExcelValidationError(f"Could not find row for {test_case_id} when writing results")


def cmd_store(args: argparse.Namespace) -> int:
    settings = load_settings(args.env_file)
    store = ElementStore(settings.database_url.replace("sqlite:///", ""))
    try:
        if args.store_cmd == "list":
            for r in store.list_all(args.application):
                print(f"[{r.id}] {r.key} v{r.version} status={r.status} source={r.source}")
        elif args.store_cmd == "show":
            r = store.get_by_id(args.id)
            print(r)
        elif args.store_cmd == "export":
            import json

            Path(args.file).write_text(json.dumps(store.export(), default=str, indent=2), encoding="utf-8")
            print(f"Exported store to {args.file}")
        elif args.store_cmd == "invalidate":
            count = store.invalidate(application=args.application, environment=args.environment, page=args.page, description=args.description)
            print(f"Invalidated {count} record(s)")
        elif args.store_cmd == "history":
            for r in store.history(args.application, args.environment, args.page, args.description):
                print(f"v{r.version} status={r.status} source={r.source} run={r.run_id} updated={r.updated_at}")
    finally:
        store.close()
    return 0


def cmd_review(args: argparse.Namespace) -> int:
    settings = load_settings(args.env_file)
    store = ElementStore(settings.database_url.replace("sqlite:///", ""))
    queue = ReviewQueue(store)
    try:
        if args.review_cmd == "list":
            for r in queue.list_pending(args.application):
                print(f"[{r.id}] {r.key} v{r.version} source={r.source} run={r.run_id}")
        elif args.review_cmd == "approve":
            r = queue.approve(args.id)
            print(f"Approved [{r.id}] {r.key} v{r.version}")
        elif args.review_cmd == "reject":
            r = queue.reject(args.id)
            print(f"Rejected [{r.id}] {r.key} v{r.version}")
        elif args.review_cmd == "approve-all":
            records = queue.approve_all(args.run)
            print(f"Approved {len(records)} record(s)")
    finally:
        store.close()
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="shtaf")
    parser.add_argument("--env-file", default=".env")
    sub = parser.add_subparsers(dest="command", required=True)

    p_run = sub.add_parser("run")
    p_run.add_argument("--suite", required=True)
    p_run.add_argument("--sheet", default="Test Cases")
    p_run.add_argument("--app-config", required=True)
    p_run.add_argument("--env")
    p_run.add_argument("--page-name", default="Login")
    p_run.add_argument("--user-id", default="unknown")
    p_run.add_argument("--model", default="gpt-4o-mini")
    p_run.set_defaults(func=cmd_run)

    p_store = sub.add_parser("store")
    store_sub = p_store.add_subparsers(dest="store_cmd", required=True)
    for name in ("list",):
        sp = store_sub.add_parser(name)
        sp.add_argument("--application")
    sp = store_sub.add_parser("show")
    sp.add_argument("id", type=int)
    sp = store_sub.add_parser("export")
    sp.add_argument("file")
    sp = store_sub.add_parser("invalidate")
    sp.add_argument("--application", required=True)
    sp.add_argument("--environment")
    sp.add_argument("--page")
    sp.add_argument("--description")
    sp = store_sub.add_parser("history")
    sp.add_argument("application")
    sp.add_argument("environment")
    sp.add_argument("page")
    sp.add_argument("description")
    p_store.set_defaults(func=cmd_store)

    p_review = sub.add_parser("review")
    review_sub = p_review.add_subparsers(dest="review_cmd", required=True)
    sp = review_sub.add_parser("list")
    sp.add_argument("--application")
    sp = review_sub.add_parser("approve")
    sp.add_argument("id", type=int)
    sp = review_sub.add_parser("reject")
    sp.add_argument("id", type=int)
    sp = review_sub.add_parser("approve-all")
    sp.add_argument("--run")
    p_review.set_defaults(func=cmd_review)

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
