"""SQLite-backed run history table (spec §5 / R7): date, suite, environment,
pass/fail/blocked counts, total tokens, cache hit rate, heals."""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from pathlib import Path

_SCHEMA = """
CREATE TABLE IF NOT EXISTS run_history (
    run_id TEXT PRIMARY KEY,
    suite TEXT NOT NULL,
    environment TEXT NOT NULL,
    start_time TEXT NOT NULL,
    end_time TEXT NOT NULL,
    total INTEGER NOT NULL,
    passed INTEGER NOT NULL,
    failed INTEGER NOT NULL,
    blocked INTEGER NOT NULL,
    skipped INTEGER NOT NULL,
    total_input_tokens INTEGER NOT NULL,
    total_output_tokens INTEGER NOT NULL,
    estimated_cost REAL NOT NULL,
    cache_hit_rate REAL NOT NULL,
    fallback_rate REAL NOT NULL,
    ai_lookups INTEGER NOT NULL,
    heals_pending INTEGER NOT NULL,
    heals_approved INTEGER NOT NULL,
    heals_rejected INTEGER NOT NULL,
    duration_ms INTEGER NOT NULL
);
"""


@dataclass
class RunHistoryEntry:
    run_id: str
    suite: str
    environment: str
    start_time: str
    end_time: str
    total: int
    passed: int
    failed: int
    blocked: int
    skipped: int
    total_input_tokens: int
    total_output_tokens: int
    estimated_cost: float
    cache_hit_rate: float
    fallback_rate: float
    ai_lookups: int
    heals_pending: int
    heals_approved: int
    heals_rejected: int
    duration_ms: int


class RunHistoryStore:
    def __init__(self, db_path: str | Path) -> None:
        self._path = Path(db_path)
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(self._path)
        self._conn.row_factory = sqlite3.Row
        self._conn.executescript(_SCHEMA)
        self._conn.commit()

    def close(self) -> None:
        self._conn.close()

    def record(self, entry: RunHistoryEntry) -> None:
        self._conn.execute(
            "INSERT OR REPLACE INTO run_history VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                entry.run_id, entry.suite, entry.environment, entry.start_time, entry.end_time,
                entry.total, entry.passed, entry.failed, entry.blocked, entry.skipped,
                entry.total_input_tokens, entry.total_output_tokens, entry.estimated_cost,
                entry.cache_hit_rate, entry.fallback_rate, entry.ai_lookups,
                entry.heals_pending, entry.heals_approved, entry.heals_rejected, entry.duration_ms,
            ),
        )
        self._conn.commit()

    def list_all(self) -> list[sqlite3.Row]:
        cur = self._conn.execute("SELECT * FROM run_history ORDER BY start_time DESC")
        return cur.fetchall()
