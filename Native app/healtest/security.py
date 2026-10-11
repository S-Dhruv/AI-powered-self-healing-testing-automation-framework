"""Safe use of the environment: URL allow-list, masking, payment guard, rate limiting."""

import logging
import re
import threading
import time
from typing import Iterable, List, Optional
from urllib.parse import urlparse

logger = logging.getLogger(__name__)


class SecurityError(Exception):
    """Raised when an action is refused by a safety rule."""


SENSITIVE_KEY = re.compile(
    r"pass(word)?|pwd|secret|token|card|cvc|cvv|expiry|e-?mail|mobile|phone|dob|birth|address|zip|"
    r"first\s*name|last\s*name|^name$", re.I)
_EMAIL = re.compile(r"[\w.+-]+@[\w-]+(\.[\w-]+)+")
_LONG_DIGITS = re.compile(r"(?<!\d)\d(?:[ -]?\d){11,18}(?!\d)")
_PHONE = re.compile(r"(?<!\d)\+?\d{10,12}(?!\d)")


class Masker:
    """Replaces secrets and personal data before text reaches logs, the LLM or reports."""

    def __init__(self):
        self._secrets: List[str] = []

    def add_secret(self, value: Optional[str]):
        value = (value or "").strip()
        if len(value) >= 4 and value not in self._secrets:
            self._secrets.append(value)
            self._secrets.sort(key=len, reverse=True)

    def mask(self, text: str) -> str:
        if not text:
            return text
        for secret in self._secrets:
            text = text.replace(secret, "***")
        text = _EMAIL.sub("<email>", text)
        text = _LONG_DIGITS.sub("<number>", text)
        return _PHONE.sub("<number>", text)


class MaskingFilter(logging.Filter):
    def __init__(self, masker: Masker):
        super().__init__()
        self.masker = masker

    def filter(self, record: logging.LogRecord) -> bool:
        try:
            record.msg = self.masker.mask(record.getMessage())
            record.args = ()
        except Exception:
            pass
        return True


class AllowList:
    def __init__(self, hosts: Iterable[str]):
        self.hosts = [h.lower().strip() for h in hosts if h]

    def _host_ok(self, host: str) -> bool:
        host = host.lower()
        for allowed in self.hosts:
            if allowed.startswith("*."):
                if host == allowed[2:] or host.endswith(allowed[1:]):
                    return True
            elif host == allowed:
                return True
        return False

    def is_allowed(self, url: str) -> bool:
        parsed = urlparse(url)
        return parsed.scheme in ("http", "https") and bool(parsed.hostname) and self._host_ok(parsed.hostname)

    def check(self, url: str):
        if not self.is_allowed(url):
            raise SecurityError(
                f"URL refused, host is not on the approved test-environment allow-list: {url}")


class RateLimiter:
    """Guarantees a minimum gap between calls (navigation, LLM requests)."""

    def __init__(self, calls_per_second: float):
        self.interval = 1.0 / calls_per_second if calls_per_second and calls_per_second > 0 else 0.0
        self._last = 0.0
        self._lock = threading.Lock()

    def wait(self):
        if not self.interval:
            return
        with self._lock:
            gap = self.interval - (time.time() - self._last)
            if gap > 0:
                time.sleep(gap)
            self._last = time.time()


class PaymentGuard:
    """Refuses payment steps unless the environment allows them, and only ever accepts test cards."""

    PAYMENT_WORDS = re.compile(r"\b(card|cvc|cvv|credit|debit|expiry|expiration|pay|payment)\b", re.I)
    CARD_FIELD = re.compile(r"\bcard\s*(number|no)\b|\bcard number\b", re.I)

    def __init__(self, allow_payments: bool, test_cards: Iterable[str]):
        self.allow_payments = allow_payments
        self.test_cards = {re.sub(r"\D", "", c) for c in test_cards}

    def check(self, action: str, target: str, value: Optional[str] = None):
        if not target or not self.PAYMENT_WORDS.search(target):
            return
        if not self.allow_payments:
            raise SecurityError(f"Payment step refused (allow_payment_attempts is false): {action} '{target}'")
        if action == "type" and self.CARD_FIELD.search(target):
            digits = re.sub(r"\D", "", value or "")
            if digits not in self.test_cards:
                raise SecurityError("Payment refused: the card number is not one of the approved test cards")
