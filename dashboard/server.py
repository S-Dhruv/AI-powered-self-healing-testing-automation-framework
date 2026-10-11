"""Local demo dashboard for the self-healing test framework.

A thin FastAPI shell around the real `shtaf` CLI -- it never re-implements
resolution, execution, or reporting. It reads the same workbooks and the same
LLM usage log the framework itself writes, and for a live run it simply
spawns `python -m framework.cli run ...` as a subprocess and streams its real
stdout back to the browser over a WebSocket, parsing the console narrator's
own line formats (framework/reporting/console.py) into structured events.
Nothing shown in the UI is synthesized -- it is the actual run, watched live.

Not part of the framework itself (see pyproject.toml's "dashboard" extra):
a demo UI is not a dependency the core engine should carry.
"""

from __future__ import annotations

import asyncio
import json
import re
import shutil
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

import openpyxl
from fastapi import FastAPI, Query, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from framework.excel.step_parser import parse_test_steps

ROOT = Path(__file__).resolve().parent.parent
STATIC_DIR = Path(__file__).resolve().parent / "static"
USAGE_LOG = ROOT / "artifacts" / "logs" / "llm_usage.jsonl"

APPS = {
    "automationexercise": {
        "label": "automationexercise.com",
        "suite": ROOT / "AI_Assignment_Test_Cases.xlsx",
        "config": ROOT / "apps" / "automationexercise" / "config.yaml",
    },
    "bbc": {
        "label": "bbc.com",
        "suite": ROOT / "BBC_Test_Cases.xlsx",
        "config": ROOT / "apps" / "bbc" / "config.yaml",
    },
}

app = FastAPI(title="shtaf dashboard")


_RE_STEP_DETAIL = re.compile(r"^step (?P<no>\d+) \(.*?\): (?P<status>Pass|Fail|Blocked|Skipped) - (?P<detail>.*)$")


def _parse_step_outcomes(actual_result: str | None) -> dict[int, dict]:
    """Recovers each sub-step's own status/detail from the single Actual
    Result string the writer persists (executor.py joins per-step fragments
    with ' | '). This is the only place that per-step history survives once
    a run finishes and the page is reloaded -- parsing it back out lets a
    refresh restore the same step-box colors and back-face detail a live run
    showed, straight from the workbook, with no separate state store needed.
    """
    if not actual_result:
        return {}
    outcomes: dict[int, dict] = {}
    for fragment in actual_result.split(" | "):
        match = _RE_STEP_DETAIL.match(fragment.strip())
        if match:
            outcomes[int(match["no"])] = {"status": match["status"], "detail": match["detail"]}
    return outcomes


def _read_test_cases(app_key: str) -> list[dict]:
    meta = APPS[app_key]
    wb = openpyxl.load_workbook(meta["suite"], data_only=True)
    ws = wb["Test Cases"]
    headers = [c.value for c in ws[1]]
    idx = {h: i for i, h in enumerate(headers) if h}
    rows = []
    for row in ws.iter_rows(min_row=2, values_only=True):
        tc_id = row[idx["TestCase ID"]]
        if not tc_id:
            continue
        test_step = str(row[idx["Test Step"]] or "")
        actual_result = row[idx["Actual Result"]] if "Actual Result" in idx else None
        step_outcomes = _parse_step_outcomes(actual_result)
        try:
            steps = [
                {
                    "no": s.step_no, "action": s.action, "target": s.target,
                    **step_outcomes.get(s.step_no, {}),
                }
                for s in parse_test_steps(test_step)
            ]
        except Exception:
            steps = []
        rows.append(
            {
                "id": str(tc_id).strip(),
                "title": str(row[idx["Title"]] or ""),
                "execute": str(row[idx.get("Execute", -1)] or "").strip().upper() == "Y"
                if "Execute" in idx
                else True,
                "status": row[idx["Status"]] if "Status" in idx else None,
                "actual_result": actual_result,
                "tokens_used": row[idx["Tokens Used"]] if "Tokens Used" in idx else None,
                "element_source": row[idx["Element Source"]] if "Element Source" in idx else None,
                "steps": steps,
            }
        )
    wb.close()
    return rows


