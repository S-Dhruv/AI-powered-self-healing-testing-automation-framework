"""Element store record schema. Backed by SQLite (framework/store/backend.py)."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Literal

Status = Literal["active", "pending", "rejected", "superseded", "invalidated"]
Source = Literal["AI", "Healed"]


@dataclass(frozen=True)
class Strategy:
    """One way of locating an element, in priority order. Never brittle (no
    absolute XPath / DOM index / pixel coordinates / line numbers)."""

    kind: Literal["test_id", "role_name", "label", "text", "css"]
    value: str
    # For role_name strategies: the accessible role (e.g. "button").
    role: str | None = None


@dataclass(frozen=True)
class Fingerprint:
    tag: str
    role: str | None
    accessible_name: str | None
    visible_text: str | None
    attributes: dict[str, str] = field(default_factory=dict)
    parent_landmark: str | None = None

    def to_json(self) -> str:
        return json.dumps(
            {
                "tag": self.tag,
                "role": self.role,
                "accessible_name": self.accessible_name,
                "visible_text": self.visible_text,
                "attributes": self.attributes,
                "parent_landmark": self.parent_landmark,
            },
            sort_keys=True,
        )

    @staticmethod
    def from_json(raw: str) -> "Fingerprint":
        d = json.loads(raw)
        return Fingerprint(
            tag=d["tag"],
            role=d.get("role"),
            accessible_name=d.get("accessible_name"),
            visible_text=d.get("visible_text"),
            attributes=d.get("attributes", {}),
            parent_landmark=d.get("parent_landmark"),
        )


@dataclass
class ElementRecord:
    """A versioned element reference. Store key = app + environment + page + description."""

    id: int | None
    application: str
    environment: str
    page: str
    description: str
    version: int
    strategies: list[Strategy]
    fingerprint: Fingerprint
    status: Status
    source: Source
    run_id: str
    created_at: datetime
    updated_at: datetime
    hit_count: int = 0
    miss_count: int = 0

    @property
    def key(self) -> str:
        return f"{self.application}::{self.environment}::{self.page}::{self.description}"


def utcnow() -> datetime:
    return datetime.now(timezone.utc)
