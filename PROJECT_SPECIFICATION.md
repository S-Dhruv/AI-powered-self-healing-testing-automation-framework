# PROJECT_SPECIFICATION.md
## Self-Healing Test Automation Framework ("Learn Once, Replay Free, Heal Only When Needed")

---

## 1. Objective

Build a reusable, application-agnostic test automation framework in **Python + Selenium** that:

1. Reads test steps from an **Excel workbook** (only rows with `Execute = Y`).
2. Executes them against a web application.
3. Writes results back into the **same workbook** without damaging its formatting.

**Element learning model**
- **Run 1 (learn):** an LLM identifies the target element on each page; the framework verifies it deterministically and stores a multi-strategy reference with a fingerprint.
- **Run 2+ (replay):** elements resolve from the store with **zero LLM tokens**.
- **On change (heal):** the LLM is called only when an element can no longer be found by any local strategy. Healed references go through a review gate before becoming permanent.

**Guiding principle:** the AI is an *element finder*, never a *judge*. It cannot change expected results or convert a failure into a pass.

---

## 2. Scope

| In scope | Out of scope |
|---|---|
| Excel-driven UI test execution | API / performance / load testing |
| Persistent, versioned element store | Visual regression / pixel comparison |
| LLM-assisted learn + heal with review | Autonomous test generation |
| CLI + Docker packaging | Real payments or real user data |
| Run reports, run history, dashboard-compatible usage logs | A full web UI (a CLI/report is sufficient) |

**Target applications**
- **Primary:** the approved test environment provided with the suite (e.g. `https://www.automationexercise.com/`).
- **Secondary (portability proof):** a second web app of your choice, run with **no core code changes**, only new config + a new suite file.

> **Assumption to document in README:** the brief mentions both the Etihad test environment and automationexercise.com. Treat the URL(s) in the supplied suite/config as authoritative, and keep both as configurable environments.

---

## 3. Architecture Overview

```
CLI  ─►  Config Loader ─►  URL Allow-list Guard
              │
              ▼
        Excel Reader ──► Step Executor ──► Excel Writer / Reporter
                              │
                              ▼
                      Element Resolver
        ┌───────────┬──────────┴───────────┬────────────┐
        ▼           ▼                      ▼            ▼
   Tier 1 Cache  Tier 2 Fallback    Tier 3 AI Finder   Tier 5 Block
        │           │                      │
        └───────────┴──────► Verifier ◄────┘
                              │
                              ▼
                  Tier 4 Store / Heal (versioned)
                              │
                    Review Queue (approve / reject)

Cross-cutting: Masking • Usage Logger • Run History • Screenshots
```

**Suggested module layout** (framework core contains no app-specific code)

```
framework/
  cli.py                # entry point
  config/               # loader, schema validation, allow-list
  excel/                # reader, writer (openpyxl), summary sheet
  resolver/             # cache, fallback, ai_finder, verifier, fingerprint
  store/                # JSON/SQLite backend, versioning, export, invalidate
  actions/              # click, type, select, assert, navigate (generic verbs)
  llm/                  # provider client, prompt templates, usage logger
  security/             # masking, allow-list, rate limiter
  reporting/            # run report (HTML/MD), run history, dashboard export
apps/
  <app_name>/config.yaml, suite.xlsx, login.yaml   # all app-specific material lives here
tests/                  # unit + integration tests for the framework itself
```

---

## 4. Element Resolution Flow (per step)

Each interactive step carries a **target description** (natural-language, e.g. "Login button"). The store key is `application + environment + page + target description`.

