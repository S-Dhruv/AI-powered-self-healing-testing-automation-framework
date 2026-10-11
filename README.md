# Self-Healing Test Automation Framework (shtaf)

*"Learn once, replay for free, heal only when needed."*

A Selenium-based UI test framework that resolves elements the way a human tester does: find it, remember exactly how you found it, and only ask an LLM again when the page has genuinely changed. It is driven entirely by Excel test-case workbooks and app-specific YAML config — the core engine has no knowledge of any one website baked into it.

This document covers the tech stack, the architecture and why it's shaped the way it is, the end-to-end execution flow, the real problems hit while building and proving it against two different live websites, and how to run it.

---

## 1. Tech stack

| Concern | Choice | Why |
|---|---|---|
| Language | Python 3.10+ | Type-hinted throughout, formatted with `black`/`ruff` |
| Browser automation | Selenium 4.x | Direct WebDriver control; no auto-wait abstraction hiding timing issues from the resolver |
| LLM | OpenAI SDK, model `gpt-6-luna` (default, overridable via `--model`) | Structured JSON output, temperature 0 for every model that supports it (`gpt-6-luna` only accepts its default temperature, so the client omits the parameter for it specifically), used only as an element finder (never a judge) |
| Excel I/O | `openpyxl` only | Preserves fonts, fills, borders, widths, merged cells, data validation. `pandas.to_excel()` is explicitly forbidden by this project's own spec — it would silently discard all formatting on every write |
| Config validation | `pydantic` v2 | App config (`apps/<name>/config.yaml`) and process settings (`.env`) are both schema-validated at startup, not discovered as runtime errors mid-suite |
| Element store | SQLite | A versioned, queryable store of "how we last found this element," partitioned by application + environment + page + description |
| Config format | YAML (app config) + `.env` (secrets/process settings) | App-specific knowledge and framework-wide secrets are deliberately different files, never mixed |
| Tests | `pytest` + `pytest-mock` | The framework's own test suite mocks the LLM and Selenium entirely — 72 tests run in under a minute with no browser or network involved |

```toml
dependencies = ["selenium", "openpyxl", "pydantic", "pyyaml", "openai", "python-dotenv"]
```

---

## 2. The core idea: application-agnosticism, enforced structurally

`framework/` contains **zero knowledge of any specific website** — no hardcoded URLs, selectors, field names, or business logic. Every resolver, verb, and action module takes the application's base URL, allow-listed hosts, login routine, and interaction quirks as *parameters* passed in from `apps/<name>/config.yaml` at runtime, never as literals.

This isn't just a convention — it was load-bearing. Partway through this project a second, structurally unrelated application (originally saucedemo.com — a React SPA with session-based logins, vs. automationexercise.com's server-rendered forms with open registration) was added by writing exactly two things:

1. `apps/<name>/config.yaml` — base URL, allowed hosts, a declarative login routine (if any), which env vars hold its test credentials, and any interaction quirks specific to that site
2. `<App>_Test_Cases.xlsx` — a workbook in the same format as the original

**Zero lines changed in `framework/`'s actual resolution/verification/step logic to support it.** The only framework-level additions that came out of adding this second app were two genuinely generic robustness fixes (§6.4) — not anything specific to that app's markup.

saucedemo.com was later swapped out for **bbc.com** (§8) — a real, popular site rather than a QA-practice demo — again by only touching those same two things, `apps/bbc/config.yaml` and `BBC_Test_Cases.xlsx`. §6.4's debugging history below is kept exactly as it happened (the bugs were real and the fixes are now permanent, generic framework behavior, independent of which app is currently the "second" one); the two-app comparison in §8 and the setup instructions in §9 reflect the current pair.

---

## 3. Architecture

### 3.1 Module map

