"""LLM access: element identification and step planning, with per-call usage logging.

Token counts always come from the API response `usage` fields, never from estimates.
"""

import getpass
import json
import logging
import os
import time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

from dotenv import load_dotenv

from healtest.security import Masker, RateLimiter

logger = logging.getLogger(__name__)


class LLMFatalError(Exception):
    """The LLM cannot be used at all (no credits, bad key); the run is stopped instead of blocking every step."""


@dataclass
class CallContext:
    run_id: str
    test_case_id: str = ""
    step_number: int = 0


class UsageLogger:
    """Appends one JSON line per LLM call (Assignment 2 dashboard-compatible fields)."""

    def __init__(self, log_dir: str, user_id: str, pricing: Dict[str, float]):
        self.path = Path(log_dir) / "llm_usage.jsonl"
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.user_id = user_id
        self.pricing = pricing
        self.calls: List[Dict[str, Any]] = []

    def cost(self, input_tokens: int, output_tokens: int) -> float:
        return (input_tokens * self.pricing.get("input_per_1k", 0.01)
                + output_tokens * self.pricing.get("output_per_1k", 0.03)) / 1000.0

    def record(self, ctx: CallContext, agent: str, model: str, reason: str, input_tokens: int,
               output_tokens: int, latency_ms: int, status: str, error: Optional[str] = None) -> Dict[str, Any]:
        row = {
            "timestamp": datetime.now().isoformat(timespec="seconds"),
            "user_id": self.user_id, "agent_name": agent, "model": model,
            "run_id": ctx.run_id, "test_case_id": ctx.test_case_id, "step_number": ctx.step_number,
            "reason": reason, "input_tokens": input_tokens, "output_tokens": output_tokens,
            "total_tokens": input_tokens + output_tokens, "latency_ms": latency_ms, "status": status,
            "cost_usd": round(self.cost(input_tokens, output_tokens), 6), "error": error,
        }
        self.calls.append(row)
        with open(self.path, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(row) + "\n")
        return row

    def totals(self, run_id: Optional[str] = None) -> Dict[str, Any]:
        rows = [c for c in self.calls if run_id is None or c["run_id"] == run_id]
        return {
            "calls": len(rows),
            "input_tokens": sum(c["input_tokens"] for c in rows),
            "output_tokens": sum(c["output_tokens"] for c in rows),
            "total_tokens": sum(c["total_tokens"] for c in rows),
            "cost_usd": round(sum(c["cost_usd"] for c in rows), 4),
            "avg_latency_ms": int(sum(c["latency_ms"] for c in rows) / len(rows)) if rows else 0,
            "errors": len([c for c in rows if c["status"] != "success"]),
            "first_learning_calls": len([c for c in rows if c["reason"] == "first_learning"]),
            "healing_calls": len([c for c in rows if c["reason"] == "healing"]),
        }


@dataclass
class Identification:
    ok: bool
    index: Optional[int] = None
    confidence: float = 0.0
    reasoning: str = ""
    input_tokens: int = 0
    output_tokens: int = 0
    error: str = ""

    @property
    def tokens(self) -> int:
        return self.input_tokens + self.output_tokens


@dataclass
class Plan:
    ok: bool
    actions: List[Dict[str, Any]] = field(default_factory=list)
    input_tokens: int = 0
    output_tokens: int = 0
    error: str = ""

    @property
    def tokens(self) -> int:
        return self.input_tokens + self.output_tokens


IDENTIFY_SYSTEM = """You identify ONE element on a web page for a test-automation tool.
You get the target description, the intended action and a numbered list of the visible interactive
elements. Each line is: index|tag[:type]|text="..."|attributes|@context (form / heading / card / region).
Pick the single element that best matches the description and can perform the action.

Words such as "button" or "link" in the description are informal: a link styled as a button counts as a button, and a
button counts as a link. Match on the visible label and the context, not on the exact tag.

Special cases you handle:
- Icons: buttons with only an icon count as buttons by position, size and data attributes, not their tag.
  Text-less elements list cls= (their class) and icon= (their icon's class) instead of text: read those names as meaning.
  "X", "close", "remove", "delete" -> fa-times, close, remove, delete, trash, cart_quantity_delete.  "search" -> fa-search.
  "arrow", "next", "previous" -> fa-arrow*, fa-angle*, chevron, carousel/slider controls.  Use the surrounding context
  ("in ..." / "under ...") to pick the one that belongs to the product or section the target names.
- Styled buttons: links that look/act like buttons match "button" descriptions; buttons match "link" descriptions
- Products: "Add to cart" buttons appear near product images; "View Product" links are in product rows/cards
- Search: search buttons typically come right after search input fields in the same context
- Navigation: next/previous arrows are near product lists or carousels; match by position and icon meaning
- Subscriptions: subscription buttons/arrows appear in footer sections; match by context and proximity

Reply with JSON only: {"index": <integer or null>, "confidence": <0..1>, "reasoning": "<one short sentence>"}.
If nothing clearly matches, return index null and a low confidence. Never guess."""

