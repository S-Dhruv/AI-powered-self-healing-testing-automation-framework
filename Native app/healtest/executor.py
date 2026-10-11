"""Generic suite executor. No application-specific code lives here: everything an application
needs (URL, login/registration flow, accounts, ad-block list, payment policy) comes from config."""

import getpass
import hashlib
import logging
import os
import re
import tempfile
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Set, Tuple
from urllib.parse import urlparse

from selenium.common.exceptions import ElementClickInterceptedException, NoAlertPresentException, WebDriverException
from selenium.webdriver import ActionChains
from selenium.webdriver.support.select import Select

from healtest.browser import BrowserManager
from healtest.excel_io import ExcelSuite, TestCase
from healtest.llm import CallContext, LLMClient, LLMFatalError, UsageLogger
from healtest.resolver import ElementResolver
from healtest.security import AllowList, Masker, MaskingFilter, PaymentGuard, RateLimiter, SecurityError, SENSITIVE_KEY
from healtest.settings import Settings
from healtest.store import ElementStore

logger = logging.getLogger(__name__)

SEVERITY = ["Pass", "Skipped", "Blocked", "Fail"]
ACTIONS_NEEDING_TARGET = {"click", "type", "select", "upload", "hover"}
ALLOWED_ACTIONS = ACTIONS_NEEDING_TARGET | {"navigate", "scroll", "accept_alert", "find_text", "login", "observe", "wait"}
NON_TEXT_INPUTS = {"hidden", "submit", "button", "checkbox", "radio", "file", "image", "reset"}
_CURRENCY = r"(?:Rs\.?|USD|INR|EUR|GBP|[$€£₹])"


@dataclass
class RunOptions:
    headless: Optional[bool] = None
    action_delay: Optional[float] = None


@dataclass
class StepRecord:
    test_case_id: str
    step_number: int
    step_text: str
    status: str = "Pass"
    actual_result: str = ""
    sources: Set[str] = field(default_factory=set)
    tokens: int = 0
    seconds: float = 0.0
    screenshot: str = ""


class DataBag:
    """Test data for one test case, with {{Key.modifier}} substitution."""

    MODIFIERS = {"first", "last", "day", "month", "year", "name"}

    def __init__(self, raw: str, timestamp: str, account_email: str):
        self.values: Dict[str, str] = {}
        self.overrides: Dict[str, str] = {}
        for line in (raw or "").splitlines():
            if ":" not in line:
                continue
            key, value = line.split(":", 1)
            value = value.strip()
            value = "" if value.lower() == "(blank)" else value
            value = value.replace("<timestamp>", timestamp)
            value = re.sub(r"<[^>]*e-?mail[^>]*>", account_email, value, flags=re.I)
            self.values[key.strip().lower()] = value

    def keys(self) -> List[str]:
        return sorted(self.values)

    def get(self, key: str) -> Optional[str]:
        key = key.lower()
        return self.overrides.get(key, self.values.get(key))

    @staticmethod
    def _date(value: str) -> Optional[datetime]:
        for fmt in ("%d-%b-%Y", "%d-%B-%Y", "%d/%m/%Y", "%Y-%m-%d"):
            try:
                return datetime.strptime(value, fmt)
            except ValueError:
                continue
        return None

    def _modify(self, value: str, mod: str) -> str:
        if mod == "name":
            return re.sub(r"\s*\(.*\)\s*$", "", value).strip()
        if mod in ("first", "last"):
            parts = value.split()
            return (parts[0] if mod == "first" else parts[-1]) if parts else value
        dt = self._date(value)
        if dt:
            return {"day": str(dt.day), "month": dt.strftime("%B"), "year": str(dt.year)}[mod]
        if "/" in value and mod in ("month", "year"):
            month, year = [p.strip() for p in value.split("/", 1)]
            return month if mod == "month" else year
        raise KeyError(f"cannot take .{mod} of '{value}'")

    def resolve(self, text: Optional[str]) -> Optional[str]:
        if text is None:
            return None

        def sub(match):
            token = match.group(1).strip()
            key, mod = token, None
            if "." in token and token.rsplit(".", 1)[1].lower() in self.MODIFIERS:
                key, mod = token.rsplit(".", 1)
                mod = mod.lower()
            value = self.get(key)
            if value is None:
                raise KeyError(f"test data has no '{key}' value")
            return self._modify(value, mod) if mod else value
        return re.sub(r"\{\{\s*(.+?)\s*\}\}", sub, text)