```
framework/
├── cli.py                 entry point: loads config, drives the per-row loop, writes results
├── browser.py              Selenium facade: locators, click/fill/select, cookie/tab reset
├── executor.py             runs one test case: dispatches sub-steps to the resolver + verbs
├── synonyms.py             generic concept synonyms (e.g. "keyword"<->"search", "x"<->"close")
│
├── config/
│   ├── schema.py           pydantic models: AppConfig, EnvironmentConfig, Settings
│   ├── loader.py           loads + validates apps/<name>/config.yaml and .env
│   └── allowlist.py        fails closed on any navigation outside allowed_hosts
│
├── excel/
│   ├── reader.py           validates + reads the Test Cases sheet
│   ├── step_parser.py      free-text "Test Step" cell -> ordered SubStep actions
│   ├── expected_parser.py  "Expected Result" cell -> deterministic checks (never LLM-judged)
│   └── writer.py           writes results back in place; backs up the workbook first
│
├── resolver/                           the 5-tier resolution pipeline
│   ├── resolve.py          orchestrates tiers in fixed order; never skips ahead
│   ├── cache.py            Tier 1: replay a stored strategy, verify, done
│   ├── fallback.py         Tier 2: try this record's other stored strategies
│   ├── ai_finder.py        Tier 3: DOM -> indexed element list -> LLM -> index -> selector
│   ├── verifier.py         Tier 4 (always): count==1, visible, action-compatible, plausible
│   └── fingerprint.py      what "the same element" means across page loads
│
├── store/
│   ├── backend.py          SQLite-backed versioned element store
│   ├── models.py           ElementRecord / Strategy / Fingerprint
│   ├── review.py           pending-heal approval queue
│   └── account_cache.py    persists one working test account across rows/runs
│
├── llm/
│   ├── client.py           OpenAI call wrapper; structured JSON, temperature 0
│   ├── prompts.py          the element-finder system prompt (never sees Expected Result)
│   └── usage_logger.py     JSONL log: tokens, cost, latency, reason, per the spec's schema
│
├── actions/
│   ├── verbs.py            click/type/select/check/upload/accept_dialog primitives
│   └── ad_utils.py         CDP-level ad/tracker request blocking + overlay dismissal
│
├── security/masking.py      credential/PII masking for logs, reports, screenshots, LLM payloads
└── reporting/                console narration, run-history DB, run reports, dashboard export
```

### 3.2 The 5-tier resolution pipeline

This is the center of the design. Every element lookup goes through the *same* fixed order — the spec requires it and the code enforces it structurally (`resolve_element` in `resolve.py` is the only entry point; nothing calls the LLM directly):

```
Tier 1  Cache       Replay the stored primary strategy for this (app, env, page, description).
                    Verify it. One match, visible, action-compatible, fingerprint-consistent?
                    -> use it. 0 tokens.
                    ↓ miss
Tier 2  Fallback    Try this same record's OTHER stored strategies, in order. Verify each.
                    -> use the first that passes. Still 0 tokens.
                    ↓ miss
Tier 3  AI          Walk the visible, interactive DOM into a compact indexed list (never raw
                    HTML). Ask the LLM for {index, confidence, reason}. Derive a selector
                    locally from the chosen element -- the model never writes a selector itself.
                    ↓
Tier 4  Verify      ALWAYS, regardless of which tier produced the selector: count()==1,
                    visible, action-compatible with the requested verb, plausibly consistent
                    with the description's own words, fingerprint-consistent if healing.
                    Fails -> retry Tier 3 once with corrective feedback, or Blocked.
                    ↓ passes
Store/Heal          First time seeing this element -> store active immediately.
                    Healing (a prior version existed) -> store as PENDING; the old version
                    stays active and usable until a human approves the new one (unless
                    healing.mode: auto).
                    ↓
Refuse              Low confidence, ambiguous, or failed verification -> step is Blocked
                    with a reason. Never guessed, never silently passed.
```

**Why this order, and not "try AI first, cache the result"**: the spec's "zero-token replay" requirement means a *repeat* run against an unchanged app must cost 0 LLM tokens. That's only possible if the cheap, deterministic tiers are tried first and the AI tier is the genuine last resort — not an optimization applied after the fact.

**Why Tier 4 (verify) runs even when the AI *didn't* produce the selector**: a cached or fallback strategy can go stale too (the element moved, got removed, or now matches something else). Verification isn't "trust the AI less" — it's "never act on anything unverified," cache included.

### 3.3 What makes two elements "the same" — fingerprinting

A `Fingerprint` (tag, role, accessible name, visible text, a curated set of stable attributes, parent landmark) is captured whenever an element is first stored. On every later resolution, the *candidate's* fingerprint is compared against the *stored* one with a configurable similarity threshold. This is what lets the cache tier detect "this selector still matches something, but it's not the same logical element anymore" instead of blindly trusting a selector match.

Two things the fingerprint deliberately does *not* include, because they're the definition of a brittle locator the spec forbids as a primary strategy: absolute XPath, raw DOM index, and pixel coordinates.