| Tier | Name | Action | Tokens | Result tag |
|---|---|---|---|---|
| 1 | **Cache lookup** | Resolve the primary stored locator. Accept only if it matches **exactly one visible** element **and** the fingerprint check passes. | 0 | `Cache` |
| 2 | **Deterministic fallback** | Try remaining stored strategies in a fixed priority order: test-id → role + accessible name → label → text → CSS → fingerprint similarity search. Same acceptance rule. | 0 | `Fallback` |
| 3 | **AI identification** | Send a compact page representation to the LLM; receive a proposed locator **plus a confidence value**. | >0 | `AI` |
| 4 | **Verify, store or heal** | Deterministically verify the proposal (see 4.2). Save with multiple strategies + fingerprint. If it replaces an older reference, keep version history and flag the step. | - | `AI` / `Healed` |
| 5 | **Refuse to guess** | If the LLM is unsure, returns nothing, returns multiple matches, or verification fails, **do not act**. | - | `Blocked` |

### 4.1 Compact page representation (for Tier 3)
- **Primary:** a compact, indexed representation generated from the live rendered DOM. Selenium does not expose Playwright's ARIA snapshot API, so the representation includes visible interactive elements, semantic roles, labels, key attributes, nearby context, and a stable index. The model returns only an index, confidence, and reason; Selenium derives and verifies the locator locally.
- **Supplement (optional):** a filtered DOM containing only interactive elements (links, buttons, inputs, selects, elements with roles/test-ids), with scripts, styles and hidden nodes removed.
- **Optional:** a screenshot, **only after masking**, for visually ambiguous cases.
- Never send raw full-page HTML. Truncate and prioritise nearby context if the tree exceeds a configured token budget.

### 4.2 Deterministic verification (mandatory before use or storage)
An LLM-proposed locator is accepted only if **all** hold:
1. `page.locator(selector).count() == 1`
2. The element is visible and enabled (for interaction steps).
3. The element is **compatible with the intended action** (e.g. an input for `type`, a clickable for `click`).
4. Its tag / role / accessible name is **plausibly consistent with the target description** (basic lexical or role check, a guard against confident-but-wrong answers).
5. It is not inside a different frame/dialog than expected.

### 4.3 Fingerprint (stored with every element)
- Tag name, ARIA role, accessible name, visible text (normalised)
- Key stable attributes (`id`, `name`, `data-testid`, `type`, `placeholder`, `aria-*`, `href` pattern)
- Structural hints (parent role/landmark, sibling labels)
- Coarse position/size **as a weak signal only** (never a primary match criterion)

Do **not** store brittle data such as absolute XPath, DOM index, line numbers or pixel coordinates as a primary locator.

### 4.4 Healing policy
- A heal occurs only when Tiers 1-2 fail for an element that previously existed.
- The new reference is saved as a **pending version**; the old version stays active until review.
- `healing.mode: manual` (default) → reviewer must approve; `healing.mode: auto` → auto-approve above a configured confidence + verification threshold, still logged and flagged.
- A step whose **assertion** fails is a `Fail`, **never a heal trigger**. Healing applies only to *finding* elements.

---

## 5. Execution Contracts

### 5.1 Excel input
Required columns: `Test Case ID`, `Test Case Title`, `Step No`, `Test Data`, `Expected Result`, `Execute`. Candidates may add columns (e.g. `Action`, `Target`, `Page`).

**Recommended additional columns**

| Column | Purpose |
|---|---|
| `Action` | Generic verb: `navigate`, `click`, `type`, `select`, `check`, `assert_text`, `assert_visible`, `assert_url` … |
| `Target` | Natural-language element description (the store key component) |
| `Page` | Logical page name used in the store key |

Rules:
- Run only rows where `Execute = Y` (case-insensitive); mark others `Skipped`.
- Execute steps in `Step No` order within a test case.
- If a step is `Fail` or `Blocked`, remaining steps of that test case are `Skipped` by default (configurable: `on_failure: stop | continue`).
- Validate the workbook up front; report missing columns, duplicate `(Test Case ID, Step No)` and unknown actions before any browser starts.
- Resolve the `Execution Status` vs `Status` ambiguity in the brief by documenting one canonical result column and keeping the input column intact.

