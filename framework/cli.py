"""CLI entry point: `shtaf run|store|review ...`"""

from __future__ import annotations

import argparse
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

from selenium import webdriver
from selenium.common.exceptions import NoAlertPresentException
from selenium.webdriver.chrome.options import Options as ChromeOptions
from selenium.webdriver.firefox.options import Options as FirefoxOptions

from framework.browser import SeleniumPage
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
from framework.store.account_cache import clear_account
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

    if args.test_case_id:
        wanted = set(args.test_case_id)
        missing = wanted - {r.test_case_id for r in rows}
        if missing:
            console.line(f"SUITE VALIDATION ERROR: unknown TestCase ID(s): {sorted(missing)}")
            return 2
        # Filtering the list itself, not adding a per-row skip condition,
        # means rows outside the filter are never iterated at all -- their
        # Status/Actual Result cells are therefore never touched by the
        # write-back below (write_results only writes rows present in its
        # own results list), so a single-test-case rerun can't clobber the
        # rest of a workbook's last-known results.
        rows = [r for r in rows if r.test_case_id in wanted]

    run_id = _new_run_id()
    console.line(f"=== Starting run {run_id} | suite={args.suite} | app={app_config.app_name} | env={env_name} ===")

    store = ElementStore(settings.database_url.replace("sqlite:///", ""))
    review_queue = ReviewQueue(store)
    history_store = RunHistoryStore(settings.run_history_db.replace("sqlite:///", ""))
    usage_logger = UsageLogger(Path(settings.log_dir) / "llm_usage.jsonl")

    model = args.model or os.environ.get("OPENAI_MODEL", "gpt-4o-mini")
    llm_client: LlmClient | None = None
    if settings.llm_provider == "openai" and settings.openai_api_key:
        llm_client = LlmClient(
            api_key=settings.openai_api_key, model=model, request_timeout=settings.llm_request_timeout
        )
    else:
        console.line("WARNING: no usable LLM provider configured; Tier 3 will be unavailable this run.")

    headless = settings.browser_headless
    started_iso = datetime.now(timezone.utc).isoformat()
    row_results = []

    driver = _create_driver(settings.browser_engine, headless, app_config.blocked_ad_url_patterns)
    page = SeleniumPage(driver, click_strategy=app_config.click_strategy)
    try:
        for row_index, row in enumerate(rows):
            if not row.execute:
                console.line(f"[{row.test_case_id}] Execute != Y -> Skipped")
                row_results.append(_skipped_result(row.test_case_id))
                continue

            if row_index > 0 and app_config.rate_limit_delay_ms > 0:
                # A bounded, config-declared pause between rows, not an
                # arbitrary sleep sprinkled into step logic -- some sites
                # (confirmed live: saucedemo.com's login, when several rows
                # in the same run each trigger their own fresh login attempt
                # within a short window) soft-throttle rapid repeated
                # attempts from one client regardless of how long any single
                # action took. Skipped before the very first row since there
                # is nothing yet to be rate-limited against.
                time.sleep(app_config.rate_limit_delay_ms / 1000)

            _reset_session(driver, page, env_cfg.base_url, guard, settings.navigation_timeout_ms, console, row.test_case_id)

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
                login_steps=app_config.login_steps, registration_steps=app_config.registration_steps,
                login_success_text=app_config.login_success_text,
                test_account_env_vars=app_config.test_account_env_vars,
                account_cache_dir=Path(settings.database_url.replace("sqlite:///", "")).parent,
            )
            row_results.append(result)

            if result.status in ("Fail", "Blocked") and settings.stop_on_failure:
                console.line(f"[{row.test_case_id}] stop_on_failure=true and status={result.status}; skipping remaining rows")
                remaining = rows[rows.index(row) + 1 :]
                for skipped_row in remaining:
                    row_results.append(_skipped_result(skipped_row.test_case_id))
                break
    finally:
        page.close()

    ended_iso = datetime.now(timezone.utc).isoformat()
    _write_outputs(
        args, run_id, app_config, env_name, row_results, store, history_store,
        started_iso, ended_iso, console, settings.log_dir,
    )

    store.close()
    history_store.close()

    any_bad = any(r.status in ("Fail", "Blocked") for r in row_results)
    return 1 if any_bad else 0


