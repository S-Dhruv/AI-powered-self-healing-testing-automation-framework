"""Mini-grammar for 'Expected Result' -> deterministic checks (spec §5.3).

The LLM never sees this column and never judges pass/fail — only this parser
and the Playwright-based checker in actions/verbs.py decide Pass vs Fail.

Grammar: the cell is split on ';' and on top-level ' and ' into clauses. Each
clause is classified into exactly one CheckKind by simple, documented pattern
matching. A clause that matches no pattern becomes kind="unverifiable" and is
reported as such (never silently treated as a pass).

Supported clause patterns (case-insensitive):
    "'<text>' is displayed/shown"        -> text_visible(text)
    "<text> is visible"                  -> text_visible(text)
    "header shows '<text>'"              -> text_visible(text)
    "page titled '<text>'"               -> text_visible(text)
    "url matches/contains <url-or-text>" -> url_contains(value)
    "redirected to <url>"                -> url_contains(url)
    anything else                        -> unverifiable(raw clause)
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Literal

CheckKind = Literal["text_visible", "url_contains", "unverifiable"]

_CLAUSE_SPLIT_RE = re.compile(r";| and (?=[A-Z'\"])")
_QUOTED_RE = re.compile(r"['\"]([^'\"]+)['\"]")
_URL_PATTERN_RE = re.compile(r"(?i)\burl\s+(matches|contains)\s+(.+)$")
_REDIRECT_RE = re.compile(r"(?i)redirected to\s+(.+)$")
_DISPLAYED_RE = re.compile(r"(?i)(is displayed|is shown|appears)\b")


@dataclass
class ExpectedCheck:
    kind: CheckKind
    value: str
    raw_clause: str


def parse_expected_result(expected_result_cell: str) -> list[ExpectedCheck]:
    clauses = [c.strip() for c in _CLAUSE_SPLIT_RE.split(expected_result_cell) if c.strip()]
    return [_parse_clause(c) for c in clauses]


def _parse_clause(clause: str) -> ExpectedCheck:
    redirect_match = _REDIRECT_RE.search(clause)
    if redirect_match:
        return ExpectedCheck("url_contains", redirect_match.group(1).strip().rstrip("."), clause)

    url_match = _URL_PATTERN_RE.search(clause)
    if url_match:
        return ExpectedCheck("url_contains", url_match.group(2).strip().rstrip("."), clause)

    quoted = _QUOTED_RE.search(clause)
    if quoted and _DISPLAYED_RE.search(clause):
        return ExpectedCheck("text_visible", quoted.group(1), clause)

    if quoted and re.search(r"(?i)\bshows\b", clause):
        return ExpectedCheck("text_visible", quoted.group(1), clause)

    if quoted and re.search(r"(?i)\btitled\b", clause):
        return ExpectedCheck("text_visible", quoted.group(1), clause)

    return ExpectedCheck("unverifiable", clause, clause)