### 3.4 Two grammars, both deterministic, both LLM-free

- **Step grammar** (`step_parser.py`): turns a free-text "Test Step" cell like `"Enter registered email and correct password under 'Login to your account'"` into ordered `(action, target, context)` sub-steps via pattern matching — verb detection, quoted-phrase extraction, connector words (`under`/`in`), compound-line splitting (`"X and go to Y"`, `"X, click 'Y'"`). The LLM never parses test steps.
- **Expected-Result grammar** (`expected_parser.py`): turns `"'ORDER PLACED!' is shown; redirected to /login"` into typed checks (`text_visible`, `url_contains`) evaluated deterministically by Selenium/Playwright-style assertions against the live page. **The LLM never sees this column and never judges pass/fail** — a mismatch between expected and actual is always `Fail`, never a heal trigger, never silently converted to a pass. The sample suite deliberately includes rows with a wrong expected value to prove this (§5).

---

## 4. End-to-end flow: what happens when you run a suite

```
shtaf run --suite X.xlsx --app-config apps/Y/config.yaml --env test
```

1. **Load & validate.** `.env` -> `Settings`, `apps/Y/config.yaml` -> `AppConfig`, both pydantic-validated. Workbook headers checked against required columns before any browser opens.
2. **Launch one browser** for the whole run (Chrome or Firefox, ad-network CDP blocking applied per `blocked_ad_url_patterns`).
3. **For each row** (`Execute == Y`):
   a. **Reset session**: open a fresh tab, close the old one, clear cookies (via CDP — see §6.4), clear local/session storage, navigate to the app's base URL. Every row starts anonymous and logged-out unless the row itself logs in.
   b. **Parse** the row's `Test Step` cell into sub-steps.
   c. **For each sub-step**: resolve the target element through the 5-tier pipeline, verify, perform the action (`verbs.click`/`type_text`/`select_option`/...), log the outcome.
   d. **Evaluate `Expected Result`** once, deterministically, against whatever page the row ended on.
   e. Record Pass / Fail / Blocked, screenshot on failure, tokens used, element source, timing.
4. **Write results back into the same workbook**, in place, after backing it up with a timestamped copy. New result columns are appended if the template didn't have them; nothing else in the sheet is touched.
5. **Append a Run Summary** sheet entry and a markdown run report (`artifacts/reports/run-<id>.md`) with totals, token cost, cache-hit rate, heals.

---

## 5. Key architecture decisions and why

- **The LLM is only ever an element finder.** It's never shown `Expected Result`, never asked "did this pass," and its output is constrained to `{index, confidence, reason}` — not a selector. The framework derives the actual selector locally from whichever live element the index pointed to, and independently verifies it. This closes off an entire class of failure where a confidently-wrong model could silently convert a real bug into a reported pass.
- **Healing never overwrites silently.** A new reference for an element that already has one is stored as a new *version*, status `pending`; the old version stays active and usable until approved (or auto-approved, if configured). Version history is kept, not replaced.
- **No brittle locators as primary strategies.** Absolute XPath, DOM index, and pixel coordinates are structurally excluded from what gets stored — only `test_id`, `role_name`, `label`, `text`, or a scoped CSS strategy.
- **Everything app-specific is data, not code** (§2). This is what made the second-app addition a config + workbook exercise instead of a fork.
- **Interaction *mechanism* choices are per-app config, not framework defaults, once they're proven to trade off against each other.** `click_strategy: native | action_chains` is the clearest example (§6.4): `action_chains` fixed real, confirmed click-reliability issues on saucedemo's React controls, but its pointer movement also triggers CSS `:hover` states that regressed automationexercise's hover-reveal product grid when tried as a global default. There is no mechanism proven safe for every site's UI — so it's a declared per-app choice, the same way login steps or allowed hosts are.
- **Every shared-code change is validated against *both* live apps before being trusted**, not just the one it was written for. Several fixes in this project looked correct in isolation and regressed the other app; each regression was caught this way before being called "done," and each such bounce is documented in §6 rather than quietly dropped.
- **Masking is applied before anything leaves the process** — credentials and PII are stripped from logs, reports, screenshots, and every LLM payload, with a canary-secret test proving it.
- **Rate limiting is config-driven, not improvised.** `rate_limit_delay_ms` is read once per row in the main loop, never as an ad-hoc `sleep()` buried in step logic.