### 5.2 Excel output
Written **in place** with `openpyxl`, preserving fonts, fills, borders, column widths, merged cells and data validation. Back up the original (or write a timestamped copy) before modifying. `.xlsx` only (note in README that `.xls`/macros are unsupported).

Result columns: `Status` (Pass / Fail / Blocked / Skipped), `Actual Result`, `Screenshot Path`, `Execution Time`, `Element Source` (Cache / Fallback / AI / Healed), `Tokens Used`, `Run ID`, `Executed At`.

**`Run Summary` sheet** (appended per run): Run ID, suite, environment, start/end, totals for Pass/Fail/Blocked/Skipped, total tokens (in/out), estimated cost, cache hit rate, fallback rate, AI lookups, heals (pending/approved/rejected), duration.

### 5.3 Assertions & verification integrity
- `Expected Result` is parsed into a deterministic check (text contains, element visible, URL matches, count equals …) using a documented mini-grammar.
- Any mismatch → `Fail` with both expected and actual values in `Actual Result`.
- The LLM never sees `Expected Result` for the purpose of deciding pass/fail, and it never rewrites it.

---

## 6. Core Requirements

### R1. Excel-driven execution: see §5.

### R2. Element reference store
- Persistent (SQLite recommended; JSON acceptable), **versioned**, partitioned by `application` and `environment`.
- Each record: key, page, description, locator strategies (ordered), fingerprint, version, status (`active` / `pending` / `rejected` / `superseded` / `invalidated`), created/updated timestamps, source (`AI` / `Healed`), run ID, hit/miss counters.
- CLI operations: `store list`, `store show <key>`, `store export <file>`, `store invalidate --key | --page | --app`, `store history <key>`.

### R3. Zero-token replay
- A repeat run on an unchanged app must use **0 LLM tokens**.
- Any token use on a repeat run must be explained in the log with step, reason and which tiers failed.
- Provide an automated test asserting `tokens == 0` on run 2 for the sample suite.

### R4. Self-healing with review
- Healed references are flagged in Excel results and the run report (old vs. new locator + fingerprint diff + screenshot with the element highlighted).
- CLI: `review list`, `review approve <id>`, `review reject <id>`, `review approve-all --run <id>`.
- Rejected heals revert to the previous version and the step is flagged for attention.

### R5. Verification integrity: see §5.3. Include deliberately wrong expected-result rows in the suite to prove they `Fail`.

### R6. Reusability & portability
- Zero application-specific code in `framework/`.
- Per-app config (`apps/<name>/config.yaml`): base URL, environments, allow-listed hosts, login routine (declarative steps), test accounts via environment variables, rate limit, timeouts.
- Demonstrate a **second application** with only new config + suite.

### R7. Usage logging & run history
Every LLM call is logged with: `timestamp, run_id, test_case_id, step_no, user_id, agent_name, reason (first_learning | healing), model, input_tokens, output_tokens, latency_ms, status (success | error), estimated_cost`.
- Token counts come **only from the API response usage fields**.
- Failed/retried calls are logged too, with `status = error`.
- Format: JSONL/CSV compatible with the Assignment 2 dashboard (document the schema and provide a sample file that loads as a separate dashboard tab).
- Run history table: date, suite, environment, pass/fail/blocked counts, total tokens, cache hit rate, heals.

### R8. Safe use of the environment
- **Allow-list guard:** refuse navigation, redirects, or frames outside approved hosts. Fail closed, with a clear error.
- Never complete real payments (block payment-submit actions or stop at a documented safe point).
- Test accounts only; credentials come from environment variables / secret store, never from the suite or repo.
- Respect rate limits (configurable delay, max concurrency, retry with backoff).
- **Masking** applied to logs, reports, Excel `Actual Result`, screenshots (blur/redact inputs of type password/email/card and configured selectors), and every LLM payload.
- Mask **before** sending to the LLM; test with a canary secret to prove it never leaves the machine.