PLAN_SYSTEM = """You convert ONE natural-language test step into a short list of atomic browser actions.
Reply with JSON only: {"actions": [{"action": "...", "target": "...", "value": "..."}]}

Allowed actions:
- navigate: value = a full URL, a path starting with "/", or "home" for the application's home page.
- click: target = description of the element to click.
- type: target = description of the input field; value = the text to enter.
- select: target = description of a dropdown; value = the option text. (Use click for radio buttons and checkboxes.)
- upload: target = file input; value = file name.
- hover: target = element to hover.
- scroll: value = "bottom" or "top".
- accept_alert: accept a browser alert/confirm dialog.
- find_text: value = text that must be present on the page (for steps like "Locate 'X'").
- login: perform the application's configured login. value = "attempt" when the step is a login attempt that may legitimately fail.
- observe: nothing to do. Use a single observe action for steps that only verify, observe, launch the browser or leave fields empty. Expected results are checked separately.

Rules:
- Test data VALUES are never shown to you. Refer to a test-data field with {{Key}}, using exactly one of the available keys.
  Modifiers: {{Name.first}} {{Name.last}} (split a full name), {{DOB.day}} {{DOB.month}} {{DOB.year}} (for 10-May-1995 style dates),
  {{Expiry.month}} {{Expiry.year}} (for 12/2028 style values), {{Product 1.name}} / {{File.name}} (drop a trailing "(...)" note).
  Use a literal value only when it appears in quotes in the step text.
- One step can need several actions: "Enter name and email" -> two type actions, one per field.
  "Click 'A' > 'B'" -> click A, then click B.
- Every target must be self-contained: say what the element is (button/link/input/dropdown/radio), its visible label and, when the
  step mentions it, the form or section it belongs to, so it can be found without seeing the step.
- Do not add steps the text does not ask for. Do not invent test-data keys.
- If the step repeats an earlier step with a new value, redo that earlier step's type and click actions with the new value.
- Use the page elements you are shown to choose the right action for each field: type for text boxes, select for dropdowns
  (a date split into day/month/year dropdowns needs three select actions), click for radio buttons, checkboxes and labels.
- Write every target in plain words a person would use ("Continue button", "email input in the signup form"). Never copy the
  element-list syntax (no "button:submit", no index numbers). The page elements are only for your reference.
- Only use the modifiers listed above, and only on keys that exist. Never invent a key or a modifier.
- A step that is only "Login" or "Login with valid credentials" (no fields or buttons spelled out) is ONE action: login.
  Do not turn it into a click on a Login link. A step that spells out fields and clicks keeps those actions instead.
- A step that names a button only by an icon or shape ("arrow button", "search icon") refers to the button next to the field
  the previous step used. Name that field in the target ("arrow button next to the subscription email input in the footer").
  Do not invent a different section such as a slider or carousel.
- If a field needs a value that has no test-data key (for example a Mr/Mrs title), choose a sensible literal default and click it."""


