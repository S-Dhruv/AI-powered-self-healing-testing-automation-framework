"""Tier 3: AI identification. Injects a DOM walker script that creates a numbered
list of interactive elements, sends it to the LLM, and receives an index + confidence.
Verification happens in resolve.py (Tier 4) — this module only proposes, never
accepts on its own."""

from __future__ import annotations

from dataclasses import dataclass

from playwright.sync_api import Page

from framework.llm.client import LlmClient, LlmProposal, estimate_cost
from framework.llm.usage_logger import UsageLogEntry, UsageLogger
from framework.security.masking import mask

# Playwright token budget: a configurable cap on characters sent.
DEFAULT_CHAR_BUDGET = 12_000


@dataclass
class AiFinderResult:
    proposal: LlmProposal
    usage_entry: UsageLogEntry
    estimated_cost: float | None


def _wait_for_dom_stability(page: Page, timeout_ms: int = 5000) -> None:
    """Bounded wait for network/DOM stability before reading the page."""
    try:
        page.wait_for_load_state("networkidle", timeout=timeout_ms)
    except Exception:
        pass
    try:
        page.wait_for_load_state("domcontentloaded", timeout=timeout_ms)
    except Exception:
        pass


def _inject_element_walker(page: Page) -> None:
    """Injects a script that walks the DOM and tags visible/clickable/typeable
    elements with data-sh-idx attribute. Returns the numbered list as a string."""
    script = """
    (() => {
        const isVisible = (el) => {
            if (!el.offsetParent && el.style.display !== 'block') return false;
            const style = window.getComputedStyle(el);
            if (style.display === 'none' || style.visibility === 'hidden' || style.opacity === '0') return false;
            const rect = el.getBoundingClientRect();
            return rect.width > 0 && rect.height > 0;
        };

        const isInteractive = (el) => {
            const tag = el.tagName.toLowerCase();
            const role = el.getAttribute('role');
            const clickable = ['a', 'button', 'input', 'select', 'textarea'].includes(tag);
            const roleClickable = ['button', 'link', 'checkbox', 'textbox', 'option'].includes(role);
            return clickable || roleClickable || el.hasAttribute('data-testid');
        };

        const getNearestHeading = (el) => {
            let node = el;
            for (let i = 0; i < 6 && node; i++) {
                node = node.parentElement;
                if (!node) break;
                const h = node.querySelector('h1, h2, h3, legend');
                if (h) return h.innerText.trim();
            }
            return null;
        };

        const getElementContext = (el) => {
            const attrs = [];
            const tag = el.tagName.toLowerCase();
            const testid = el.getAttribute('data-testid');
            const role = el.getAttribute('role');
            const type = el.getAttribute('type');
            const name = el.getAttribute('name');
            const id = el.getAttribute('id');
            const placeholder = el.getAttribute('placeholder');
            const aria_label = el.getAttribute('aria-label');
            const value = el.getAttribute('value');
            const text = (el.innerText || '').trim().slice(0, 60);
            const heading = getNearestHeading(el);

            return {
                tag, testid, role, type, name, id, placeholder,
                aria_label, value, text, heading
            };
        };

        let idx = 0;
        document.querySelectorAll('*').forEach(el => {
            if (isVisible(el) && isInteractive(el)) {
                el.setAttribute('data-sh-idx', idx.toString());
                idx++;
            }
        });

        return idx;
    })();
    """
    page.evaluate(script)


def build_element_list(page: Page, char_budget: int = DEFAULT_CHAR_BUDGET) -> str:
    """Walks the DOM and returns a numbered list of interactive elements."""
    _wait_for_dom_stability(page)
    _inject_element_walker(page)

    script = """
    (() => {
        const getNearestHeading = (el) => {
            let node = el;
            for (let i = 0; i < 6 && node; i++) {
                node = node.parentElement;
                if (!node) break;
                const h = node.querySelector('h1, h2, h3, legend');
                if (h) return h.innerText.trim();
            }
            return null;
        };

        const lines = [];
        document.querySelectorAll('[data-sh-idx]').forEach(el => {
            const idx = el.getAttribute('data-sh-idx');
            const tag = el.tagName.toLowerCase();
            const testid = el.getAttribute('data-testid');
            const role = el.getAttribute('role');
            const type = el.getAttribute('type');
            const name = el.getAttribute('name');
            const id = el.getAttribute('id');
            const placeholder = el.getAttribute('placeholder');
            const aria_label = el.getAttribute('aria-label');
            const text = (el.innerText || '').trim().slice(0, 60);
            const heading = getNearestHeading(el);

            const attrs = [];
            if (type) attrs.push(`type=${JSON.stringify(type)}`);
            if (name) attrs.push(`name=${JSON.stringify(name)}`);
            if (id) attrs.push(`id=${JSON.stringify(id)}`);
            if (placeholder) attrs.push(`placeholder=${JSON.stringify(placeholder)}`);
            if (aria_label) attrs.push(`aria-label=${JSON.stringify(aria_label)}`);
            if (text) attrs.push(`text=${JSON.stringify(text)}`);
            if (heading) attrs.push(`section=${JSON.stringify(heading)}`);

            const attrStr = attrs.length > 0 ? ' ' + attrs.join(' ') : '';
            const roleStr = role ? ` role=${JSON.stringify(role)}` : '';
            const testidStr = testid ? ` testid=${JSON.stringify(testid)}` : '';

            lines.push(`${idx}. <${tag}>${roleStr}${testidStr}${attrStr}`);
        });

        return lines.join('\\n');
    })();
    """

    element_list = page.evaluate(script) or ""
    element_list = mask(element_list)
    if len(element_list) > char_budget:
        element_list = element_list[:char_budget] + "\n...[truncated to token budget]"

    return element_list


def try_ai_finder(
    client: LlmClient,
    page: Page,
    *,
    target_description: str,
    action: str,
    run_id: str,
    test_case_id: str,
    step_no: int,
    user_id: str,
    reason: str,  # "first_learning" | "healing"
    char_budget: int = DEFAULT_CHAR_BUDGET,
    retry_feedback: str | None = None,
) -> AiFinderResult:
    element_list = build_element_list(page, char_budget)

    proposal = client.find_element(
        target_description=target_description,
        action=action,
        element_list=element_list,
        retry_feedback=retry_feedback,
    )

    cost = (
        estimate_cost(client.model, proposal.input_tokens, proposal.output_tokens)
        if proposal.status == "success"
        else 0.0
    )

    entry = UsageLogEntry(
        timestamp=UsageLogger.now_iso(),
        run_id=run_id,
        test_case_id=test_case_id,
        step_no=step_no,
        user_id=user_id,
        agent_name="ai_finder_retry" if retry_feedback else "ai_finder",
        reason=reason,  # type: ignore[arg-type]
        model=client.model,
        input_tokens=proposal.input_tokens,
        output_tokens=proposal.output_tokens,
        latency_ms=proposal.latency_ms,
        status=proposal.status,  # type: ignore[arg-type]
        estimated_cost=cost if cost is not None else 0.0,
    )

    return AiFinderResult(proposal=proposal, usage_entry=entry, estimated_cost=cost)
