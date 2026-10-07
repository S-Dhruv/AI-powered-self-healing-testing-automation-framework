"""Masking of credentials and personal data.

Applied before: any LLM payload is sent, any log line is written, any Excel
Actual Result/screenshot is saved. A single `mask()` is the only path allowed
to touch these surfaces with raw text.
"""

from __future__ import annotations

import re

_EMAIL_RE = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")
_CARD_RE = re.compile(r"\b(?:\d[ -]?){13,19}\b")
_CVC_LABEL_RE = re.compile(r"(?i)\b(cvc|cvv)\b\s*[:=]?\s*\d{3,4}")
_PASSWORD_LABEL_RE = re.compile(r"(?i)\b(password|pwd|pass)\b\s*[:=]\s*\S+")

# Names of configured secrets are masked verbatim too (e.g. .env values), so a
# canary string planted in any of these never reaches a log or LLM payload.
_SENSITIVE_ENV_VARS = (
    "OPENAI_API_KEY",
    "AZURE_OPENAI_API_KEY",
    "TEST_ACCOUNT_PASSWORD",
)


def mask(text: str, extra_secrets: list[str] | None = None) -> str:
    """Return text with emails, passwords, card numbers and known secrets redacted."""
    if not text:
        return text
    masked = text
    for secret in extra_secrets or []:
        if secret:
            masked = masked.replace(secret, "***MASKED***")
    masked = _PASSWORD_LABEL_RE.sub(lambda m: f"{m.group(1)}: ***MASKED***", masked)
    masked = _CVC_LABEL_RE.sub(lambda m: f"{m.group(1)}: ***MASKED***", masked)
    masked = _CARD_RE.sub("***MASKED-CARD***", masked)
    masked = _EMAIL_RE.sub("***MASKED-EMAIL***", masked)
    return masked


def mask_secrets_from_env(text: str, env: dict[str, str]) -> str:
    """Mask the literal values of known sensitive env vars present in `env`."""
    secrets = [env[name] for name in _SENSITIVE_ENV_VARS if env.get(name)]
    return mask(text, extra_secrets=secrets)