def _read_usage() -> dict:
    entries = []
    if USAGE_LOG.is_file():
        for line in USAGE_LOG.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                entries.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    entries.sort(key=lambda e: e.get("timestamp", ""))
    total_in = sum(e.get("input_tokens", 0) for e in entries)
    total_out = sum(e.get("output_tokens", 0) for e in entries)
    total_cost = sum(e.get("estimated_cost", 0.0) for e in entries)
    success = sum(1 for e in entries if e.get("status") == "success")
    healing = sum(1 for e in entries if e.get("reason") == "healing")
    first_learning = sum(1 for e in entries if e.get("reason") == "first_learning")
    avg_latency = (
        sum(e.get("latency_ms", 0) for e in entries) / len(entries) if entries else 0
    )
    return {
        "total_calls": len(entries),
        "total_input_tokens": total_in,
        "total_output_tokens": total_out,
        "total_tokens": total_in + total_out,
        "total_cost": round(total_cost, 6),
        "success_rate": round(success / len(entries), 4) if entries else None,
        "first_learning_calls": first_learning,
        "healing_calls": healing,
        "avg_latency_ms": round(avg_latency, 1),
        "recent": list(reversed(entries))[:50],
    }


@app.get("/")
def index():
    return FileResponse(STATIC_DIR / "index.html", headers={"Cache-Control": "no-store"})


_NO_STORE = {"Cache-Control": "no-store"}


@app.get("/api/testcases/{app_key}")
def api_testcases(app_key: str):
    # no-store: this is read fresh from the workbook on every call already,
    # so a page reload must never be served a browser-cached copy of an
    # earlier run's results for this same URL.
    if app_key not in APPS:
        return JSONResponse({"error": "unknown app"}, status_code=404, headers=_NO_STORE)
    return JSONResponse(
        {
            "app": app_key,
            "label": APPS[app_key]["label"],
            "test_cases": _read_test_cases(app_key),
        },
        headers=_NO_STORE,
    )


@app.get("/api/llm-usage")
def api_llm_usage():
    return JSONResponse(_read_usage(), headers=_NO_STORE)


@app.post("/api/clear-cache/{app_key}")
async def api_clear_cache(app_key: str):
    """Hard-resets this app's element store and cached test account -- the
    exact same operation as `shtaf store reset --application <name>`, run as
    that same subprocess rather than a second, separately-opened database
    connection (avoids racing a connection a live run's own subprocess
    already holds open). The next run then resolves and stores every
    element completely fresh via the AI tier.

    Previously called `store invalidate`, which only flips active records to
    invalidated -- the row stays in the database, and resolve.py's
    had_prior_record (any history at all, not just an active record) still
    treats the element as mid-heal rather than a clean first learn on its
    next resolution. `store reset` actually removes the rows, which is what
    a "Clear Cache" button is supposed to deliver.

    Uses asyncio's own subprocess API, the same mechanism already used (and
    proven working) for the live run WebSocket above, rather than a second,
    separate mechanism for one more endpoint.
    """
    if app_key not in APPS:
        return JSONResponse({"error": "unknown app"}, status_code=404)
    try:
        proc = await asyncio.create_subprocess_exec(
            sys.executable, "-m", "framework.cli", "store", "reset", "--application", app_key,
            cwd=str(ROOT), stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=30)
    except Exception as exc:
        return JSONResponse({"error": str(exc)}, status_code=500)
    if proc.returncode != 0:
        message = stdout.decode(errors="replace").strip() or stderr.decode(errors="replace").strip()
        return JSONResponse({"error": message}, status_code=500)
    return {"app": app_key, "message": stdout.decode(errors="replace").strip()}


