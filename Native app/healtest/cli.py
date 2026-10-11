"""Command line: run a suite, manage the element store, evaluate repeat-run behaviour."""

import argparse
import json
import logging
import sys
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List

from healtest.executor import Executor, RunOptions
from healtest.llm import LLMFatalError
from healtest.page import similarity, summarize_fingerprint
from healtest.reporting import append_run_history, write_report
from healtest.settings import ConfigError, Settings
from healtest.store import ElementStore

logger = logging.getLogger("healtest")


def setup_logging(debug: bool, log_dir: str = "logs"):
    root = logging.getLogger()
    root.handlers.clear()
    root.setLevel(logging.DEBUG if debug else logging.INFO)
    fmt = logging.Formatter("%(asctime)s | %(message)s", "%H:%M:%S")
    console = logging.StreamHandler(sys.stdout)
    console.setFormatter(fmt)
    Path(log_dir).mkdir(parents=True, exist_ok=True)
    file_handler = logging.FileHandler(Path(log_dir) / "run.log", encoding="utf-8")
    file_handler.setFormatter(logging.Formatter("%(asctime)s | %(levelname)s | %(name)s | %(message)s"))
    root.addHandler(console)
    root.addHandler(file_handler)
    for noisy in ("httpx", "httpx2", "httpcore", "openai", "urllib3", "selenium", "WDM", "asyncio"):
        logging.getLogger(noisy).setLevel(logging.WARNING)
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")


def _store(args, settings: Settings) -> ElementStore:
    cfg = settings.section("element_store")
    return ElementStore(cfg.get("location", "./storage/element_store"), args.app, args.env,
                        auto_approve=settings.section("self_healing").get("auto_approve", False))


# ---------------------------------------------------------------------- run

def run_once(settings: Settings, args) -> Dict[str, Any]:
    options = RunOptions(headless=True if args.headless else None, action_delay=args.slow)
    executor = Executor(settings, args.app, args.env, options)
    result = executor.run(args.suite)
    append_run_history(executor.log_dir, result, args.app, args.env)
    report = write_report(result, executor.store)
    print("\n" + report + "\n")
    result["executor"] = executor
    return result


def cmd_run(args, settings: Settings) -> int:
    code = 0
    for number in range(1, args.runs + 1):
        if args.runs > 1:
            logger.info("##### run %d of %d #####", number, args.runs)
        result = run_once(settings, args)
        s = result["summary"]
        if s["Fail"] or s["Blocked"]:
            code = 1
    return code


# ---------------------------------------------------------------------- store

def cmd_store(args, settings: Settings) -> int:
    store = _store(args, settings)
    action = args.store_command
    if action == "list":
        for e in store.entries(args.page):
            flag = " [PENDING HEAL]" if e.get("pending") else ""
            print(f"{e['key']}  v{e['version']}  used {e['usage_count']}x{flag}\n    page  : {e['page']}\n"
                  f"    target: {e['target']}\n    element: {summarize_fingerprint(e['fingerprint'])}\n"
                  f"    locators: {', '.join(k for k, v in e['locators'].items() if v)}")
        print(f"\n{len(store.data)} elements. {store.stats()}")
    elif action == "stats":
        print(json.dumps(store.stats(), indent=2))
    elif action == "export":
        store.export(args.output)
        print(f"Exported to {args.output}")
    elif action == "invalidate":
        if args.key:
            print("Invalidated" if store.invalidate(args.key) else "No such key")
        elif args.page:
            print(f"Invalidated {store.invalidate_page(args.page)} elements on pages matching {args.page!r}")
        elif args.plans:
            store.plans.data.clear()
            store.plans.save()
            print("Cleared the cached step plans")
        else:
            print("Give --key, --page or --plans")
            return 2
    elif action == "review":
        pending = store.pending()
        if not pending:
            print("No healed references are waiting for review.")
        for e in pending:
            p = e["pending"]
            print(f"{e['key']}  {e['target']}  ({e['page']})\n    old: {summarize_fingerprint(e['fingerprint'])}\n"
                  f"    new: {summarize_fingerprint(p['fingerprint'])}  healed {p['healed_at']} in run {p['run_id']}\n"
                  f"    similarity old/new: {similarity(e['fingerprint'], p['fingerprint']):.2f}")
    elif action in ("approve", "reject"):
        keys = [e["key"] for e in store.pending()] if args.all else [args.key]
        done = [k for k in keys if (store.approve(k) if action == "approve" else store.reject(k))]
        print(f"{action}d {len(done)} healed reference(s)")
    return 0


