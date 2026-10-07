"""Heal review workflow: list / approve / reject / approve-all.

healing.mode is driven by Settings.healing_auto_approve (see config/schema.py).
Even in auto mode, every heal is still logged and flagged — auto-approve is not
a silent bypass, it is a policy applied through this same code path.
"""

from __future__ import annotations

from dataclasses import dataclass

from framework.store.backend import ElementStore
from framework.store.models import ElementRecord


class ReviewError(RuntimeError):
    pass


@dataclass
class ReviewDecision:
    record_id: int
    approved: bool
    reason: str


class ReviewQueue:
    def __init__(self, store: ElementStore) -> None:
        self._store = store

    def list_pending(self, application: str | None = None) -> list[ElementRecord]:
        return self._store.list_pending(application)

    def approve(self, record_id: int, reason: str = "manual approval") -> ElementRecord:
        record = self._store.get_by_id(record_id)
        if record is None:
            raise ReviewError(f"No record with id {record_id}")
        if record.status != "pending":
            raise ReviewError(f"Record {record_id} is '{record.status}', not pending")
        self._supersede_prior_active(record)
        self._store.set_status(record_id, "active")
        return self._store.get_by_id(record_id)

    def reject(self, record_id: int, reason: str = "manual rejection") -> ElementRecord:
        record = self._store.get_by_id(record_id)
        if record is None:
            raise ReviewError(f"No record with id {record_id}")
        if record.status != "pending":
            raise ReviewError(f"Record {record_id} is '{record.status}', not pending")
        # Rejected heals revert to the previous version; the old active record,
        # if any, is left untouched (it was never demoted while this was pending).
        self._store.set_status(record_id, "rejected")
        return self._store.get_by_id(record_id)

    def approve_all(self, run_id: str | None = None) -> list[ElementRecord]:
        pending = self._store.list_pending()
        if run_id:
            pending = [p for p in pending if p.run_id == run_id]
        return [self.approve(p.id, reason=f"approve-all run={run_id}") for p in pending]

    def auto_approve_if_configured(
        self, record: ElementRecord, auto_approve_enabled: bool, min_confidence_met: bool
    ) -> ElementRecord:
        """Called right after a heal is stored as pending. If healing.mode=auto
        and the verification/confidence threshold was met, auto-approve now —
        still going through the same approve() path so it is logged identically."""
        if auto_approve_enabled and min_confidence_met:
            return self.approve(record.id, reason="auto-approved (healing.mode=auto)")
        return record

    def _supersede_prior_active(self, new_record: ElementRecord) -> None:
        prior = self._store.get_active(
            new_record.application,
            new_record.environment,
            new_record.page,
            new_record.description,
        )
        if prior and prior.id != new_record.id:
            self._store.set_status(prior.id, "superseded")