---

## 6. Problems faced, and how they were actually solved

This section groups the real debugging history by theme rather than by date. Every fix below was found by reproducing the failure live (direct Selenium diagnostics against the real site), not by guessing from a stack trace, and every framework-level fix was re-validated against both applications before being kept.

### 6.1 Element disambiguation (the largest category by far)

The most common real bug was never "the model picked the wrong element" in isolation — it was the framework giving the model (or the verifier) *too little information to tell two real candidates apart*.

- **Identical-text repeated controls.** Every product card's "Add to cart" button reads exactly "Add to cart." Fixed by adding a `context=` field to the element list: the immediate parent's residual text after the element's own text is removed (so a specific card's button carries its product name/price). The model's own stated reasoning confirmed it working: *"has a context that specifically mentions 'Men Tshirt.'"*
- **Same attributes, different form.** A login email field and a signup email field had identical name/type/placeholder — the only difference was a custom `data-qa`/`data-test` attribute neither the fingerprint nor the selector-builder originally captured. Fixed by generically scanning *all* `data-*` attributes (not a hardcoded allowlist) into both the fingerprint and the selector strategy.
- **Hover-reveal duplicate markup.** A product grid rendering a hidden overlay copy of "Add to cart" alongside the normal one meant the AI's chosen index sometimes resolved to whichever copy was invisible at that instant. Fixed with `_find_visible_twin()`: when a resolved element fails the visibility check, re-query by its own shared `data-*` attributes and substitute whichever copy of it actually is visible — threaded back through all three resolver tiers via a `resolved_selector` field so the correct one gets used and stored, not the original.
- **Icon-only controls.** A delete button with no visible text, only `<i class="fa fa-times">`, couldn't be matched against a step that said "click the 'X' icon" — single-character words were filtered out of plausibility matching entirely. Fixed with a narrow exception for `"x"` plus generic icon-name synonyms (`times`/`close`/`remove`/`trash`).
- **An exact text match losing to indirect reasoning.** With "Logout" sitting right there in the element list as `text='Logout'`, the model sometimes picked an unrelated "X" (close icon) link instead via icon-semantics reasoning. Fixed by adding explicit prompt guidance: an exact match between the target's own wording and an element's own text/label is the *strongest* signal available, stronger than any indirect/icon-based reasoning.

### 6.2 Session, account, and grammar gaps

- **Registration fallback.** A login step with no working cached account used to just fail. Added a generic fallback: if login is rejected and the app config declares `registration_steps`, register a fresh account and retry once — using the *same* declarative step engine as login, not a parallel implementation.
- **A stale cache-invalidation that outlived its reason.** Early on, deleting an account on-site also deleted it from the local cache, so a later row needing to re-attempt login with the *same, now-invalid* credentials had nothing to try. Once the registration fallback existed, this invalidation was actively counterproductive — removed.
- **Compound test-step lines.** `"Add product to cart and go to Cart"` and `"Enter comment, click 'Place Order'"` are two actions on one numbered line, which the original grammar parsed as one (wrong) target. Fixed with two compound-line splits (`"... and go to <place>"`, `"..., <verb> '<target>'"`), each rewritten into a synthetic second line and parsed through the *same* code path as a real one — not a parallel implementation. One subtle bug surfaced fixing this: the comma-split was briefly double-counting an unrelated `"On <page>, <verb> ..."` leading clause's own comma, caught by re-parsing every row in the real workbook and diffing step counts before trusting it.
- **Composite form fields named collectively.** `"Enter card details"` covers four real Test Data fields (name, number, CVC, expiry) but was resolving as one generic step. Fixed by extending the existing DOB-style field-group expansion mechanism — including splitting a combined `"12/2028"` expiry value into the form's actual separate month/year inputs, the same way DOB already splits into day/month/year.
- **A `select` naming its option directly in the step text.** `"Select 'Price (low to high)' from the sort dropdown"` has no Test Data label to look up — the value *is* the step text. Fixed with a fallback to the step's own wording when a Test Data lookup finds nothing, mirroring how `check` already treats a radio option named in the step. Paired with a second fix: `select_option` only ever tried `select_by_value`, which fails whenever a site's option *value* attribute doesn't match its visible text (confirmed live: a sort option's value was `"lohi"` for visible text "Price (low to high)") — added a `select_by_visible_text` fallback.

### 6.3 Dead configuration found while chasing something else

