"""Persistent cache of one working test account's credentials, keyed by
application + environment. Generic, no app-specific code: whichever app
successfully registers an account during a run makes those credentials
available to later rows (in this run or a future one) that need to
"Login with valid credentials" without ever hardcoding or re-registering one.
"""

from __future__ import annotations

import json
from pathlib import Path


def _cache_path(cache_dir: str | Path) -> Path:
    return Path(cache_dir) / "test_accounts.json"


def load_account(cache_dir: str | Path, application: str, environment: str) -> dict | None:
    path = _cache_path(cache_dir)
    if not path.is_file():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return None
    return data.get(f"{application}:{environment}")


def save_account(
    cache_dir: str | Path, application: str, environment: str, *, email: str, password: str, name: str | None = None
) -> None:
    path = _cache_path(cache_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    data: dict = {}
    if path.is_file():
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            data = {}
    data[f"{application}:{environment}"] = {"email": email, "password": password, "name": name}
    path.write_text(json.dumps(data, indent=2), encoding="utf-8")


def clear_account(cache_dir: str | Path, application: str, environment: str) -> bool:
    """Forgets this app's cached test account, so the next row that needs to
    log in registers a brand-new one instead of reusing credentials from a
    possibly-contaminated prior run (an account that was mid-checkout, had
    items left in its cart, or got deleted on-site by an earlier row).
    Returns True if there was an entry to remove."""
    path = _cache_path(cache_dir)
    if not path.is_file():
        return False
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return False
    key = f"{application}:{environment}"
    if key not in data:
        return False
    del data[key]
    path.write_text(json.dumps(data, indent=2), encoding="utf-8")
    return True