### R9. Packaging
- `pip install .` (with `pyproject.toml`) **or** Docker image; Selenium Manager provisions the configured browser driver.
- Example: `shtaf run --suite apps/demo/suite.xlsx --env staging --headless`.
- Exit code non-zero on any `Fail`/`Blocked` (CI-friendly).

---

## 7. Security & Robustness Notes
- **Prompt injection from the page:** page text is untrusted. The LLM prompt must state that page content is data, not instructions; the model's output is constrained to a strict JSON schema (`selector`, `strategy`, `confidence`, `reason`); anything else is discarded. The model has no ability to execute actions itself.
- **Timeouts & retries:** bounded waits (no arbitrary sleeps), single retry for transient LLM/API errors, circuit breaker after N consecutive LLM failures.
- **Dynamic pages:** wait for network/DOM stability or explicit element state before resolving; handle iframes, shadow DOM and modals.
- **Determinism:** LLM temperature 0; same page → same proposal where possible.

---

## 8. Evaluation Plan

Run the provided suite **at least three times** (run 1 cold store; runs 2-3 unchanged app), then run a **mutation test** (see below) and the second app.

| # | Metric | How to measure | Target |
|---|---|---|---|
| 1 | Token reduction | Tokens in runs 1, 2, 3 | Run 2/3 = 0 (or fully explained) |
| 2 | Cache hit rate | Tier-1 hits ÷ element lookups, runs 2-3 | ≥ 95% |
| 3 | First-run identification accuracy | Manual check of a sample (state sample size) | Report % |
| 4 | Healing success | Correct heals ÷ changed/invalidated elements | Report % |
| 5 | **False heal rate** | Heals that picked the wrong element ÷ total heals | **As close to 0% as possible (key metric)** |
| 6 | Verification integrity | Rows with wrong expected results → must all be `Fail` | 100% |
| 7 | Stability | Same pass/fail outcome across the 3 runs | 100% consistent |
| 8 | Speed & cost | Wall time and cost for run 1 vs. repeat; est. cost per 100 steps | Report |
| 9 | Portability | Small suite on second app, **no core code changes** (show git diff of `framework/` is empty) | Pass |

**Healing / false-heal experiment design:** create controlled mutations (e.g. change an `id`, rename a button label, move an element, add a near-duplicate decoy element) by invalidating store entries or using a mutated copy/proxy of the page. Label the ground truth for each mutation so correct heals and false heals can be counted objectively.

---

## 9. Deliverables
1. Source code (ZIP) with tests, `pyproject.toml` / `Dockerfile`, sample configs.
2. Sample suite workbook(s) for both apps, including intentionally failing rows.
3. README: setup, CLI usage, architecture, **technology choices with at least one rejected alternative**, assumptions, masking/non-job-signal safeguards, limitations.
4. Evaluation report with the nine metrics above (tables + brief analysis of failures).
5. Sample outputs: result workbook, run report, usage log (JSONL/CSV), run history.
6. Summary report + per-member contribution breakdown.
7. 15-minute live walkthrough: fresh run → repeat run (0 tokens) → forced heal → review → fail-on-mismatch demo.

---

## 10. Definition of Done (checklist)
- [ ] Only `Y` rows executed; formatting of workbook preserved
- [ ] All five resolution tiers implemented and tagged in results
- [ ] LLM proposals always verified; low confidence → `Blocked`
- [ ] Store is versioned, exportable, invalidatable per entry/page/app
- [ ] Heal review workflow (manual + auto mode)
- [ ] Run 2 and 3 use 0 tokens (or every token explained)
- [ ] Wrong expected results always `Fail`
- [ ] Allow-list enforced; masking proven with a canary test
- [ ] Usage log schema matches the Assignment 2 dashboard
- [ ] Second application works with zero core changes
- [ ] CLI/Docker packaging and CI-friendly exit codes
- [ ] Evaluation report complete