class Executor:
    def __init__(self, settings: Settings, app: str, env: str, options: Optional[RunOptions] = None):
        self.settings, self.options = settings, options or RunOptions()
        self.app, self.env = app, env
        self.cfg = settings.environment(app, env)
        fw = settings.framework
        self.run_id = uuid.uuid4().hex[:12]
        self.timestamp = str(int(time.time()))
        self.base_url = self.cfg["url"].rstrip("/")

        sec = settings.section("security")
        self.allow_list = AllowList(sec.get("allowed_hosts", []))
        self.allow_list.check(self.base_url)
        self.masker = Masker()
        for handler in logging.getLogger().handlers:
            handler.addFilter(MaskingFilter(self.masker))

        account = dict(self.cfg.get("test_account", {}))
        account["email"] = str(account.get("email", f"qa.tester+{{timestamp}}@test.com")).replace("{timestamp}", self.timestamp)
        self.account = {k: str(v) for k, v in account.items()}
        self.masker.add_secret(self.account.get("password"))
        self.masker.add_secret(self.account.get("email"))

        rate = sec.get("rate_limit", {})
        self.nav_limiter = RateLimiter(rate.get("requests_per_second", 1))
        self.payments = PaymentGuard(bool(self.cfg.get("allow_payment_attempts", False)),
                                     sec.get("payment", {}).get("test_cards", []))

        ex = settings.section("execution")
        self.execution = ex
        self.action_delay = self.options.action_delay if self.options.action_delay is not None else ex.get("action_delay_seconds", 0.3)
        self.settle_seconds = ex.get("settle_seconds", 0.8)

        usage_cfg = settings.section("usage_logging")
        user_id = usage_cfg.get("user_id") or os.getenv("USER_ID") or getpass.getuser()
        self.log_dir = usage_cfg.get("log_dir", "logs")
        self.usage = UsageLogger(self.log_dir, user_id, settings.section("llm").get("pricing", {}))
        self.llm = LLMClient(settings.section("llm"), self.masker, self.usage,
                             RateLimiter(rate.get("llm_calls_per_second", 2)))
        store_cfg = settings.section("element_store")
        self.store = ElementStore(store_cfg.get("location", "./storage/element_store"), app, env,
                                  auto_approve=settings.section("self_healing").get("auto_approve", False))

        self.browser: Optional[BrowserManager] = None
        self.resolver: Optional[ElementResolver] = None
        self.step_records: List[StepRecord] = []
        self.case_rows: List[Dict[str, Any]] = []
        self._seen = ""
        self._acc: Optional[StepRecord] = None
        self._ctx = CallContext(run_id=self.run_id)

    # ------------------------------------------------------------------ suite

    def run(self, suite_path: str) -> Dict[str, Any]:
        started = time.time()
        suite = ExcelSuite(suite_path)
        cases = suite.read()
        runnable = [c for c in cases if c.execute != "N"]
        logger.info("[RUN] %s | app=%s env=%s | %d of %d test cases will run", self.run_id, self.app, self.env,
                    len(runnable), len(cases))
        headless = self.options.headless if self.options.headless is not None else self.execution.get("headless", False)
        self.browser = BrowserManager(
            self.allow_list, self.nav_limiter, headless=headless,
            window_size=self.execution.get("window_size", "1920x1080"),
            page_load_timeout=self.execution.get("page_load_timeout_seconds", 30),
            block_hosts=self.cfg.get("block_hosts", []),
            screenshot_dir=self.execution.get("screenshot_dir", "screenshots"))
        try:
            self.browser.start()
            self.resolver = ElementResolver(self.browser.driver, self.store, self.llm,
                                            self.settings.section("element_identification"), self.run_id)
            self.browser.navigate_to(self.base_url)
            for case in cases:
                if case.execute == "N":
                    self._skipped_row(case)
                else:
                    self._run_case(case)
            self.store.save()
        finally:
            self.browser.close()

        summary = self._summarize(time.time() - started)
        output = suite.write_results(cases, self.case_rows, self._step_rows(), self.run_id, summary)
        return {"run_id": self.run_id, "summary": summary, "cases": self.case_rows, "steps": self._step_rows(),
                "results_file": str(output), "suite": Path(suite_path).name}

    def _skipped_row(self, case: TestCase):
        self.case_rows.append(self._row(case, "Skipped", "Execute flag is N", "", 0.0, set(), 0))
        logger.info("[SKIP] %s %s (Execute = N)", case.test_case_id, case.title)

    # ------------------------------------------------------------------ test case

    def _run_case(self, case: TestCase):
        logger.info("=" * 78)
        logger.info("[TEST CASE START] %s: %s", case.test_case_id, case.title)
        logger.info("=" * 78)
        started = time.time()
        self._reset_session()
        self._seen = ""
        bag = DataBag(case.test_data, self.timestamp, self.account["email"])
        for key, value in bag.values.items():
            if SENSITIVE_KEY.search(key):
                self.masker.add_secret(value)

        steps = self._parse_steps(case.test_step)
        records: List[StepRecord] = []
        blocked = False
        for number, text in enumerate(steps, 1):
            logger.info("-" * 78)
            logger.info("[STEP %d/%d] %s", number, len(steps), text)
            if blocked:
                record = StepRecord(case.test_case_id, number, text, "Skipped", "Not run: an earlier step was blocked")
                logger.info("[RESULT] Step %d: Skipped (an earlier step was blocked)", number)
            else:
                record = self._run_step(case, number, text, steps[:number - 1], bag)
                logger.info("[RESULT] Step %d: %s | Source: %s | Tokens: %d | Time: %.1fs%s", number, record.status,
                            ", ".join(sorted(record.sources)) or "-", record.tokens, record.seconds,
                            "" if record.status == "Pass" else f" | {record.actual_result}")
                blocked = record.status == "Blocked"
            records.append(record)

        if not blocked:
            records.append(self._verify_expected(case, len(steps) + 1))
        self.step_records.extend(records)

        worst = max(records, key=lambda r: SEVERITY.index(r.status)) if records else None
        status = worst.status if worst else "Blocked"
        problems = [r for r in records if r.status != "Pass"]
        actual = "\n".join(f"Step {r.step_number} [{r.status}]: {r.actual_result}" for r in problems) or \
                 f"All {len(records)} steps passed"
        sources = set().union(*(r.sources for r in records)) if records else set()
        shot = next((r.screenshot for r in problems if r.screenshot), "")
        self.case_rows.append(self._row(case, status, actual, shot, time.time() - started, sources,
                                        sum(r.tokens for r in records)))
        logger.info("[TEST CASE END] %s -> %s", case.test_case_id, status)

    def _row(self, case: TestCase, status: str, actual: str, shot: str, seconds: float, sources: Set[str],
             tokens: int) -> Dict[str, Any]:
        return {"test_case_id": case.test_case_id, "title": case.title, "execute": case.execute, "status": status,
                "actual_result": actual, "screenshot_path": shot, "execution_time": round(seconds, 1),
                "element_source": ", ".join(sorted(sources)), "tokens_used": tokens,
                "executed_at": datetime.now().isoformat(timespec="seconds")}

    def _step_rows(self) -> List[Dict[str, Any]]:
        return [{"test_case_id": r.test_case_id, "step_number": r.step_number, "step_text": r.step_text,
                 "status": r.status, "actual_result": r.actual_result, "element_source": ", ".join(sorted(r.sources)),
                 "tokens_used": r.tokens, "execution_time": round(r.seconds, 1)} for r in self.step_records]

    @staticmethod
    def _parse_steps(text: str) -> List[str]:
        parts = re.split(r"\n\s*\d+\.\s+", "\n" + (text or "").strip())
        return [re.sub(r"^\s*\d+\.\s*", "", p).strip() for p in parts if p.strip()]

    def _reset_session(self):
        self._accept_alert_if_any()
        try:
            self.browser.driver.delete_all_cookies()
            self.browser.navigate_to(self.base_url)
        except SecurityError:
            raise
        except WebDriverException as exc:
            logger.warning("could not reset the browser session: %s", str(exc)[:100])

    # ------------------------------------------------------------------ steps and plans

    def _run_step(self, case: TestCase, number: int, text: str, prior: List[str], bag: DataBag) -> StepRecord:
        record = StepRecord(case.test_case_id, number, text)
        self._acc = record
        self._ctx = CallContext(run_id=self.run_id, test_case_id=case.test_case_id, step_number=number)
        started = time.time()
        try:
            actions = self._plan(text, prior, bag, record)
            if actions is None:
                record.status, record.actual_result = "Blocked", record.actual_result or "Could not plan the step"
            else:
                messages = []
                for index, action in enumerate(actions, 1):
                    logger.info("[ACTION %d/%d] %s", index, len(actions), self._describe(action))
                    status, message = self._do(action, bag)
                    messages.append(message)
                    self._remember_page_text()
                    if status != "Pass":
                        record.status, record.actual_result = status, message
                        break
                else:
                    record.actual_result = "; ".join(messages)
        except LLMFatalError:
            raise
        except SecurityError as exc:
            record.status, record.actual_result = "Blocked", f"Refused by safety rule: {exc}"
        except KeyError as exc:
            record.status, record.actual_result = "Blocked", f"Test data problem: {exc}"
        except Exception as exc:
            logger.error("step failed unexpectedly: %s", exc, exc_info=logger.isEnabledFor(logging.DEBUG))
            record.status, record.actual_result = "Blocked", f"Error: {str(exc)[:150]}"
        record.seconds = time.time() - started
        if record.status in ("Blocked", "Fail"):
            record.screenshot = self.browser.take_screenshot(f"{case.test_case_id}_step{number}")
        self._acc = None
        return record

    def _plan(self, text: str, prior: List[str], bag: DataBag, record: StepRecord) -> Optional[List[Dict[str, Any]]]:
        key = hashlib.md5("|".join([self.app, text, "||".join(prior), ",".join(bag.keys())]).encode()).hexdigest()[:16]
        cached = self.store.get_plan(key)
        if cached:
            logger.info("[PLAN] cached plan, %d actions -> 0 tokens", len(cached["actions"]))
            return cached["actions"]
        logger.info("[PLAN] no stored plan for this step -> asking the LLM once (it is stored for later runs)")
        try:
            page_lines, _ = self.resolver.tools.snapshot(150)
        except WebDriverException:
            page_lines = []
        finally:
            self.resolver.tools.clean_markers()

        feedback = ""
        for attempt in (1, 2):
            plan = self.llm.plan(self._ctx, text, prior, bag.keys(), self.cfg.get("name", self.app), page_lines, feedback)
            record.tokens += plan.tokens
            if not plan.ok:
                record.actual_result = f"Step planning failed: {plan.error}"
                return None
            feedback = self._plan_problems(plan.actions, bag)
            if not feedback:
                self.store.put_plan(key, text, plan.actions, self.llm.model)
                return plan.actions
            logger.warning("[PLAN] rejected (attempt %d): %s", attempt, feedback)
        record.actual_result = f"Planner returned an unusable plan: {feedback}"
        return None

    @staticmethod
    def _plan_problems(actions: List[Any], bag: DataBag) -> str:
        """Dry-run a plan: known actions, targets present, every {{placeholder}} resolvable from the test data."""
        for action in actions:
            if not isinstance(action, dict) or action.get("action") not in ALLOWED_ACTIONS:
                return f"unknown action in {action!r}; allowed: {sorted(ALLOWED_ACTIONS)}"
            if action["action"] in ACTIONS_NEEDING_TARGET and not action.get("target"):
                return f"{action['action']} needs a target: {action!r}"
            for field_name in ("target", "value"):
                try:
                    bag.resolve(action.get(field_name))
                except KeyError as exc:
                    return f"{exc} (available keys: {bag.keys()}; modifiers: {sorted(DataBag.MODIFIERS)})"
        return ""

    @staticmethod
    def _describe(action: Dict[str, Any]) -> str:
        bits = [action.get("action", "?")]
        if action.get("target"):
            bits.append(f"-> {action['target']!r}")
        if action.get("value"):
            bits.append(f"value={action['value']!r}")
        return " ".join(bits)

    # ------------------------------------------------------------------ actions

    def _do(self, action: Dict[str, Any], bag: DataBag) -> Tuple[str, str]:
        name = action["action"]
        if name != "accept_alert":
            self._accept_alert_if_any()
        target = bag.resolve(action.get("target"))
        value = bag.resolve(action.get("value"))
        self.payments.check(name, target or "", value)

        if name == "observe":
            return "Pass", "Observed"
        if name == "wait":
            time.sleep(min(float(value or 1), 10))
            return "Pass", "Waited"
        if name == "navigate":
            return self._navigate(value or "home")
        if name == "login":
            return self._login(value == "attempt", bag)
        if name == "accept_alert":
            return self._accept_alert()
        if name == "find_text":
            return self._find_text(value or "")
        if name == "scroll":
            return self._scroll(target, value)
        return self._element_action(name, target, value)

    def _element_action(self, name: str, target: str, value: Optional[str]) -> Tuple[str, str]:
        validator = {"type": self._is_text_field, "select": lambda e: e.tag_name.lower() == "select" and e.is_displayed(),
                     "upload": self._is_file_input, "click": self._click_validator(target)}.get(name, lambda e: e.is_displayed())
        res = self.resolver.resolve(self._ctx, target, name, self._page_description(), validator)
        self._acc.tokens += res.tokens
        if not res.ok:
            return "Blocked", f"Could not identify '{target}': {res.reason}"
        self._acc.sources.add(res.source)
        element, driver = res.element, self.browser.driver
        driver.execute_script("arguments[0].scrollIntoView({block: 'center'});", element)

        if name == "click":
            before = self._page_state()
            self._click(element)
            self._wait_for_change(before)
            self._settle()
            self._guard_host()
            return "Pass", f"Clicked '{target}'"
        if name == "hover":
            ActionChains(driver).move_to_element(element).perform()
            return "Pass", f"Hovered '{target}'"
        if name == "upload":
            path = self._sample_file(value or "sample.txt")
            element.send_keys(str(path))
            return "Pass", f"Selected file {path.name}"
        if name == "select" or element.tag_name.lower() == "select":
            return ("Pass", f"Selected '{value}' in '{target}'") if self._choose(element, value or "") \
                else ("Blocked", f"No option '{value}' in '{target}'")
        element.clear()
        if value:
            element.send_keys(value)
        logger.info("    typed %s into %r", "<masked>" if SENSITIVE_KEY.search(target) else repr(value), target)
        return "Pass", f"Entered a value in '{target}'"

    @staticmethod
    def _is_text_field(element) -> bool:
        tag = element.tag_name.lower()
        kind = (element.get_attribute("type") or "text").lower()
        ok = tag in ("textarea", "select") or (tag == "input" and kind not in NON_TEXT_INPUTS) \
            or element.get_attribute("contenteditable") == "true"
        return ok and element.is_displayed()

    @staticmethod
    def _click_validator(target: str):
        """A click target must be something clickable: never a text box, text area or dropdown."""
        wants = (target or "").lower()

        def check(element) -> bool:
            if not element.is_displayed():
                return False
            tag = element.tag_name.lower()
            kind = (element.get_attribute("type") or "").lower()
            if tag in ("textarea", "select"):
                return False
            if tag == "input" and kind not in ("submit", "button", "image", "reset", "radio", "checkbox"):
                return False
            if "radio" in wants:
                return (tag == "input" and kind == "radio") or tag == "label" or element.get_attribute("role") == "radio"
            if "checkbox" in wants:
                return (tag == "input" and kind == "checkbox") or tag == "label" or element.get_attribute("role") == "checkbox"
            return True
        return check

    def _page_state(self) -> str:
        try:
            return self.browser.driver.execute_script(
                "return location.href + '|' + (document.body ? document.body.innerText.length : 0) + '|' + document.querySelectorAll('*').length")
        except WebDriverException:
            return ""

    def _wait_for_change(self, before: str, timeout: float = 2.5):
        """After a click, wait until the page visibly changes (new page, modal, message) so the new state is read, not the old."""
        end = time.time() + timeout
        while time.time() < end:
            if self._page_state() != before:
                return
            time.sleep(0.15)

    @staticmethod
    def _is_file_input(element) -> bool:
        return element.tag_name.lower() == "input" and (element.get_attribute("type") or "").lower() == "file"

    def _click(self, element):
        try:
            element.click()
        except (ElementClickInterceptedException, WebDriverException):
            self.browser.driver.execute_script("arguments[0].click();", element)

    @staticmethod
    def _choose(element, value: str) -> bool:
        select, wanted = Select(element), value.strip().lower()
        for option in select.options:
            if option.text.strip().lower() == wanted or (option.get_attribute("value") or "").lower() == wanted:
                select.select_by_visible_text(option.text)
                return True
        for option in select.options:
            if wanted and wanted in option.text.lower():
                select.select_by_visible_text(option.text)
                return True
        return False

    @staticmethod
    def _sample_file(name: str) -> Path:
        name = re.sub(r"\s*\(.*\)\s*$", "", name).strip() or "sample.txt"
        path = Path(tempfile.gettempdir()) / name
        if not path.exists():
            path.write_text("Sample file for upload tests\n")
        return path

    def _navigate(self, value: str) -> Tuple[str, str]:
        url = self.base_url if value == "home" else (self.base_url + value if value.startswith("/") else value)
        self.browser.navigate_to(url)
        self._settle()
        return "Pass", f"Navigated to {url}"

    def _scroll(self, target: Optional[str], value: Optional[str]) -> Tuple[str, str]:
        driver = self.browser.driver
        if target:
            res = self.resolver.resolve(self._ctx, target, "scroll", self._page_description(), lambda e: e.is_displayed())
            self._acc.tokens += res.tokens
            if not res.ok:
                return "Blocked", f"Could not identify '{target}': {res.reason}"
            self._acc.sources.add(res.source)
            driver.execute_script("arguments[0].scrollIntoView({block: 'center'});", res.element)
        else:
            driver.execute_script("window.scrollTo(0, arguments[0]);", 0 if value == "top" else 10 ** 6)
        time.sleep(0.4)
        return "Pass", f"Scrolled {value or target}"

    def _find_text(self, text: str) -> Tuple[str, str]:
        wanted = self._norm(text)
        for _ in range(6):
            if wanted in self._norm(self.browser.driver.execute_script("return document.body.innerText")):
                return "Pass", f"Found '{text}'"
            time.sleep(0.5)
        return "Blocked", f"'{text}' is not on the page"

    def _accept_alert(self) -> Tuple[str, str]:
        for _ in range(8):
            try:
                self.browser.driver.switch_to.alert.accept()
                time.sleep(0.8)
                return "Pass", "Browser alert accepted"
            except NoAlertPresentException:
                time.sleep(0.4)
        return "Pass", "No browser alert appeared"

    def _accept_alert_if_any(self):
        try:
            self.browser.driver.switch_to.alert.accept()
        except Exception:
            pass

    def _guard_host(self):
        if not self.browser.current_host_allowed():
            self.browser.driver.back()
            raise SecurityError("the page navigated outside the approved test environments; went back")

    def _settle(self):
        end = time.time() + 8
        while time.time() < end:
            try:
                if self.browser.driver.execute_script("return document.readyState") == "complete":
                    break
            except WebDriverException:
                break
            time.sleep(0.2)
        time.sleep(self.settle_seconds + self.action_delay)

    def _page_description(self) -> str:
        driver = self.browser.driver
        path = re.sub(r"\d+", "N", urlparse(driver.current_url).path)
        return f"{driver.title or 'Untitled'} [{path}]"

    # ------------------------------------------------------------------ login / registration (config driven)

    def _logged_in(self, text: str) -> bool:
        return bool(text) and self._norm(text) in self._norm(self._page_text())

    def _page_text(self) -> str:
        try:
            return self.browser.driver.execute_script("return document.body ? document.body.innerText : ''") or ""
        except WebDriverException:
            return ""

    def _run_flow(self, actions: List[Dict[str, Any]], bag: DataBag, variables: Dict[str, str]) -> Tuple[str, str]:
        bag.overrides = {k.lower(): v for k, v in variables.items()}
        try:
            for action in actions:
                logger.info("[ACTION] (flow) %s", self._describe(action))
                status, message = self._do(dict(action), bag)
                self._remember_page_text()
                if status != "Pass":
                    return status, message
            return "Pass", "Flow completed"
        finally:
            bag.overrides = {}

    def _login(self, attempt_only: bool, bag: DataBag) -> Tuple[str, str]:
        login = self.cfg.get("login")
        if not login:
            return "Blocked", "No login flow is configured for this application"
        success = login.get("success_text", "")
        if self._logged_in(success):
            return "Pass", "Already logged in"
        variables = dict(self.account)
        for key in ("email", "username", "password"):
            if bag.get(key):
                variables[key] = bag.get(key)
        self.masker.add_secret(variables.get("password"))

        status, message = self._run_flow(login.get("actions", []), bag, variables)
        if status != "Pass":
            return status, message
        time.sleep(1)
        if self._logged_in(success):
            return "Pass", "Logged in"
        if attempt_only:
            return "Pass", "Login attempted (result checked against the expected result)"
        register = self.cfg.get("register")
        if register:
            logger.info("[LOGIN] account is not usable; creating the test account from config")
            status, message = self._run_flow(register.get("actions", []), bag, dict(self.account))
            if status == "Pass" and self._logged_in(success):
                return "Pass", "Test account created and logged in"
            return "Blocked", f"Login failed and the test account could not be created: {message}"
        return "Blocked", "Login failed"

    # ------------------------------------------------------------------ verification

    @staticmethod
    def _norm(text: str) -> str:
        return re.sub(r"\s+", " ", text or "").strip().lower()

    def _remember_page_text(self):
        try:
            self._seen = (self._seen + " " + self._norm(self.resolver.tools.seen_text()))[-300000:]
        except Exception:
            pass

    @staticmethod
    def expected_phrases(expected: str) -> List[str]:
        phrases = [p.split("<")[0].strip() for p in re.findall(r"['\"]([^'\"]{3,}?)['\"]", expected)]
        phrases += re.findall(_CURRENCY + r"\s*\d[\d,]*(?:\.\d+)?", expected)
        if not phrases:
            groups = re.findall(r"\(([^)]*)\)", expected)
            if groups:
                phrases = [p.strip() for p in groups[0].split(",") if p.strip()]
        return [p for p in phrases if len(p) >= 3]

    def _verify_expected(self, case: TestCase, number: int) -> StepRecord:
        """Deterministic: every expected text must have appeared. The AI never takes part in this."""
        record = StepRecord(case.test_case_id, number, "Verify expected result")
        phrases = self.expected_phrases(case.expected_result or "")
        time.sleep(0.5)
        self._remember_page_text()
        if not phrases:
            record.actual_result = "No checkable text in the expected result"
            logger.info("[VERIFY] %s: no checkable text in the expected result", case.test_case_id)
            return record
        missing = [p for p in phrases if self._norm(p) not in self._seen]
        if missing:
            record.status = "Fail"
            record.actual_result = "Expected text never shown: " + "; ".join(f"'{m}'" for m in missing)
            record.screenshot = self.browser.take_screenshot(f"{case.test_case_id}_verify")
            logger.warning("[VERIFY] FAIL %s", record.actual_result)
        else:
            record.actual_result = f"All {len(phrases)} expected texts were shown"
            logger.info("[VERIFY] PASS %s", record.actual_result)
        return record

    # ------------------------------------------------------------------ summary

    def _summarize(self, seconds: float) -> Dict[str, Any]:
        counts = {s: len([r for r in self.case_rows if r["status"] == s]) for s in SEVERITY}
        totals = self.usage.totals(self.run_id)
        stats = self.resolver.stats if self.resolver else {}
        summary = {**counts, "total_tokens": totals["total_tokens"], "input_tokens": totals["input_tokens"],
                   "output_tokens": totals["output_tokens"], "llm_calls": totals["calls"],
                   "cost_usd": totals["cost_usd"], "cache_hit_rate": self.resolver.cache_hit_rate() if self.resolver else 0,
                   "lookups": stats.get("lookups", 0), "cache_hits": stats.get("Cache", 0),
                   "fallback_hits": stats.get("Fallback", 0), "ai_identified": stats.get("AI", 0),
                   "healed_count": stats.get("Healed", 0), "blocked_lookups": stats.get("Blocked", 0),
                   "duration_s": round(seconds, 1), "steps": {s: len([r for r in self.step_records if r.status == s])
                                                              for s in SEVERITY}}
        return summary
