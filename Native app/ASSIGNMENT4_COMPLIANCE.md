# Assignment 4 - compliance report

## What was wrong and what changed

The first version did not follow the assignment's workflow:

| Assignment requirement | What the old code did | What it does now |
|---|---|---|
| Element flow: cache, fallback, AI, store/heal, refuse | Tried hardcoded XPaths and keyword matching first; the LLM was almost never called, so nothing was learned (0 tokens on every run, empty store) | `healtest/resolver.py` implements exactly the 5-step flow; there are no built-in selectors |
| No application-specific code in the core | `element_selectors.py`, `/login`, `data-qa` ids, form scopes, add-to-cart logic in `test_executor.py` | Core is generic; everything application-specific is in `config/apps/<name>.yaml` |
| Element store per app and environment, inspectable, exportable, invalidation | One flat file, no CLI | `<store>/<app>/<env>/elements.json`; `healtest store list/export/invalidate` (one entry, a page, or the cached plans) |
| Healing with review | `auto_approve` setting existed but did nothing | Healed reference is saved as *pending*; old one stays active; `store review/approve/reject`; `auto_approve: true` promotes immediately; version history kept |
| Verification integrity | Heuristic text check (kept) but never demonstrated | Deterministic phrase check, AI not involved; mismatches are Fail (`suites/integrity_checks.xlsx`) |
| Token logging from API usage fields | Cost estimated, no user/agent | `logs/llm_usage.jsonl` with user_id, agent_name, run, test, step, reason, model, tokens from `response.usage`, cost, latency, status |
| Run history | Not kept | `logs/run_history.json` |
| Safe use of the environment | `mask_*` and `allow_payment_attempts` config values were never read; no allow-list | URL allow-list on every navigation and after clicks; payment guard (only approved test cards; refused when `allow_payment_attempts` is false); masking of secrets/personal data in logs, LLM input and screenshots; navigation rate limit |
| Packaging | Dockerfile pointed at the wrong API key variable | `pyproject.toml` (`healtest` command) and a corrected Dockerfile |
| Second application | Placeholder pointing at example.com | `config/apps/saucedemo.yaml` + `suites/saucedemo_suite.xlsx`, no core changes |
| CLI visibility | `--headless` was parsed and ignored | Visible browser by default, `--headless` works, `--slow N` to watch; every cache lookup, fallback, LLM call (input, output, tokens, latency, cost) and step result is printed |

Design notes:
- The LLM identifies elements by **index in a compact list of visible elements**, not by writing XPath. The framework then generates
  the locators itself (id, test id, CSS, XPath, role+name, label, text) and a fingerprint (tag, text, key attributes, position), and
  verifies the choice before use.
- Natural-language steps are converted to atomic actions once and stored (`plans.json`), so repeat runs spend no tokens on planning.
  Test data values are never sent to the LLM; steps refer to them as `{{Key}}`.
- If the API has no credits or the key is invalid, the run stops at once instead of blocking every step.
- Supported suite format: one row per test case with numbered steps in a cell (the provided suite). A per-step-row format is not supported.
- A test case whose expected result contains no quoted text, amounts or bracketed list has nothing checkable and passes on its steps alone.
- `automationexercise` has `allow_payment_attempts: true` because it is a practice shop with a simulated checkout; only the test cards
  in `config/config.yaml` can be typed. Set it to `false` to refuse payment steps (TC_017 then shows Blocked).

## What has been verified

- `pytest tests` -> 10 passed. This covers allow-list, masking, payment guard, test-data placeholders, expected-text extraction,
  fingerprint similarity, store versioning, review, and environment separation, plus (live site, fake LLM) **learn once -> 0-token
  cache replay -> 0-token fallback when locators break -> heal saved as pending when the element changed -> 0-token replay of the
  pending reference -> refusal to guess at low confidence**.
- Live page check: the compact page view is about 1.3K tokens for the home page and every visible element got a unique locator.
- Run against the real site up to the first LLM call (Excel read, browser, step handling, blocking); then stopped.

## What has NOT been verified yet

The OpenAI account returned `429 insufficient_quota` ("You have no credits remaining"), so **no real LLM call has succeeded with the
new code**. That means these are untested: the step-planner prompt on the 20 real test cases, first-run identification accuracy, the
three-run token reduction, healing against a real model, and the second-application run. No evaluation numbers are reported
because none exist; none are estimated.

Once credits are added, run:

```
healtest evaluate --suite AI_Assignment_Test_Cases.xlsx --runs 3 --break-elements 5
healtest run --suite suites/saucedemo_suite.xlsx --app saucedemo
healtest run --suite suites/integrity_checks.xlsx --app saucedemo        # IC_001 and IC_003 must be Fail, IC_004 Skipped
```

`evaluate` reports tokens per run, cache hit rate, time and cost (and cost per 100 steps), stability across runs, and, with
`--break-elements N`, how many corrupted references were healed, how many were healed correctly and how many were false heals
(compared against the original fingerprint). First-run identification accuracy still needs a manual check of `healtest store list`
against the page; expect planner prompt tweaks after the first real run.
