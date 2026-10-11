"""Persistent, versioned element reference store (one per application + environment).

Each entry keeps an active reference, an optional pending (healed, awaiting review) reference
and the history of retired versions. Plans (LLM step -> actions) are cached next to it.
"""

import hashlib
import json
import logging
import shutil
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


def make_key(page: str, target: str) -> str:
    return hashlib.md5(f"{page}|{target}".encode("utf-8")).hexdigest()[:16]


class _JsonFile:
    def __init__(self, path: Path):
        self.path = path
        self.data: Dict[str, Any] = {}
        if path.exists():
            try:
                self.data = json.loads(path.read_text(encoding="utf-8"))
            except Exception as exc:
                logger.error("Could not read %s: %s", path, exc)

    def save(self):
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(self.data, indent=2, default=str), encoding="utf-8")
        tmp.replace(self.path)


class ElementStore:
    def __init__(self, base_dir: str, app: str, environment: str, auto_approve: bool = False):
        self.app, self.environment, self.auto_approve = app, environment, auto_approve
        self.dir = Path(base_dir) / app / environment
        self.dir.mkdir(parents=True, exist_ok=True)
        self.backup_dir = self.dir / "backups"
        self.backup_dir.mkdir(exist_ok=True)
        self._file = _JsonFile(self.dir / "elements.json")
        self.plans = _JsonFile(self.dir / "plans.json")

    @property
    def data(self) -> Dict[str, Any]:
        return self._file.data

    def save(self):
        self._file.save()

    def backup(self):
        if self._file.path.exists():
            stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
            shutil.copy(self._file.path, self.backup_dir / f"elements_{stamp}.json")

    # ------------------------------------------------------------------ read

    def get(self, page: str, target: str) -> Optional[Dict[str, Any]]:
        return self.data.get(make_key(page, target))

    def entries(self, page_filter: Optional[str] = None) -> List[Dict[str, Any]]:
        rows = list(self.data.values())
        if page_filter:
            rows = [r for r in rows if page_filter.lower() in r["page"].lower()]
        return sorted(rows, key=lambda r: (r["page"], r["target"]))

    def pending(self) -> List[Dict[str, Any]]:
        return [e for e in self.data.values() if e.get("pending")]

    # ------------------------------------------------------------------ write

    def learn(self, page: str, target: str, locators: Dict[str, Any], fingerprint: Dict[str, Any],
              run_id: str, model: str) -> Dict[str, Any]:
        key = make_key(page, target)
        entry = {
            "key": key, "app": self.app, "environment": self.environment, "page": page, "target": target,
            "locators": locators, "fingerprint": fingerprint, "version": 1,
            "learned_at": _now(), "learned_by_run": run_id, "model": model,
            "last_used": _now(), "usage_count": 0, "pending": None, "history": [],
        }
        self.data[key] = entry
        self.save()
        return entry

    def propose_heal(self, key: str, locators: Dict[str, Any], fingerprint: Dict[str, Any],
                     run_id: str, model: str, reason: str) -> str:
        """Returns 'promoted' (auto-approve) or 'pending' (awaiting review)."""
        entry = self.data[key]
        self.backup()
        proposal = {"locators": locators, "fingerprint": fingerprint, "healed_at": _now(),
                    "run_id": run_id, "model": model, "reason": reason}
        if self.auto_approve:
            self._promote(entry, proposal, "auto-approved")
            self.save()
            return "promoted"
        entry["pending"] = proposal
        self.save()
        return "pending"

    def _promote(self, entry: Dict[str, Any], proposal: Dict[str, Any], how: str):
        entry["history"].insert(0, {
            "version": entry["version"], "locators": entry["locators"], "fingerprint": entry["fingerprint"],
            "learned_at": entry["learned_at"], "retired_at": _now(), "retired_because": how,
        })
        entry["locators"], entry["fingerprint"] = proposal["locators"], proposal["fingerprint"]
        entry["version"] += 1
        entry["learned_at"] = proposal["healed_at"]
        entry["healing_note"] = f"Healed {proposal['healed_at']} ({how}): {proposal['reason']}"
        entry["pending"] = None

    def approve(self, key: str) -> bool:
        entry = self.data.get(key)
        if not entry or not entry.get("pending"):
            return False
        self.backup()
        self._promote(entry, entry["pending"], "approved by reviewer")
        self.save()
        return True

    def reject(self, key: str) -> bool:
        entry = self.data.get(key)
        if not entry or not entry.get("pending"):
            return False
        entry["pending"] = None
        entry["healing_note"] = f"Healed reference rejected by reviewer {_now()}"
        self.save()
        return True

    def touch(self, key: str):
        entry = self.data.get(key)
        if entry:
            entry["usage_count"] += 1
            entry["last_used"] = _now()

    def invalidate(self, key: str) -> bool:
        if key in self.data:
            self.backup()
            del self.data[key]
            self.save()
            return True
        return False

    def invalidate_page(self, page_filter: str) -> int:
        keys = [k for k, v in self.data.items() if page_filter.lower() in v["page"].lower()]
        if keys:
            self.backup()
            for key in keys:
                del self.data[key]
            self.save()
        return len(keys)

    # ------------------------------------------------------------------ reporting

    def export(self, path: str):
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        Path(path).write_text(json.dumps(
            {"app": self.app, "environment": self.environment, "exported_at": _now(),
             "elements": list(self.data.values()), "plans": self.plans.data}, indent=2, default=str),
            encoding="utf-8")

    def stats(self) -> Dict[str, Any]:
        entries = list(self.data.values())
        return {
            "total_elements": len(entries),
            "pages": len({e["page"] for e in entries}),
            "pending_review": len([e for e in entries if e.get("pending")]),
            "healed_versions": sum(len(e["history"]) for e in entries),
            "plans_cached": len(self.plans.data),
        }

    # ------------------------------------------------------------------ plans

    def get_plan(self, key: str) -> Optional[Dict[str, Any]]:
        return self.plans.data.get(key)

    def put_plan(self, key: str, step: str, actions: List[Dict[str, Any]], model: str):
        self.plans.data[key] = {"step": step, "actions": actions, "model": model, "learned_at": _now()}
        self.plans.save()