@app.post("/api/clear-usage")
def api_clear_usage():
    """Clears the LLM usage log (artifacts/logs/llm_usage.jsonl) that backs
    the LLM Usage panel -- resets its call count, token totals, cost, and
    recent-calls table back to zero for a fresh demo. A timestamped backup
    is written first (the same file writer.py's own backup_workbook already
    does before any other destructive write in this project), never a bare
    delete -- the real, append-only log the spec requires every LLM call to
    be written to (including failed calls) is not something to lose
    permanently just to reset a dashboard view.
    """
    if not USAGE_LOG.is_file():
        return {"message": "No usage log to clear.", "entries_cleared": 0}
    entries_cleared = sum(1 for line in USAGE_LOG.read_text(encoding="utf-8").splitlines() if line.strip())
    ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    backup_path = USAGE_LOG.with_name(f"{USAGE_LOG.stem}.backup-{ts}{USAGE_LOG.suffix}")
    shutil.copy2(USAGE_LOG, backup_path)
    USAGE_LOG.write_text("", encoding="utf-8")
    return {"message": f"Cleared {entries_cleared} entries (backed up to {backup_path.name}).", "entries_cleared": entries_cleared}


# ---------------------------------------------------------------------------
# Live run streaming: parses the real console narrator's own line shapes
# (framework/reporting/console.py) -- never a separate implementation of
# what the framework already prints.
# ---------------------------------------------------------------------------

_RE_TEST_CASE_START = re.compile(r"\[(?P<tc>[\w-]+)\]\[step 0\] test case -> '(?P<title>.*)'$")
_RE_TIER = re.compile(r"\[(?P<tc>[\w-]+)\]\[step (?P<step>\d+)\] Tier (?P<tier>[\w .()]+): (?P<outcome>.*)$")
_RE_LLM_CALL = re.compile(
    r"\[(?P<tc>[\w-]+)\]\[step (?P<step>\d+)\] LLM call -> model=(?P<model>\S+) "
    r"in=(?P<tin>\d+) out=(?P<tout>\d+) confidence=(?P<conf>[\d.]+)"
)
_RE_VERIFY = re.compile(r"\[(?P<tc>[\w-]+)\]\[step (?P<step>\d+)\] verify: (?P<ok>PASS|FAIL) \((?P<reason>.*)\)$")
_RE_ACTION = re.compile(r"\[(?P<tc>[\w-]+)\]\[step (?P<step>\d+)\] ACTION (?P<verb>\S+) -> (?P<selector>.*)$")
_RE_BLOCKED = re.compile(r"\[(?P<tc>[\w-]+)\]\[step (?P<step>\d+)\] BLOCKED: (?P<reason>.*)$")
_RE_STEP_RESULT = re.compile(r"\[(?P<tc>[\w-]+)\]\[step (?P<step>\d+)\] => (?P<status>Pass|Fail|Blocked)$")
_RE_RUN_SUMMARY = re.compile(r"Run (?P<run_id>\S+) summary: (?P<totals>\{.*\})$")
_RE_SKIPPED = re.compile(r"\[(?P<tc>[\w-]+)\] Execute != Y -> Skipped$")
_RE_LLM_RESPONSE_HEADER = re.compile(r"\[(?P<tc>[\w-]+)\]\[step (?P<step>\d+)\] LLM RESPONSE \(raw\):$")
_RE_LLM_RESPONSE_LINE = re.compile(r"^\s*\|\s*(?P<json>\{.*\})\s*$")
_RE_LLM_PROMPT_HEADER = re.compile(r"\[(?P<tc>[\w-]+)\]\[step (?P<step>\d+)\] LLM REQUEST \(prompt\):$")
_RE_PIPE_LINE = re.compile(r"^\s*\| ?(?P<content>.*)$")
_RE_BLOCK_SEPARATOR = re.compile(r"^\s*-{10,}\s*$")