def _reset_session(driver, page, base_url, guard, navigation_timeout_ms, console, test_case_id) -> None:
    """Start each workbook row from a clean, logged-out state, in a fresh
    browser tab.

    Rows execute one after another in a single continuous browser session; a
    row that logs in (e.g. a signup flow) would otherwise leave every later
    row running against a logged-in page it never asked for -- breaking
    anything written assuming a fresh, anonymous visit (most rows), while
    rows that deliberately depend on a prior login (e.g. "Logout") are
    expected to set that up themselves rather than rely on row ordering.

    The reset opens a new tab and closes the previous one rather than just
    navigating the same tab back to base_url. Confirmed live on
    saucedemo.com: repeatedly navigating ONE tab back to the same URL,
    regardless of whether cookies/storage were also cleared, left a React
    input silently refusing to accept send_keys keystrokes from the second
    navigation onward (no exception, field just stayed empty) -- every
    other variable (click mechanism, wait duration, bfcache) was tested and
    ruled out; a fresh tab per attempt was the one change that made it
    reliable, five-for-five. A closed-then-reopened tab is otherwise
    behaviourally identical to navigating in place for every other purpose
    this reset serves.
    """
    try:
        # unhandledPromptBehavior="ignore" (see _create_driver) deliberately
        # leaves a native dialog open rather than auto-dismissing it, so the
        # framework's own accept_dialog verb can handle it deterministically
        # -- but a row whose OWN accept_dialog step never runs (an earlier
        # step in that same row already Failed/Blocked and the row bailed
        # out first, confirmed live: a file-upload row's submit click can
        # leave a confirm() dialog open exactly this way) leaves it
        # stranded. Every WebDriver command -- opening a tab, clearing
        # cookies, even just reading the current URL -- raises
        # UnexpectedAlertPresentException while any dialog is open, which
        # would otherwise crash every later row's reset, not just the one
        # that caused it. Accepting (not dismissing) is the same choice
        # accept_dialog itself makes elsewhere, and is a safe default here
        # regardless of which row's dialog this was: that row's own outcome
        # was already decided before this ever runs.
        alert_text = driver.switch_to.alert.text
        driver.switch_to.alert.accept()
        console.line(
            f"[{test_case_id}] WARNING: a previous row left a native dialog open "
            f"({alert_text!r}) -- accepted it before starting this row"
        )
    except NoAlertPresentException:
        pass  # the expected, common case
    except Exception:
        pass
    try:
        old_handles = driver.window_handles
        driver.switch_to.new_window("tab")
        new_handle = driver.current_window_handle
        for handle in old_handles:
            driver.switch_to.window(handle)
            driver.close()
        driver.switch_to.window(new_handle)
    except Exception as exc:
        console.line(f"[{test_case_id}] WARNING: could not open a fresh tab before row: {exc}")
    cookies_cleared = False
    try:
        # Cleared via CDP, not driver.delete_all_cookies(): a brand-new tab
        # starts on about:blank, which has no origin, and
        # delete_all_cookies() can only reach cookies belonging to the
        # page it's currently on -- it would silently clear nothing here,
        # leaving the row logged in as whoever a previous row's row left
        # authenticated. Network.clearBrowserCookies has no such
        # restriction, so it can run before this tab's one and only
        # navigation below -- deliberately only one: saucedemo.com's
        # fresh-tab fix (see this function's own docstring) depends on this
        # tab never navigating more than once. Chrome-only (no CDP on
        # Firefox); the fallback below covers that case instead of silently
        # leaving cookies uncleared.
        driver.execute_cdp_cmd("Network.clearBrowserCookies", {})
        cookies_cleared = True
    except Exception:
        pass
    try:
        guard.check(base_url)
        driver.get(base_url)
        if not cookies_cleared:
            # Firefox (or any other non-CDP driver): no way to clear
            # cookies before the first navigation, so this costs a second
            # one in this tab -- accepted here since it's Chrome, not
            # Firefox, where the fresh-tab fix's own motivating bug was
            # ever observed.
            driver.delete_all_cookies()
            driver.get(base_url)
        driver.execute_script("window.localStorage.clear(); window.sessionStorage.clear();")
    except Exception as exc:
        console.line(f"[{test_case_id}] WARNING: could not reset session before row: {exc}")


def _create_driver(browser_engine: str, headless: bool, blocked_ad_url_patterns: list[str] | None = None):
    """Create the Selenium driver selected by the framework settings.

    unhandledPromptBehavior="ignore" is set explicitly so a native JS dialog
    is left exactly as-is for the framework's own accept_dialog verb to
    handle deterministically -- without it, the driver's default ("dismiss
    and notify") can silently auto-dismiss an alert on its own schedule,
    racing against whenever the framework gets around to checking for one.
    """
    if browser_engine == "firefox":
        options = FirefoxOptions()
        if headless:
            options.add_argument("-headless")
        options.set_capability("unhandledPromptBehavior", "ignore")
        return webdriver.Firefox(options=options)

    options = ChromeOptions()
    if headless:
        options.add_argument("--headless=new")
    options.add_argument("--disable-popup-blocking")
    options.add_argument("--disable-notifications")
    options.add_argument("--window-size=1366,900")
    options.set_capability("unhandledPromptBehavior", "ignore")
    driver = webdriver.Chrome(options=options)
    if blocked_ad_url_patterns:
        _block_ad_network_requests(driver, blocked_ad_url_patterns)
    return driver


