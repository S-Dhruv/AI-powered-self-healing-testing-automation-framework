"""Element resolution: cache -> deterministic fallback -> AI -> store/heal -> refuse to guess."""

import logging
import time
from dataclasses import dataclass
from typing import Any, Callable, Dict, Optional, Tuple

from healtest.llm import CallContext, LLMClient
from healtest.page import LOCATOR_ORDER, PageTools, similarity, summarize_fingerprint
from healtest.store import ElementStore

logger = logging.getLogger(__name__)

Validator = Callable[[Any], bool]


@dataclass
class Resolution:
    element: Any = None
    source: str = ""          # Cache | Fallback | AI | Healed | Blocked
    tokens: int = 0
    reason: str = ""

    @property
    def ok(self) -> bool:
        return self.element is not None


class ElementResolver:
    def __init__(self, driver, store: ElementStore, llm: LLMClient, cfg: Dict[str, Any], run_id: str):
        self.driver, self.store, self.llm, self.run_id = driver, store, llm, run_id
        self.tools = PageTools(driver)
        self.match_threshold = cfg.get("fingerprint_match_threshold", 0.8)
        self.scan_threshold = cfg.get("fingerprint_scan_threshold", 0.85)
        self.lookup_wait = cfg.get("lookup_wait_seconds", 3)
        self.max_elements = cfg.get("max_page_elements", 300)
        self.stats = {"lookups": 0, "Cache": 0, "Fallback": 0, "AI": 0, "Healed": 0, "Blocked": 0}

    # ------------------------------------------------------------------ public

    def resolve(self, ctx: CallContext, target: str, action: str, page: str, validator: Validator) -> Resolution:
        self.stats["lookups"] += 1
        logger.info("[CACHE] lookup target=%r page=%r", target, page)
        entry = self.store.get(page, target)
        if entry:
            hit = self._from_reference(entry["locators"], entry["fingerprint"], validator, self.lookup_wait)
            if hit:
                element, via = hit
                source = "Cache" if via == "primary" else "Fallback"
                self.store.touch(entry["key"])
                label = "HIT" if source == "Cache" else "FALLBACK HIT"
                logger.info("[%s] %s v%d (used %d times) via %s -> 0 tokens", "CACHE" if source == "Cache" else "FALLBACK",
                            label, entry["version"], entry["usage_count"], via)
                return self._done(source, element)

            pending = entry.get("pending")
            if pending:
                hit = self._from_reference(pending["locators"], pending["fingerprint"], validator, 0)
                if hit:
                    logger.info("[HEAL] using healed reference awaiting review (run %s) -> 0 tokens", pending["run_id"])
                    self.store.touch(entry["key"])
                    return self._done("Healed", hit[0])

            logger.warning("[CACHE] STALE reference for %r: stored locators and fingerprint no longer match the page", target)
            return self._identify(ctx, target, action, page, validator, entry)

        logger.info("[CACHE] MISS no stored reference for %r -> the LLM is needed", target)
        return self._identify(ctx, target, action, page, validator, None)

    # ------------------------------------------------------------------ cache / fallback

    def _from_reference(self, locators: Dict[str, Any], fingerprint: Dict[str, Any], validator: Validator,
                        wait: float) -> Optional[Tuple[Any, str]]:
        strategies = [s for s in LOCATOR_ORDER if locators.get(s)]
        deadline = time.time() + wait
        while True:
            for position, strategy in enumerate(strategies):
                matches = self.tools.find(strategy, locators[strategy])
                if len(matches) != 1:
                    continue
                element = matches[0]
                score = similarity(fingerprint, self.tools.fingerprint_of(element))
                if score >= self.match_threshold and validator(element):
                    return element, ("primary" if position == 0 else strategy)
                logger.debug("locator %s resolved but fingerprint score %.2f / validator failed", strategy, score)
            if time.time() >= deadline:
                break
            time.sleep(0.5)

        return self._scan_by_fingerprint(fingerprint, validator)

    def _scan_by_fingerprint(self, fingerprint: Dict[str, Any], validator: Validator) -> Optional[Tuple[Any, str]]:
        tag = fingerprint.get("tag")
        if not tag:
            return None
        try:
            scored = sorted(((similarity(fingerprint, fp), el) for el, fp in self.tools.candidates(tag)),
                            key=lambda pair: pair[0], reverse=True)
        finally:
            self.tools.clean_markers()
        if not scored or scored[0][0] < self.scan_threshold:
            return None
        if len(scored) > 1 and scored[0][0] - scored[1][0] < 0.1:
            logger.debug("fingerprint scan is ambiguous (%.2f vs %.2f)", scored[0][0], scored[1][0])
            return None
        return (scored[0][1], "fingerprint") if validator(scored[0][1]) else None

    # ------------------------------------------------------------------ AI + store/heal

    def _identify(self, ctx: CallContext, target: str, action: str, page: str, validator: Validator,
                  entry: Optional[Dict[str, Any]]) -> Resolution:
        healing = entry is not None
        reason = "healing" if healing else "first_learning"
        feedback = ""
        try:
            self._wait_ready()
            lines, total = self.tools.snapshot(self.max_elements)
            if not lines:
                return self._blocked("the page has no visible interactive elements", 0)

            # First attempt
            ident = self.llm.identify(ctx, target, action, page, lines, reason,
                                      previous=summarize_fingerprint(entry["fingerprint"]) if healing else "")
            tokens = ident.tokens
            if not ident.ok:
                return self._blocked(f"LLM call failed: {ident.error}", tokens)

            # Re-try with feedback if confidence is low (0.2-0.7 range, not 0.1)
            # For 0.1, use more aggressive retry instructions
            if (ident.index is None or ident.confidence < self.llm.min_confidence) and feedback == "":
                logger.info("[IDENTIFY] Low confidence (%.2f), retrying with aggressive matching...", ident.confidence)
                if ident.confidence <= 0.1:
                    feedback = (f"Confidence too low ({ident.confidence:.2f}). Find the element by ANY reasonable match: "
                               f"text content (partial match ok), nearby text, position (first product, arrow), icon meaning, "
                               f"data attributes. For 'Add to cart': first button near product image or 'Add' text. "
                               f"For 'View Product': links in product rows. For arrows: navigation buttons at page edges.")
                else:
                    feedback = (f"Your confidence ({ident.confidence:.2f}) is too low. Re-examine carefully. "
                               f"Look for: exact text, partial text, position, surrounding context, data attributes. "
                               f"Be aggressive with styled or icon-only elements.")
                ident = self.llm.identify(ctx, target, action, page, lines, reason,
                                         previous=summarize_fingerprint(entry["fingerprint"]) if healing else "",
                                         feedback=feedback)
                tokens += ident.tokens
                if not ident.ok:
                    return self._blocked(f"LLM call failed: {ident.error}", tokens)

            if ident.index is None or ident.confidence < self.llm.min_confidence:
                return self._blocked(f"LLM could not identify the element with enough confidence "
                                     f"({ident.confidence:.2f} < {self.llm.min_confidence}); refusing to guess", tokens)
            element = self.tools.marked_element(ident.index)
            if element is None:
                return self._blocked(f"LLM chose index {ident.index}, which is not on the page", tokens)
            fingerprint = self.tools.fingerprint_of(element)
            locators = self.tools.locators_for(element)
        finally:
            self.tools.clean_markers()

        logger.info("[VERIFY] LLM chose %s (confidence %.2f): %s", summarize_fingerprint(fingerprint), ident.confidence, ident.reasoning)
        if not validator(element):
            return self._blocked(f"the chosen element {summarize_fingerprint(fingerprint)} cannot be used for '{action}'", tokens)

        if healing:
            old_tag = entry["fingerprint"].get("tag")
            if old_tag and old_tag != fingerprint["tag"] and ident.confidence < 0.9:
                return self._blocked(f"healing would replace a <{old_tag}> with a <{fingerprint['tag']}> at only "
                                     f"{ident.confidence:.2f} confidence; refusing to guess", tokens)
            outcome = self.store.propose_heal(entry["key"], locators, fingerprint, self.run_id, self.llm.model,
                                              f"re-identified after the stored reference stopped matching: {target}")
            logger.info("[HEAL] new reference %s (%s)", "approved automatically" if outcome == "promoted"
                        else "saved as PENDING - run `healtest store review` to approve or reject", summarize_fingerprint(fingerprint))
            return self._done("Healed", element, tokens)

        self.store.learn(page, target, locators, fingerprint, self.run_id, self.llm.model)
        logger.info("[STORE] learned %r with %d locator strategies", target, len([k for k in locators if locators[k]]))
        return self._done("AI", element, tokens)

    # ------------------------------------------------------------------ helpers

    def _wait_ready(self, timeout: float = 8.0):
        end = time.time() + timeout
        while time.time() < end:
            try:
                if self.driver.execute_script("return document.readyState") == "complete":
                    return
            except Exception:
                return
            time.sleep(0.2)

    def _done(self, source: str, element: Any, tokens: int = 0) -> Resolution:
        self.stats[source] += 1
        return Resolution(element=element, source=source, tokens=tokens)

    def _blocked(self, reason: str, tokens: int) -> Resolution:
        self.stats["Blocked"] += 1
        logger.warning("[BLOCKED] %s", reason)
        return Resolution(source="Blocked", tokens=tokens, reason=reason)

    def cache_hit_rate(self) -> float:
        lookups = self.stats["lookups"]
        return round((self.stats["Cache"] + self.stats["Fallback"]) / lookups * 100, 1) if lookups else 0.0