def _parse_line(raw_line: str) -> dict | None:
    # Console lines are "[HH:MM:SS] <rest>" or "[HH:MM:SS]   <rest>" etc.
    m = re.match(r"^\[\d{2}:\d{2}:\d{2}\]\s?(.*)$", raw_line)
    text = m.group(1) if m else raw_line

    if match := _RE_TEST_CASE_START.search(text):
        return {"type": "test_case_start", "tc": match["tc"], "title": match["title"]}
    if match := _RE_LLM_CALL.search(text):
        return {
            "type": "llm_call",
            "tc": match["tc"], "step": int(match["step"]), "model": match["model"],
            "tokens_in": int(match["tin"]), "tokens_out": int(match["tout"]),
            "confidence": float(match["conf"]),
        }
    if match := _RE_VERIFY.search(text):
        return {
            "type": "verify", "tc": match["tc"], "step": int(match["step"]),
            "ok": match["ok"] == "PASS", "reason": match["reason"],
        }
    if match := _RE_TIER.search(text):
        return {
            "type": "tier", "tc": match["tc"], "step": int(match["step"]),
            "tier": match["tier"], "outcome": match["outcome"],
        }
    if match := _RE_ACTION.search(text):
        return {
            "type": "action", "tc": match["tc"], "step": int(match["step"]),
            "verb": match["verb"], "selector": match["selector"],
        }
    if match := _RE_BLOCKED.search(text):
        return {"type": "blocked", "tc": match["tc"], "step": int(match["step"]), "reason": match["reason"]}
    if match := _RE_STEP_RESULT.search(text):
        return {
            "type": "step_result", "tc": match["tc"], "step": int(match["step"]),
            "status": match["status"],
        }
    if match := _RE_SKIPPED.search(text):
        return {"type": "skipped", "tc": match["tc"]}
    if match := _RE_RUN_SUMMARY.search(text):
        try:
            totals = json.loads(match["totals"].replace("'", '"'))
        except Exception:
            totals = None
        return {"type": "run_summary", "run_id": match["run_id"], "totals": totals}
    return None


def _kill_process_tree(pid: int) -> None:
    """Kill a process AND its children (chromedriver, the real browser) --
    terminating just the top-level `python -m framework.cli` process leaves
    those orphaned on Windows, since killing a process there does not kill
    its children the way a SIGTERM to a process group would on POSIX.
    """
    if sys.platform == "win32":
        subprocess.run(
            ["taskkill", "/F", "/T", "/PID", str(pid)],
            capture_output=True, check=False,
        )
    else:
        import os
        import signal

        try:
            os.killpg(os.getpgid(pid), signal.SIGKILL)
        except ProcessLookupError:
            pass


