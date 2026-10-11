"""SQLite-backed element store. Versioned, partitioned by application + environment."""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

from framework.store.models import ElementRecord, Fingerprint, Status, Strategy, utcnow

_SCHEMA = """
CREATE TABLE IF NOT EXISTS element_records (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    application TEXT NOT NULL,
    environment TEXT NOT NULL,
    page TEXT NOT NULL,
    description TEXT NOT NULL,
    version INTEGER NOT NULL,
    strategies_json TEXT NOT NULL,
    fingerprint_json TEXT NOT NULL,
    status TEXT NOT NULL,
    source TEXT NOT NULL,
    run_id TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    hit_count INTEGER NOT NULL DEFAULT 0,
    miss_count INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_element_key
    ON element_records(application, environment, page, description, status);
"""


def _row_to_record(row: sqlite3.Row) -> ElementRecord:
    strategies = [Strategy(**s) for s in json.loads(row["strategies_json"])]
    return ElementRecord(
        id=row["id"],
        application=row["application"],
        environment=row["environment"],
        page=row["page"],
        description=row["description"],
        version=row["version"],
        strategies=strategies,
        fingerprint=Fingerprint.from_json(row["fingerprint_json"]),
        status=row["status"],
        source=row["source"],
        run_id=row["run_id"],
        created_at=row["created_at"],
        updated_at=row["updated_at"],
        hit_count=row["hit_count"],
        miss_count=row["miss_count"],
    )


class ElementStore:
    def __init__(self, db_path: str | Path) -> None:
        self._path = Path(db_path)
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(self._path)
        self._conn.row_factory = sqlite3.Row
        self._conn.executescript(_SCHEMA)
        self._conn.commit()

    def close(self) -> None:
        self._conn.close()

    def get_active(
        self, application: str, environment: str, page: str, description: str
    ) -> ElementRecord | None:
        cur = self._conn.execute(
            "SELECT * FROM element_records WHERE application=? AND environment=? "
            "AND page=? AND description=? AND status='active' ORDER BY version DESC LIMIT 1",
            (application, environment, page, description),
        )
        row = cur.fetchone()
        return _row_to_record(row) if row else None

    def history(
        self, application: str, environment: str, page: str, description: str
    ) -> list[ElementRecord]:
        cur = self._conn.execute(
            "SELECT * FROM element_records WHERE application=? AND environment=? "
            "AND page=? AND description=? ORDER BY version ASC",
            (application, environment, page, description),
        )
        return [_row_to_record(r) for r in cur.fetchall()]

    def list_pending(self, application: str | None = None) -> list[ElementRecord]:
        if application:
            cur = self._conn.execute(
                "SELECT * FROM element_records WHERE status='pending' AND application=? "
                "ORDER BY updated_at ASC",
                (application,),
            )
        else:
            cur = self._conn.execute(
                "SELECT * FROM element_records WHERE status='pending' ORDER BY updated_at ASC"
            )
        return [_row_to_record(r) for r in cur.fetchall()]

    def list_all(self, application: str | None = None) -> list[ElementRecord]:
        if application:
            cur = self._conn.execute(
                "SELECT * FROM element_records WHERE application=? ORDER BY updated_at DESC",
                (application,),
            )
        else:
            cur = self._conn.execute("SELECT * FROM element_records ORDER BY updated_at DESC")
        return [_row_to_record(r) for r in cur.fetchall()]

    def get_by_id(self, record_id: int) -> ElementRecord | None:
        cur = self._conn.execute("SELECT * FROM element_records WHERE id=?", (record_id,))
        row = cur.fetchone()
        return _row_to_record(row) if row else None

    def insert(
        self,
        *,
        application: str,
        environment: str,
        page: str,
        description: str,
        strategies: list[Strategy],
        fingerprint: Fingerprint,
        status: Status,
        source: str,
        run_id: str,
    ) -> ElementRecord:
        """Insert a new version. Caller decides status (active on first_learning,
        pending on heal unless auto-approve)."""
        existing = self.history(application, environment, page, description)
        next_version = (max((r.version for r in existing), default=0)) + 1
        now = utcnow().isoformat()
        cur = self._conn.execute(
            "INSERT INTO element_records (application, environment, page, description, "
            "version, strategies_json, fingerprint_json, status, source, run_id, "
            "created_at, updated_at, hit_count, miss_count) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,0,0)",
            (
                application,
                environment,
                page,
                description,
                next_version,
                json.dumps([vars(s) for s in strategies]),
                fingerprint.to_json(),
                status,
                source,
                run_id,
                now,
                now,
            ),
        )
        self._conn.commit()
        return self.get_by_id(cur.lastrowid)

    def set_status(self, record_id: int, status: Status) -> None:
        self._conn.execute(
            "UPDATE element_records SET status=?, updated_at=? WHERE id=?",
            (status, utcnow().isoformat(), record_id),
        )
        self._conn.commit()

    def record_hit(self, record_id: int) -> None:
        self._conn.execute(
            "UPDATE element_records SET hit_count = hit_count + 1, updated_at=? WHERE id=?",
            (utcnow().isoformat(), record_id),
        )
        self._conn.commit()

    def record_miss(self, record_id: int) -> None:
        self._conn.execute(
            "UPDATE element_records SET miss_count = miss_count + 1, updated_at=? WHERE id=?",
            (utcnow().isoformat(), record_id),
        )
        self._conn.commit()

    def invalidate(
        self,
        *,
        application: str,
        environment: str | None = None,
        page: str | None = None,
        description: str | None = None,
    ) -> int:
        """Invalidate active records matching the given filter. Returns count affected."""
        clauses = ["application=?", "status='active'"]
        params: list[str] = [application]
        if environment:
            clauses.append("environment=?")
            params.append(environment)
        if page:
            clauses.append("page=?")
            params.append(page)
        if description:
            clauses.append("description=?")
            params.append(description)
        cur = self._conn.execute(
            f"UPDATE element_records SET status='invalidated', updated_at=? WHERE {' AND '.join(clauses)}",
            [utcnow().isoformat(), *params],
        )
        self._conn.commit()
        return cur.rowcount

    def purge(self, *, application: str, environment: str | None = None) -> int:
        """Hard-deletes every record (any status, any version) for this app --
        not a status flip. invalidate() only ever retires active records, so
        a long run of experiments leaves hundreds of invalidated/superseded
        versions behind in the database even after "clearing the cache" --
        still queryable, still showing up in `store history`, and still
        meaning resolve.py's had_prior_record (any history at all, not just
        an active record) treats the element as mid-heal rather than a clean
        first learn. purge() is a genuine "forget this app and start over":
        the next run resolves and stores every element completely fresh, with
        no earlier version lingering anywhere. Returns count removed."""
        clauses = ["application=?"]
        params: list[str] = [application]
        if environment:
            clauses.append("environment=?")
            params.append(environment)
        cur = self._conn.execute(
            f"DELETE FROM element_records WHERE {' AND '.join(clauses)}", params
        )
        self._conn.commit()
        return cur.rowcount

    def export(self) -> list[dict]:
        return [vars(r) for r in self.list_all()]