# ---------------------------------------------------------------------- evaluate

def _break_sample(store: ElementStore, count: int) -> List[Dict[str, Any]]:
    """Corrupt locators and fingerprint of `count` entries to simulate changed elements; remember the truth."""
    entries = store.entries()
    step = max(1, len(entries) // max(count, 1))
    chosen = entries[::step][:count]
    truth = []
    for e in chosen:
        truth.append({"key": e["key"], "target": e["target"], "fingerprint": e["fingerprint"]})
        e["locators"] = {"css": "#element-removed-by-evaluation", "xpath": "//*[@id='element-removed-by-evaluation']"}
        e["fingerprint"] = {"tag": e["fingerprint"].get("tag"), "text": "changed-by-evaluation",
                            "attrs": {"id": "changed-by-evaluation"}}
    store.save()
    return truth


def cmd_evaluate(args, settings: Settings) -> int:
    runs = []
    for number in range(1, args.runs + 1):
        logger.info("##### evaluation run %d of %d #####", number, args.runs)
        result = run_once(settings, args)
        runs.append(result)

    report: Dict[str, Any] = {"suite": Path(args.suite).name, "app": args.app, "env": args.env,
                              "generated": datetime.now().isoformat(timespec="seconds"), "runs": []}
    for i, r in enumerate(runs, 1):
        s = r["summary"]
        steps = sum(s["steps"].values()) or 1
        report["runs"].append({"run": i, "run_id": r["run_id"], "tokens": s["total_tokens"], "llm_calls": s["llm_calls"],
                               "cache_hit_rate": s["cache_hit_rate"], "lookups": s["lookups"], "duration_s": s["duration_s"],
                               "cost_usd": s["cost_usd"], "cost_per_100_steps": round(s["cost_usd"] / steps * 100, 4),
                               "pass": s["Pass"], "fail": s["Fail"], "blocked": s["Blocked"], "skipped": s["Skipped"]})
    statuses = {}
    for r in runs:
        for row in r["cases"]:
            statuses.setdefault(row["test_case_id"], []).append(row["status"])
    report["stability"] = {"consistent": len([k for k, v in statuses.items() if len(set(v)) == 1]),
                           "total": len(statuses), "unstable": {k: v for k, v in statuses.items() if len(set(v)) > 1}}

    if args.break_elements:
        store = _store(args, settings)
        truth = _break_sample(store, args.break_elements)
        logger.info("##### healing evaluation: %d stored references corrupted #####", len(truth))
        heal_run = run_once(settings, args)
        store = _store(args, settings)
        outcomes = []
        for t in truth:
            e = store.data.get(t["key"])
            healed_fp = (e["pending"] or {}).get("fingerprint") if e and e.get("pending") else (e or {}).get("fingerprint")
            healed = bool(e) and (bool(e.get("pending")) or e["version"] > 1)
            score = similarity(t["fingerprint"], healed_fp) if healed and healed_fp else 0.0
            outcomes.append({"target": t["target"], "healed": healed, "same_element_score": round(score, 2),
                             "correct": healed and score >= 0.9})
        healed_n = len([o for o in outcomes if o["healed"]])
        report["healing"] = {"broken": len(truth), "healed": healed_n,
                             "correct": len([o for o in outcomes if o["correct"]]),
                             "false_heals": healed_n - len([o for o in outcomes if o["correct"]]),
                             "tokens": heal_run["summary"]["total_tokens"], "cost_usd": heal_run["summary"]["cost_usd"],
                             "pass": heal_run["summary"]["Pass"], "details": outcomes}

    out = Path("reports") / f"evaluation_{datetime.now().strftime('%Y%m%d_%H%M%S')}.json"
    out.parent.mkdir(exist_ok=True)
    out.write_text(json.dumps(report, indent=2), encoding="utf-8")
    print("\nEVALUATION SUMMARY")
    for row in report["runs"]:
        print(f"  run {row['run']}: tokens={row['tokens']:,} calls={row['llm_calls']} cache_hit={row['cache_hit_rate']}% "
              f"time={row['duration_s']}s cost=${row['cost_usd']} pass={row['pass']} fail={row['fail']} blocked={row['blocked']}")
    print(f"  stability: {report['stability']['consistent']}/{report['stability']['total']} test cases gave the same result every run")
    if "healing" in report:
        h = report["healing"]
        print(f"  healing: {h['healed']}/{h['broken']} healed, {h['correct']} correct, {h['false_heals']} false heals")
    print(f"  saved to {out}")
    return 0


# ---------------------------------------------------------------------- entry point

def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="healtest", description="AI-assisted, self-healing web test automation")
    p.add_argument("--config-dir", default="config")
    p.add_argument("--debug", action="store_true")
    sub = p.add_subparsers(dest="command", required=True)

    def common(sp):
        sp.add_argument("--app", "-a", default="automationexercise")
        sp.add_argument("--env", "-e", default="production")

    run = sub.add_parser("run", help="run a suite")
    common(run)
    run.add_argument("--suite", "-s", required=True)
    run.add_argument("--runs", "-r", type=int, default=1)
    run.add_argument("--headless", action="store_true", help="hide the browser window (default: visible)")
    run.add_argument("--slow", type=float, default=None, help="extra seconds to pause after every action, to watch it run")

    ev = sub.add_parser("evaluate", help="run a suite several times and report the Assignment 4 metrics")
    common(ev)
    ev.add_argument("--suite", "-s", required=True)
    ev.add_argument("--runs", "-r", type=int, default=3)
    ev.add_argument("--headless", action="store_true")
    ev.add_argument("--slow", type=float, default=None)
    ev.add_argument("--break-elements", type=int, default=0, help="afterwards corrupt N stored references and measure healing")

    st = sub.add_parser("store", help="inspect and manage the element store")
    common(st)
    ssub = st.add_subparsers(dest="store_command", required=True)
    lst = ssub.add_parser("list")
    lst.add_argument("--page")
    ssub.add_parser("stats")
    exp = ssub.add_parser("export")
    exp.add_argument("--output", "-o", default="reports/element_store_export.json")
    inv = ssub.add_parser("invalidate")
    inv.add_argument("--key")
    inv.add_argument("--page")
    inv.add_argument("--plans", action="store_true")
    ssub.add_parser("review")
    for name in ("approve", "reject"):
        sp = ssub.add_parser(name)
        sp.add_argument("--key")
        sp.add_argument("--all", action="store_true")
    return p


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    setup_logging(args.debug)
    try:
        settings = Settings(args.config_dir)
        if args.command == "run":
            return cmd_run(args, settings)
        if args.command == "evaluate":
            return cmd_evaluate(args, settings)
        return cmd_store(args, settings)
    except (ConfigError, FileNotFoundError, ValueError) as exc:
        logger.error("%s", exc)
        return 2
    except LLMFatalError as exc:
        logger.error("Run stopped: %s", exc)
        return 3
    except Exception as exc:
        logger.error("Run aborted: %s", exc, exc_info=args.debug)
        return 1


if __name__ == "__main__":
    sys.exit(main())
