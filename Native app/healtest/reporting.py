"""Run reports and the run history (date, suite, counts, tokens, cache hit rate, heals)."""

import json
import logging
from datetime import datetime
from pathlib import Path
from typing import Any, Dict

logger = logging.getLogger(__name__)


def append_run_history(log_dir: str, result: Dict[str, Any], app: str, env: str):
    path = Path(log_dir) / "run_history.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    history = []
    if path.exists():
        try:
            history = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            history = []
    s = result["summary"]
    history.append({
        "run_id": result["run_id"], "date": datetime.now().isoformat(timespec="seconds"), "suite": result["suite"],
        "app": app, "environment": env, "pass": s["Pass"], "fail": s["Fail"], "blocked": s["Blocked"],
        "skipped": s["Skipped"], "total_tokens": s["total_tokens"], "llm_calls": s["llm_calls"],
        "cost_usd": s["cost_usd"], "cache_hit_rate": s["cache_hit_rate"], "heals": s["healed_count"],
        "duration_s": s["duration_s"]})
    path.write_text(json.dumps(history, indent=2), encoding="utf-8")


def write_report(result: Dict[str, Any], store, report_dir: str = "reports") -> str:
    Path(report_dir).mkdir(parents=True, exist_ok=True)
    s, run_id = result["summary"], result["run_id"]
    pending = [{"key": e["key"], "page": e["page"], "target": e["target"], "healed_at": e["pending"]["healed_at"]}
               for e in store.pending()]
    healed_rows = [r for r in result["steps"] if "Healed" in r["element_source"]]
    (Path(report_dir) / f"report_{run_id}.json").write_text(
        json.dumps({**result, "store": store.stats(), "pending_heal_review": pending}, indent=2, default=str), encoding="utf-8")

    total = s["Pass"] + s["Fail"] + s["Blocked"] + s["Skipped"]
    lines = [
        "=" * 78, f"FINAL REPORT  run {run_id}  suite {result['suite']}", "=" * 78,
        f"Test cases : {total}  Pass {s['Pass']}  Fail {s['Fail']}  Blocked {s['Blocked']}  Skipped {s['Skipped']}",
        f"Steps      : {s['steps']}",
        f"LLM usage  : {s['llm_calls']} calls, {s['total_tokens']:,} tokens "
        f"({s['input_tokens']:,} in / {s['output_tokens']:,} out), cost ${s['cost_usd']:.4f}",
        f"Lookups    : {s['lookups']} element lookups -> {s['cache_hits']} cache, {s['fallback_hits']} fallback, "
        f"{s['ai_identified']} AI, {s['healed_count']} healed, {s['blocked_lookups']} blocked "
        f"(cache hit rate {s['cache_hit_rate']}%)",
        f"Store      : {store.stats()}",
        f"Duration   : {s['duration_s']}s",
        "", f"{'Test case':<10}{'Status':<9}{'Tokens':>8}{'Time(s)':>9}  Sources", "-" * 78]
    for row in result["cases"]:
        lines.append(f"{row['test_case_id']:<10}{row['status']:<9}{row['tokens_used']:>8}{row['execution_time']:>9}  "
                     f"{row['element_source'] or '-'}")
    if s["healed_count"] or pending:
        lines += ["", "HEALED ELEMENTS (need review: `healtest store review`)"]
        lines += [f"  step {r['test_case_id']}/{r['step_number']}: {r['step_text'][:60]}" for r in healed_rows]
        lines += [f"  pending: {p['target']} ({p['page']})" for p in pending]
    failures = [r for r in result["cases"] if r["status"] in ("Fail", "Blocked")]
    if failures:
        lines += ["", "NEEDS ATTENTION"]
        lines += [f"  {r['test_case_id']} [{r['status']}] {r['actual_result'].splitlines()[0][:150]}" for r in failures]
    lines.append("=" * 78)
    text = "\n".join(lines)
    (Path(report_dir) / f"report_{run_id}.txt").write_text(text, encoding="utf-8")
    return text
