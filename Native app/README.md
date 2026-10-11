# healtest

AI-assisted, self-healing, application-agnostic web test automation (Assignment 4).
Test steps come from Excel, run in a real browser, and results are written back to the same workbook.
**Learn once, replay for free, heal only when needed.**

## Setup

```
python -m venv .venv && .venv\Scripts\activate
pip install -e .[dev]
copy .env.example .env        # then put your OPENAI_API_KEY in .env
```

## Run

```
healtest run --suite AI_Assignment_Test_Cases.xlsx --app automationexercise      # visible browser
healtest run --suite suites/saucedemo_suite.xlsx --app saucedemo --slow 1         # second application, slowed down
healtest run --suite AI_Assignment_Test_Cases.xlsx --app automationexercise --headless
python run_tests.py --suite AI_Assignment_Test_Cases.xlsx --app automationexercise   # same thing
healtest evaluate --suite AI_Assignment_Test_Cases.xlsx --runs 3 --break-elements 5  # Assignment 4 metrics
```

The browser is visible unless you pass `--headless`. Use `--slow N` to pause N seconds after every action.
Close the workbook in Excel/LibreOffice first; if it is locked, results go to `<suite>_results_<run>.xlsx`.

## Element store (per application and environment)

```
healtest store --app automationexercise list [--page Login]
healtest store --app automationexercise export -o reports/store.json
healtest store --app automationexercise invalidate --key <key> | --page Login | --plans
healtest store --app automationexercise review            # healed references waiting for approval
healtest store --app automationexercise approve --key <key>   (or --all)
healtest store --app automationexercise reject  --key <key>
```

## How a step runs

1. The step text is turned into atomic actions by the LLM **once** and the plan is stored (no test data values are sent).
2. Each target element is resolved: **cache** (stored locator resolves to exactly one visible element matching the
   stored fingerprint) -> **fallback** (other stored strategies, then fingerprint matching; 0 tokens) ->
   **AI** (compact list of the page's visible elements; the answer is verified deterministically) ->
   **store / heal** (versioned; a healed reference stays *pending* until a reviewer approves it, unless
   `self_healing.auto_approve` is true) -> **refuse to guess** (step is Blocked).
3. Expected results are checked deterministically against what the page showed. A mismatch is **Fail**; the AI never
   takes part.

## Adding an application

Create `config/apps/<name>.yaml` (URL, test account, login flow, optional register flow, `allow_payment_attempts`),
add its host to `security.allowed_hosts` in `config/config.yaml`, and run. No core code changes.
See `config/apps/saucedemo.yaml` for a minimal example.

## Logs

- `logs/llm_usage.jsonl` - one line per LLM call: timestamp, user_id, agent_name, run_id, test case, step, reason
  (first_learning / healing), model, input/output tokens (from the API response), cost, latency, status.
- `logs/run_history.json` - one record per run: date, suite, counts, tokens, cache hit rate, heals.
- `reports/report_<run>.txt|json` - final report of each run.

Compliance with the assignment and the evaluation results: see `ASSIGNMENT4_COMPLIANCE.md`.