Two config fields existed in the schema, read by nothing: `rate_limit_delay_ms` (declared, never applied anywhere) and an earlier `element_aliases` field (added in anticipation of a need that never materialized once the generic `context=`/synonym work covered it, then deleted again rather than left as scaffolding implying a feature that didn't exist). The rate limit was wired up for real once a genuine need for it was found (§6.4); the alias field was removed.

### 6.4 Browser-interaction reliability (the hardest problems, found adding the second app)

Adding saucedemo.com surfaced a class of bug the first app never did: a Selenium action that raises **no exception at all** yet never reaches the page's own event handler. These took the most iteration, including two reverted framework-wide changes, because the right fix turned out to be *different per failure mode* rather than one universal mechanism:

- **Native click intermittently silent.** A page overlay produces a catchable `ElementClickInterceptedException` (already handled with a JS-dispatched-click fallback). But some React controls simply absorbed a native click with no exception and no effect, non-deterministically. Making a JS-dispatched click the *unconditional* default fixed those controls — but broke automationexercise's hover-reveal product grid (JS clicks don't trigger the `:hover` CSS states real pointer movement does) and, separately, broke saucedemo's own login form (a JS-dispatched click didn't reliably trigger that specific `<input type="submit">`'s form-submit semantics, confirmed by reverting and re-testing twice). The fix that actually held up: `ActionChains` (a real pointer move-then-click via the W3C Actions API), exposed as a **per-app config choice** (`click_strategy`), not a framework default — automationexercise stays on `native` (proven, hover-sensitive UI), saucedemo opts into `action_chains`.
- **A React-controlled input silently accepting zero keystrokes.** `send_keys` would leave a field empty — no exception — specifically on any page reached by a navigation other than the session's first. Fixed generically in `fill()`: read the field back after typing, and only if it doesn't match what was sent, fall back to setting the value through the element's native property setter and dispatching real `input`/`change` events (the same mechanism React's own code uses internally).
- **The same-tab navigation itself was the root cause for the worst of it.** Traced, after ruling out click mechanism, typing mechanism, wait duration, and Chrome's back-forward cache one at a time via direct reproduction scripts, to simply *reusing one browser tab* across the per-row session reset — a fresh tab per row fixed it outright (five-for-five logins, vs. consistent failure from the second attempt onward in the same tab, with identical code). Implemented as: open a new tab, close the old one, each row. This surfaced a second bug immediately — clearing cookies on a brand-new `about:blank` tab silently clears nothing, since there's no origin yet to clear cookies *from* — fixed by clearing via `Network.clearBrowserCookies` over CDP (which doesn't require being on the target origin first) instead of Selenium's origin-scoped `delete_all_cookies()`, with a same-origin-navigate-twice fallback for non-Chrome drivers where CDP isn't available.
- **A residual, still-nonzero click-silence rate even with `action_chains`.** The final generalizable fix: every click is now followed by a bounded check for *any* DOM mutation at all (a `MutationObserver` on `document.body`), and only re-fires via a JS-dispatched click if literally nothing changed — never re-firing a click that already had some effect, so it can't double-submit or double-toggle anything that worked the first time. This is deliberately app-agnostic: it doesn't know or guess what a given click *should* do, only whether the page reacted at all.
- **A CSS-transition-revealed menu read before it finished opening.** The existing DOM-stability wait covered network/navigation settling but not a pure CSS-transition reveal with no network activity at all. Added a short, bounded, fixed pause to the same stability-wait function, generic to any animated-reveal UI.

### 6.5 Element visibility: from "does CSS say it's visible" to "is this what a real click would hit"

The single highest-value fix found after the first two apps were already passing. It started as "the AI keeps saying this duplicate 'Add to cart' button has no derivable unique selector" and ended up rewriting how the whole framework decides an element is visible at all.

**Old approach.** The element-indexing walker (the thing that decides which elements even get offered to the AI as candidates) judged visibility purely from computed style: not `display:none`, not `visibility:hidden`, not `opacity:0`, and a non-zero bounding-box size. This is the obvious, textbook definition of "visible" — and it's what the original implementation used.

**What inspecting the real site's own markup showed.** automationexercise.com's product grid renders each product's "Add to cart" link **twice**: once in the always-present card footer, once inside a hover-reveal overlay. Both copies share the exact same tag, the exact same `data-product-id`, the exact same text, and — this is the part the old check missed — the exact same `display`, `visibility`, and `opacity`, and both report a non-zero size. Nothing in computed CSS distinguishes them. The *only* real difference is that one of them is physically stacked underneath the other on screen, so only one is ever what a real mouse click would actually land on — exactly what Selenium's own `is_displayed()` already detects (via its own hit-testing), which is why the bug only showed up in the framework's *own* indexing logic, not in Selenium's native checks.

**New approach.** `isVisible()` now does a real hit-test: `document.elementFromPoint()` at the element's own center, accepting it only if that point resolves to the element itself or one of its own descendants (an icon inside a link, say) — never to an unrelated ancestor container, which is what the hidden twin's point actually resolved to. Two refinements were needed to make this hold up:
- Most products on a long listing page are below the fold when indexing runs. Skipping the hit-test for anything off-screen (to avoid wrongly excluding legitimately-visible-after-scrolling elements) just let every off-screen duplicate back in. Fixed by scrolling each off-screen candidate into view (instant, no animation) before testing it, then restoring the original scroll position once indexing finishes.
- The containment check itself was briefly backwards: accepting "the hit-test landed on an *ancestor* of this element" as well as "landed on this element or its descendant" — the ancestor case is exactly what the truly-hidden twin produces (nothing more specific painted at that point), so accepting it let the bug straight back in from a different angle.

The same gap existed one layer deeper: `selector_for_index()`'s own uniqueness check (`document.querySelectorAll(selector).length === 1`) counted the hidden twin too, since it shares every attribute with the visible one — rejecting an otherwise-perfectly-good selector as "not derivable" even after the walker above stopped offering the hidden copy as a candidate. Fixed by scoping that count to elements the walker already tagged as reachable (`[data-sh-idx]`), reusing the same judgment instead of re-deriving it.

**Why this is better, not just different:** the old check asked "does this element's own style say it's visible," which is the wrong question whenever two elements can both legitimately answer "yes" while only one is reachable. The new check asks the only question that actually matters — "would a real click land here" — which is exactly what the AI and the verifier both need to know, generalizes to *any* site using this common hover-reveal pattern (not an automationexercise-specific patch), and is proven correct against Selenium's own `is_displayed()` for every case tested.

### 6.6 A retry that undid its own fix

The ad-interstitial retry (§6.1/already in place for plain navigation links) turned out to actively break a different control shape: a Bootstrap accordion toggle (`data-toggle="collapse"`). The browser layer's own click-verification (§6.4's MutationObserver check) already self-heals a collapse toggle whose click got hijacked, by re-firing it via a JS-dispatched click that bypasses the overlay entirely — and that JS-dispatched click can succeed *before* the executor-level ad-retry even runs. The ad-retry, seeing the ad's stale URL artifact still present, would then click the *same toggle* again — which, because it's a toggle, closed the panel it had just correctly opened. Net effect: the step still reported `Pass` (every individual click succeeded), but the panel the next step needed open ended up closed anyway, intermittently and non-obviously.

