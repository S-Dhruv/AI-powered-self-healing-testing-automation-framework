"""Host allow-list guard. Fails closed: anything not explicitly allowed is refused."""

from __future__ import annotations

from urllib.parse import urlparse


class AllowListViolation(RuntimeError):
    """Raised when navigation/redirect/frame targets a host outside the allow-list."""


class AllowListGuard:
    def __init__(self, allowed_hosts: list[str]) -> None:
        if not allowed_hosts:
            raise ValueError("allowed_hosts must not be empty — refusing to run open-ended")
        self._allowed = {h.lower() for h in allowed_hosts}

    def check(self, url: str) -> None:
        """Raise AllowListViolation if url's host is not on the allow-list."""
        host = (urlparse(url).hostname or "").lower()
        if not host or host not in self._allowed:
            raise AllowListViolation(
                f"Navigation to host '{host or url}' is not on the allow-list "
                f"({sorted(self._allowed)}); refusing to proceed."
            )

    def is_allowed(self, url: str) -> bool:
        try:
            self.check(url)
            return True
        except AllowListViolation:
            return False