class LLMClient:
    def __init__(self, cfg: Dict[str, Any], masker: Masker, usage: UsageLogger, limiter: Optional[RateLimiter] = None):
        load_dotenv()
        api_key = os.getenv("OPENAI_API_KEY")
        if not api_key:
            raise ValueError("OPENAI_API_KEY is not set (put it in .env or the environment)")
        from openai import OpenAI
        self.client = OpenAI(api_key=api_key, timeout=cfg.get("timeout_seconds", 60), max_retries=1)
        self.model = cfg.get("model", "gpt-4-turbo")
        self.max_tokens = cfg.get("max_tokens", 800)
        self.temperature = cfg.get("temperature", 0)
        self.min_confidence = cfg.get("min_confidence", 0.7)
        self.masker, self.usage, self.limiter = masker, usage, limiter

    def _chat(self, ctx: CallContext, agent: str, reason: str, system: str, user: str):
        if self.limiter:
            self.limiter.wait()
        start = time.time()
        try:
            response = self.client.chat.completions.create(
                model=self.model, max_tokens=self.max_tokens, temperature=self.temperature,
                response_format={"type": "json_object"},
                messages=[{"role": "system", "content": system}, {"role": "user", "content": user}])
        except Exception as exc:
            latency = int((time.time() - start) * 1000)
            self.usage.record(ctx, agent, self.model, reason, 0, 0, latency, "error", str(exc)[:200])
            logger.error("[LLM RESPONSE] FAILED after %dms: %s", latency, str(exc)[:200])
            text = str(exc).lower()
            if any(marker in text for marker in ("insufficient_quota", "credit_balance", "invalid_api_key", "incorrect api key")):
                raise LLMFatalError(f"The LLM account cannot be used (add credits or fix OPENAI_API_KEY): {str(exc)[:160]}") from exc
            raise
        latency = int((time.time() - start) * 1000)
        usage = response.usage
        row = self.usage.record(ctx, agent, self.model, reason, usage.prompt_tokens, usage.completion_tokens, latency, "success")
        content = response.choices[0].message.content or ""
        logger.info("[LLM RESPONSE] input_tokens=%d output_tokens=%d total=%d latency=%dms cost=$%.4f",
                    usage.prompt_tokens, usage.completion_tokens, row["total_tokens"], latency, row["cost_usd"])
        logger.info("    OUTPUT: %s", content.strip()[:600])
        return content, usage.prompt_tokens, usage.completion_tokens

    def identify(self, ctx: CallContext, target: str, action: str, page: str, lines: List[str],
                 reason: str, previous: str = "", feedback: str = "") -> Identification:
        user = f"Action: {action}\nTarget: {self.masker.mask(target)}\nPage: {self.masker.mask(page)}\n"
        if previous:
            user += f"This element used to be: {previous}. It may have changed; do not choose a different kind of element.\n"
        if feedback:
            user += f"Second attempt. {feedback}\n"
        user += "Elements:\n" + self.masker.mask("\n".join(lines))
        logger.info("[LLM CALL] agent=element-identifier reason=%s %s step %s", reason, ctx.test_case_id, ctx.step_number)
        logger.info("    INPUT : target=%r action=%s page=%r, %d page elements (%d chars, personal data masked)",
                    target, action, page, len(lines), len(user))
        try:
            content, tin, tout = self._chat(ctx, "element-identifier", reason, IDENTIFY_SYSTEM, user)
            data = json.loads(content)
            index = data.get("index")
            return Identification(ok=True, index=int(index) if index is not None else None,
                                  confidence=float(data.get("confidence", 0) or 0),
                                  reasoning=str(data.get("reasoning", ""))[:200], input_tokens=tin, output_tokens=tout)
        except LLMFatalError:
            raise
        except Exception as exc:
            return Identification(ok=False, error=str(exc)[:200])

    def plan(self, ctx: CallContext, step: str, previous_steps: List[str], data_keys: List[str], app_name: str,
             page_lines: Optional[List[str]] = None, feedback: str = "") -> Plan:
        user = (f"Application: {app_name}\nPrevious steps in this test case: {previous_steps or 'none'}\n"
                f"Step: {step}\nAvailable test-data keys: {data_keys or 'none'}")
        if page_lines:
            user += ("\n\nVisible elements on the page right now (index|tag|text|attributes|context), "
                     "so you can tell text boxes from dropdowns and radio buttons:\n" + self.masker.mask("\n".join(page_lines)))
        if feedback:
            user += f"\n\nYour previous answer was rejected: {feedback}\nFix it and answer again."
        logger.info("[LLM CALL] agent=step-planner reason=first_learning %s step %s", ctx.test_case_id, ctx.step_number)
        logger.info("    INPUT : step=%r keys=%s (no test-data values are sent)", step, data_keys)
        try:
            content, tin, tout = self._chat(ctx, "step-planner", "first_learning", PLAN_SYSTEM, user)
            actions = json.loads(content).get("actions", [])
            if not isinstance(actions, list) or not actions:
                return Plan(ok=False, input_tokens=tin, output_tokens=tout, error="planner returned no actions")
            return Plan(ok=True, actions=actions, input_tokens=tin, output_tokens=tout)
        except LLMFatalError:
            raise
        except Exception as exc:
            return Plan(ok=False, error=str(exc)[:200])