Fixed by checking the toggle's own target-panel state (including mid-CSS-transition, not just its settled `in`/`show` class) before retrying, and skipping the retry-click entirely if the panel is already open or actively opening. The ad artifact is still cleaned up either way; only the redundant, state-reversing click is skipped.

### 6.7 A step qualifier that was silently dropped, and the regression fixing it caused

`"Click 'Add to cart' for the first product"` and `"...for the second product"` were both parsing down to the exact same target, `"Add to cart"` — the `"for the <X> product"` qualifier had nowhere to go in the grammar, so it was dropped on the floor, and the only connector words recognized were `"under"`/`"in"`. Both steps resolved an identical, ambiguous description; which one the AI happened to land on was essentially a coin flip, so "first product" could click the second product's button, or neither step could resolve at all.

Fixed by recognizing `"for"` as a connector alongside `"under"`/`"in"`, carrying `"the first product"` / `"the second product"` through as resolution context — which an existing, already-generic mechanism (ordinal word -> that row's `"Product N: <name>"` Test Data line) was built to consume, but had never been reachable for this wording.

That fix immediately broke six *other*, previously-passing test cases with the plain (non-ordinal) wording `"...for the product"`. The ordinal/Test-Data enrichment was gated by `elif sub.context was empty`, on the old assumption that a step either had context or needed the bare `"Product: <name>"` fallback, never both. Once the parser started supplying `"the product"` as context for *every* `"for"`-qualified step, that branch stopped running at all for the non-ordinal ones, and their target went back to being the same bare, unresolvable `"Add to cart"`. Fixed by running the enrichment whenever the combined description mentions "product," regardless of whether that mention arrived via context or via the raw target — the two sources were never meant to be mutually exclusive, they just happened to be before this.

---

## 7. Verification integrity (proven, not just claimed)

- The sample suite includes rows with **deliberately wrong expected-result values**, to prove a mismatch is reported as `Fail`, never silently converted to a pass and never treated as a reason to "heal" the element.
- **Every LLM call is logged** (`timestamp, run_id, test_case_id, step_no, user_id, agent_name, reason, model, input_tokens, output_tokens, latency_ms, status, estimated_cost`) to a JSONL file, including failed calls — token counts come only from the API response's own usage fields, never estimated.
- A **canary-secret test** proves masking actually strips credentials from logs, reports, screenshots, and LLM payloads before they're written or sent anywhere.
- **Allow-list guard** refuses any navigation, redirect, or frame outside the app's declared `allowed_hosts` — fails closed.

---

## 8. Two applications, one unmodified core

| | automationexercise.com | bbc.com |
|---|---|---|
| Rendering | Server-rendered forms | React SPA (styled-components) |
| Auth | Open self-registration | None needed — every test case is a read-only flow (search, section browsing, article pages) |
| `click_strategy` | `native` (hover-sensitive product grid) | `action_chains` (same React click-reliability risk class as the original second app) |
| Workbook | `AI_Assignment_Test_Cases.xlsx` (20 cases) | `BBC_Test_Cases.xlsx` (15 cases) |
| Config | `apps/automationexercise/config.yaml` | `apps/bbc/config.yaml` |

Both run through the exact same `framework/` package. The differences between them live entirely in their two config files and two workbooks.

bbc.com replaced saucedemo.com as the second app for one reason: it's a real, popular, visually substantial site rather than a QA-practice sandbox, chosen only after live-testing (and rejecting) two riskier candidates — a real airline's production booking site (real bookings/payments, no authorization to automate it) and imdb.com (confirmed, live: blocked outright by an AWS WAF "Human Verification" wall on the very first page load, before any test could even run). bbc.com needs no login, no payment, and no account for anything this suite does.

---

## 9. Setup & usage

```bash
pip install -e ".[dev]"
cp .env.example .env   # fill in OPENAI_API_KEY

# Run either suite
shtaf run --suite AI_Assignment_Test_Cases.xlsx --app-config apps/automationexercise/config.yaml --env test
shtaf run --suite BBC_Test_Cases.xlsx --app-config apps/bbc/config.yaml --env test

# If `shtaf` isn't on PATH (venv not activated), use this form instead:
# ./.venv/Scripts/python.exe -m framework.cli run --suite <workbook> --app-config <config> --env test

# Start the local dashboard:
# ./.venv/Scripts/python.exe -m dashboard.server
# Then open http://127.0.0.1:8000

# Inspect / manage the element store
shtaf store list --application automationexercise
shtaf store invalidate --application automationexercise   # clear a contaminated store
shtaf store export out.json

# Review pending heals
shtaf review list
shtaf review approve <id>

# Operational notes

The `Native app` reference implementation contains several operational
features that are intentionally not part of the core framework yet:

| Native app capability | Current project status | Why it matters |
|---|---|---|
| Cached LLM step plans in `storage/.../plans.json` | Not needed for the current deterministic parser | Prevents repeat runs from paying for step decomposition |
| Immediate stop on fatal LLM errors | Now added as a quota/auth circuit breaker | A 401/403/429 cannot be fixed by retrying the same prompt |
| Explicit LLM output and page-size limits | Now added (`256` output tokens and a `6,000` character page payload) | The previous unbounded request path consumed several thousand input tokens per call |
| `--headless` and `--slow` execution controls | Headless is configured through `.env`; no `--slow` CLI option | Makes live diagnosis reproducible and observable |
| JSON/TXT report pairs and `run_history.json` | Markdown reports and SQLite run history | Both retain run history, but the formats are not interchangeable |
| Locked-workbook result copies | Not currently available | A locked Excel file can prevent durable result write-back |
| Dedicated `evaluate` command for repeated runs and broken-element metrics | Not currently available | Separates framework reliability measurement from a single run |

The recent 6/20 result has a more immediate cause: the run reports contain
LLM `429 insufficient_quota`/credential failures. Cache and deterministic
fallback can replay already verified references, but they cannot learn a
previously unseen element when the AI tier is unavailable. Add credits or
replace the exposed/expired API credential, then run with
`OPENAI_MODEL=gpt-4o-mini` (or pass `--model`) and keep the element store
intact. Do not delete the store before a replay run; doing so forces every
element back through the AI tier.

# Run the framework's own test suite (mocked LLM + Selenium, no browser needed)
pytest
```

## 10. Why this approach is stronger than the Native app approach

This project deliberately does not replace its parser with an LLM. The
deterministic parser in `framework/excel/step_parser.py` is the normal path:
the same workbook text always produces the same typed actions, with no model
call and no prompt-injection surface. An LLM remains restricted to its
specified role as an element finder when cache and deterministic fallback
cannot resolve a target. This keeps natural-language interpretation, element
selection, and expected-result judgment separate.

| Capability | This project | Native app | Result |
|---|---|---|---|
| Step interpretation | Deterministic typed mini-grammar; unsupported wording is explicitly blocked | LLM-generated action plans | Repeatable execution and zero planning tokens |
| Element resolution | SQLite, application/environment partitioning, multiple strategies, fingerprints, pending heals and history | JSON element store with cached plans | Queryable history and safer locator review |
| AI boundary | AI returns an indexed element choice; Selenium derives and verifies the selector locally | Planner and resolver both depend on model output | Less model authority and a smaller failure surface |
| Assertions | Expected results are parsed and evaluated by deterministic code | Executor evaluates after planned actions | AI cannot turn a wrong result into `Pass` |
| Failure handling | Distinguishes `Fail` from `Blocked`, records step-level reasons, and exposes them in Excel, Markdown, and the dashboard | Console/report-oriented output | Failures are actionable without inspecting raw prompts |
| Workbook safety | Timestamped backup plus recovery copy when Excel locks the workbook | Locked-workbook fallback output | A completed run is not lost because the workbook is open |
| Measurement | SQLite run history, JSONL usage, cache/fallback/AI/heal metrics, and dashboard views | JSON history and evaluation command | Durable cross-run analysis in one storage model |

The resulting hybrid is deterministic where determinism is possible, local and
zero-token on replay, AI-assisted only for genuinely changed or ambiguous
elements, and fail-closed when verification is inconclusive. An optional LLM
planner may be added later for new step vocabulary, but it must never replace
the primary parser or evaluate `Expected Result`.

### Locked-workbook recovery

Results are normally written back to the supplied workbook after creating a
timestamped backup. If Excel or another process has the workbook open and
`openpyxl` cannot save it, the run writes the completed result columns and
`Run Summary` sheet to a sibling file named like:

```text
AI_Assignment_Test_Cases.results-20261011T173000Z.xlsx
```

The console prints the recovery path. Review that recovery workbook or replace
the original after closing Excel; the Markdown report and SQLite history are
written independently. This fallback does not weaken validation, skip steps,
or convert a failed test into a pass.

---

## 11. Current status

Both suites run cleanly end-to-end against their real, live sites: automationexercise.com 20/20, saucedemo.com 15/15, both 0 failed / 0 blocked on the latest full run of each, after §6.5–6.7's fixes. Earlier variance between runs (occasional blocks on either suite) traced to the issues documented in §6.5–6.7 and to live LLM API quota exhaustion during heavy same-day testing, not to anything still outstanding in the framework.

When a step *is* genuinely `Blocked` (low confidence, an unresolvable ambiguity, a pending heal with nothing to fall back on), the reason string now always ends with an actionable "What to do" clause — e.g. *"the page has multiple structurally-identical elements for this target... reword the Test Step to add a distinguishing detail"* — generated once per block-site in `resolve.py` and threaded through unchanged to the console, the Excel `Actual Result` cell, and the run report, rather than a bare technical reason with no next step.

Open, honestly-tracked items:
- A low, residual rate of click-silence on saucedemo even with `action_chains` + the MutationObserver retry — mitigated, not provably eliminated, since it's an upstream site/browser interaction quirk rather than framework logic.
- `framework/resolver/fallback.py`'s full-page fingerprint-scan sub-tier (useful if *all* stored strategies fail outright) was identified as a nice-to-have early on but never became necessary to pass either suite, so it wasn't built.