def _block_ad_network_requests(driver, url_patterns: list[str]) -> None:
    """Refuse third-party ad/tracker requests at the network layer (Chrome
    DevTools Protocol), so an ad never loads far enough to render an
    interstitial, overlay, or click-intercepting iframe in the first place --
    this is strictly more reliable than any amount of after-the-fact overlay
    detection and dismissal, since there is simply nothing there to dismiss.
    Selenium 4 exposes CDP directly; no special browser launch flag needed,
    and the block list can be the app's own config, not framework code.
    """
    try:
        driver.execute_cdp_cmd("Network.enable", {})
        driver.execute_cdp_cmd("Network.setBlockedURLs", {"urls": list(url_patterns)})
    except Exception:
        pass


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
    output_path = write_results(args.suite, args.sheet, excel_rows)

    total = len(row_results)
    passed = sum(1 for r in row_results if r.status == "Pass")
    failed = sum(1 for r in row_results if r.status == "Fail")
    blocked = sum(1 for r in row_results if r.status == "Blocked")
    skipped = sum(1 for r in row_results if r.status == "Skipped")

    # Per-ELEMENT tier counts, not per-row: a row with e.g. "Cache,Cache,Fallback"
    # resolved 2 of 3 elements from cache -- counting it as "not a cache hit"
    # because the row's combined tag isn't the single literal string "Cache"
    # would badly understate the real cache-hit rate the spec asks to report.
    from collections import Counter

    element_tier_counts: Counter = Counter()
    for r in row_results:
        if r.element_source and r.element_source != "None":
            element_tier_counts.update(r.element_source.split(","))
    total_element_lookups = sum(element_tier_counts.values())
    cache_hits = element_tier_counts.get("Cache", 0)
    fallback_hits = element_tier_counts.get("Fallback", 0)
    ai_lookups = element_tier_counts.get("AI", 0) + element_tier_counts.get("Healed", 0)
    input_tokens, output_tokens, cost = _sum_usage_for_run(log_dir, run_id)

    run_records = [r for r in store.list_all() if r.run_id == run_id]
    heals_pending = sum(1 for r in run_records if r.source == "Healed" and r.status == "pending")
    heals_approved = sum(1 for r in run_records if r.source == "Healed" and r.status == "active")
    heals_rejected = sum(1 for r in run_records if r.source == "Healed" and r.status == "rejected")

    totals = RunTotals(
        run_id=run_id, suite=str(args.suite), environment=env_name, start_time=started_iso,
        end_time=ended_iso, total=total, passed=passed, failed=failed, blocked=blocked, skipped=skipped,
        total_input_tokens=input_tokens, total_output_tokens=output_tokens,
        estimated_cost=cost, cache_hit_rate=(cache_hits / total_element_lookups if total_element_lookups else 0.0),
        fallback_rate=(fallback_hits / total_element_lookups if total_element_lookups else 0.0), ai_lookups=ai_lookups,
        heals_pending=heals_pending, heals_approved=heals_approved, heals_rejected=heals_rejected,
        duration_ms=sum(r.execution_time_ms for r in row_results),
    )
    output_path = append_run_summary(output_path, totals)

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
    if output_path != Path(args.suite):
        console.line(
            f"Workbook was locked; completed results and summary written to {output_path}"
        )
    else:
        console.line(f"Results written to {output_path}")
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
        elif args.store_cmd == "reset":
            removed = store.purge(application=args.application, environment=args.environment)
            cache_dir = Path(settings.database_url.replace("sqlite:///", "")).parent
            cleared_account = clear_account(cache_dir, args.application, args.environment or "test")
            print(
                f"Removed {removed} element record(s) for {args.application}"
                + (f" (env={args.environment})" if args.environment else " (all environments)")
                + (" and cleared its cached test account" if cleared_account else " (no cached test account to clear)")
            )
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
    p_run.add_argument(
        "--model",
        default=None,
        help="LLM model (defaults to OPENAI_MODEL or gpt-4o-mini)",
    )
    # Repeatable, not comma-split: a TestCase ID could in principle contain a
    # comma in some workbook's own convention, so "give me exactly these IDs"
    # is expressed as one flag per ID rather than parsing a delimited string.
    p_run.add_argument(
        "--test-case-id", action="append", default=None,
        help="Run only these TestCase ID(s) from the suite (repeatable). Omit to run the whole suite.",
    )
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
    sp = store_sub.add_parser("reset")
    sp.add_argument("--application", required=True)
    sp.add_argument("--environment", help="Omit to reset every environment for this app")
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