@app.websocket("/ws/run/{app_key}")
async def ws_run(
    websocket: WebSocket,
    app_key: str,
    test_case_id: list[str] | None = Query(default=None),
):
    await websocket.accept()
    if app_key not in APPS:
        await websocket.send_json({"type": "error", "message": "unknown app"})
        await websocket.close()
        return

    meta = APPS[app_key]
    cmd = [
        # The SAME interpreter running this server, not whatever "python"
        # resolves to on PATH -- the run needs the project's venv (selenium,
        # openai, etc.), which is only guaranteed to be sys.executable here.
        sys.executable, "-u", "-m", "framework.cli", "run",
        "--suite", str(meta["suite"]),
        "--app-config", str(meta["config"]),
        "--env", "test",
    ]
    # ?test_case_id=TC_003&test_case_id=TC_005 (repeatable query param) ->
    # rerun exactly those rows; omitted entirely -> the whole suite, same as
    # before. One --test-case-id flag per ID, mirroring the CLI's own flag.
    for tc_id in test_case_id or []:
        cmd += ["--test-case-id", tc_id]
    popen_kwargs = {} if sys.platform == "win32" else {"start_new_session": True}
    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            cwd=str(ROOT),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
            **popen_kwargs,
        )
    except Exception as exc:
        await websocket.send_json({"type": "error", "message": f"could not start run: {exc}"})
        await websocket.close()
        return

    await websocket.send_json({"type": "run_started", "app": app_key})
    stopped_by_user = False

    async def stream_stdout() -> None:
        assert proc.stdout is not None
        # LLM RESPONSE (raw) is printed as a small, separate block (see
        # console.llm_io) right after the matching "LLM call" line: a header
        # naming the test case/step, then the actual JSON on its own "| "
        # line. Tracking "which tc/step is this block for" across those two
        # lines is the only piece of state this parser needs to also surface
        # the model's own stated reason, not just token counts.
        pending_llm_response: tuple[str, int] | None = None
        # The LLM REQUEST (prompt) block (see console.llm_io) is multi-line
        # free text, not one JSON line -- buffered until its own closing
        # separator, then sent as a single event with the whole prompt,
        # so the dashboard can show a developer EXACTLY what the model was
        # given, not just the short reason it gave back.
        pending_llm_prompt: tuple[str, int] | None = None
        prompt_lines: list[str] = []
        while True:
            raw = await proc.stdout.readline()
            if not raw:
                return
            try:
                line = raw.decode("utf-8", errors="replace").rstrip("\r\n")
            except Exception:
                continue
            if not line:
                continue
            await websocket.send_json({"type": "log", "text": line})

            if pending_llm_prompt is not None:
                if _RE_BLOCK_SEPARATOR.match(line):
                    tc, step = pending_llm_prompt
                    pending_llm_prompt = None
                    await websocket.send_json({
                        "type": "llm_prompt", "tc": tc, "step": step,
                        "prompt": "\n".join(prompt_lines),
                    })
                    prompt_lines = []
                    continue
                if pipe_match := _RE_PIPE_LINE.match(line):
                    prompt_lines.append(pipe_match["content"])
                    continue
                # Anything else means this wasn't really the block (shouldn't
                # happen given console.llm_io's own fixed format) -- stop
                # waiting rather than silently attributing unrelated lines.
                pending_llm_prompt = None
                prompt_lines = []

            if header := _RE_LLM_PROMPT_HEADER.search(line):
                pending_llm_prompt = (header["tc"], int(header["step"]))
                prompt_lines = []
                continue

            if pending_llm_response is not None:
                if match := _RE_LLM_RESPONSE_LINE.match(line):
                    tc, step = pending_llm_response
                    pending_llm_response = None
                    try:
                        parsed = json.loads(match["json"])
                    except json.JSONDecodeError:
                        parsed = {}
                    await websocket.send_json({
                        "type": "llm_reason", "tc": tc, "step": step,
                        "reason": parsed.get("reason", ""), "index": parsed.get("index"),
                        "confidence": parsed.get("confidence"),
                    })
                    continue
                # A separator/blank line between the header and the JSON is
                # expected; anything else means this wasn't the block after
                # all -- stop waiting for it rather than mis-attributing a
                # later, unrelated line.
                if line.strip() and not line.strip().startswith("-"):
                    pending_llm_response = None

            if header := _RE_LLM_RESPONSE_HEADER.search(line):
                pending_llm_response = (header["tc"], int(header["step"]))
                continue

            event = _parse_line(line)
            if event is not None:
                await websocket.send_json(event)

    async def listen_for_stop() -> None:
        nonlocal stopped_by_user
        while True:
            msg = await websocket.receive_text()
            try:
                data = json.loads(msg)
            except json.JSONDecodeError:
                continue
            if data.get("type") == "stop":
                stopped_by_user = True
                _kill_process_tree(proc.pid)
                return

    try:
        stdout_task = asyncio.ensure_future(stream_stdout())
        stop_task = asyncio.ensure_future(listen_for_stop())
        done, pending = await asyncio.wait(
            {stdout_task, stop_task}, return_when=asyncio.FIRST_COMPLETED
        )
        for task in pending:
            task.cancel()
        await proc.wait()
        # Re-read the workbook now that the CLI has written results back --
        # the authoritative final state, not anything reconstructed from
        # parsed log lines.
        final_cases = _read_test_cases(app_key)
        await websocket.send_json({"type": "final_state", "test_cases": final_cases})
        await websocket.send_json(
            {"type": "run_finished", "exit_code": proc.returncode, "stopped": stopped_by_user}
        )
        await websocket.close()
    except WebSocketDisconnect:
        if proc.returncode is None:
            _kill_process_tree(proc.pid)
    finally:
        if proc.returncode is None:
            _kill_process_tree(proc.pid)


app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")


if __name__ == "__main__":
    import uvicorn

    uvicorn.run("dashboard.server:app", host="127.0.0.1", port=8000, reload=False)
