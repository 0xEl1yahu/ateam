"""SQLite state store: state machines, attempts, telemetry, checkpoints, run lock."""

from __future__ import annotations

import json
import sqlite3
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal

SCHEMA_VERSION = 1

FEATURE_TRANSITIONS: dict[str | None, set[str]] = {
    None: {"requested"},
    "requested": {"investigating"},
    "investigating": {"plan_proposed"},
    "plan_proposed": {"plan_approved", "plan_rejected"},
    "plan_approved": {"executing"},
    "executing": {"paused", "integrating"},
    "paused": {"executing", "abandoned"},
    "integrating": {"merge_proposed"},
    "merge_proposed": {"merged", "plan_rejected"},
    "plan_rejected": set(),
    "merged": set(),
    "abandoned": set(),
}

CONTRACT_TRANSITIONS: dict[str | None, set[str]] = {
    None: {"draft", "ready"},
    "draft": {"ready"},
    "ready": {"building"},
    "building": {"verifying", "failed", "paused"},
    "verifying": {"verified", "failed", "paused"},
    "failed": {"building", "paused"},
    "paused": {"building", "ready"},
    "verified": {"integrated"},
    "integrated": set(),
}

Kind = Literal["feature", "contract"]

_SCHEMA = """
CREATE TABLE IF NOT EXISTS schema_version (version INTEGER NOT NULL);
CREATE TABLE IF NOT EXISTS record_hashes (record_id TEXT PRIMARY KEY, hash TEXT NOT NULL, at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS features (feature_id TEXT PRIMARY KEY, status TEXT NOT NULL, updated_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS contracts (
    contract_id TEXT PRIMARY KEY, plan_id TEXT NOT NULL, status TEXT NOT NULL,
    content_hash TEXT NOT NULL, updated_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS transitions (
    seq INTEGER PRIMARY KEY AUTOINCREMENT, kind TEXT NOT NULL, id TEXT NOT NULL,
    from_status TEXT, to_status TEXT NOT NULL, by TEXT NOT NULL, reason TEXT, at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS attempts (
    attempt_id INTEGER PRIMARY KEY AUTOINCREMENT, contract_id TEXT NOT NULL, role TEXT NOT NULL,
    tier TEXT NOT NULL, model TEXT NOT NULL,
    predicted_complexity TEXT, predicted_risk TEXT, predicted_uncertainty TEXT,
    started_at TEXT NOT NULL, finished_at TEXT, outcome TEXT, failure_class TEXT,
    tokens_in INTEGER, tokens_out INTEGER, cost_usd REAL, turns INTEGER, wall_s REAL, session_id TEXT);
CREATE TABLE IF NOT EXISTS checkpoints (
    seq INTEGER PRIMARY KEY AUTOINCREMENT, contract_id TEXT NOT NULL, attempt_id INTEGER,
    facts TEXT NOT NULL, at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL, at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS run_lock (
    singleton INTEGER PRIMARY KEY CHECK (singleton = 1), plan_id TEXT NOT NULL, acquired_at TEXT NOT NULL, pid INTEGER);
"""


class InvalidTransition(Exception):
    pass


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


class StateDB:
    def __init__(self, path: Path) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(self.path)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA journal_mode=WAL")

    # -- schema -------------------------------------------------------------

    def init_schema(self) -> None:
        with self.conn:
            cols = [r["name"] for r in self.conn.execute("PRAGMA table_info(run_lock)")]
            if cols and "singleton" not in cols:  # pre-release shape without the uniqueness constraint
                self.conn.execute("DROP TABLE run_lock")
            self.conn.executescript(_SCHEMA)
            if self.conn.execute("SELECT COUNT(*) FROM schema_version").fetchone()[0] == 0:
                self.conn.execute("INSERT INTO schema_version (version) VALUES (?)", (SCHEMA_VERSION,))

    def schema_version(self) -> int:
        return int(self.conn.execute("SELECT version FROM schema_version").fetchone()[0])

    # -- meta ---------------------------------------------------------------

    def set_meta(self, key: str, value: str) -> None:
        with self.conn:
            self.conn.execute(
                "INSERT INTO meta (key, value, at) VALUES (?, ?, ?) ON CONFLICT(key) DO UPDATE SET value=excluded.value, at=excluded.at",
                (key, value, _now()),
            )

    def get_meta(self, key: str) -> str | None:
        row = self.conn.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
        return row["value"] if row else None

    # -- records ------------------------------------------------------------

    def set_record_hash(self, record_id: str, hash: str) -> None:
        with self.conn:
            self.conn.execute(
                "INSERT INTO record_hashes (record_id, hash, at) VALUES (?, ?, ?) "
                "ON CONFLICT(record_id) DO UPDATE SET hash=excluded.hash, at=excluded.at",
                (record_id, hash, _now()),
            )

    def record_hashes(self) -> dict[str, str]:
        return {r["record_id"]: r["hash"] for r in self.conn.execute("SELECT record_id, hash FROM record_hashes")}

    # -- contracts and features --------------------------------------------

    def upsert_contract(self, contract_id: str, plan_id: str, status: str, content_hash: str) -> None:
        with self.conn:
            self.conn.execute(
                "INSERT INTO contracts (contract_id, plan_id, status, content_hash, updated_at) VALUES (?, ?, ?, ?, ?) "
                "ON CONFLICT(contract_id) DO UPDATE SET plan_id=excluded.plan_id, status=excluded.status, "
                "content_hash=excluded.content_hash, updated_at=excluded.updated_at",
                (contract_id, plan_id, status, content_hash, _now()),
            )

    def contract_status(self, contract_id: str) -> str | None:
        row = self.conn.execute("SELECT status FROM contracts WHERE contract_id=?", (contract_id,)).fetchone()
        return row["status"] if row else None

    def contract_hash(self, contract_id: str) -> str | None:
        row = self.conn.execute("SELECT content_hash FROM contracts WHERE contract_id=?", (contract_id,)).fetchone()
        return row["content_hash"] if row else None

    def feature_status(self, feature_id: str) -> str | None:
        row = self.conn.execute("SELECT status FROM features WHERE feature_id=?", (feature_id,)).fetchone()
        return row["status"] if row else None

    def max_feature_id(self) -> str | None:
        row = self.conn.execute("SELECT MAX(feature_id) AS m FROM features").fetchone()
        return row["m"] if row and row["m"] else None

    def contracts_for_plan(self, plan_id: str) -> list[dict[str, Any]]:
        return [dict(r) for r in self.conn.execute("SELECT * FROM contracts WHERE plan_id=? ORDER BY contract_id", (plan_id,))]

    def transition(self, kind: Kind, id: str, to: str, by: str, reason: str | None = None) -> None:
        table = FEATURE_TRANSITIONS if kind == "feature" else CONTRACT_TRANSITIONS
        current = self.feature_status(id) if kind == "feature" else self.contract_status(id)
        if to not in table.get(current, set()):
            raise InvalidTransition(f"{kind} {id}: {current} -> {to} is not allowed")
        with self.conn:
            self.conn.execute(
                "INSERT INTO transitions (kind, id, from_status, to_status, by, reason, at) VALUES (?, ?, ?, ?, ?, ?, ?)",
                (kind, id, current, to, by, reason, _now()),
            )
            if kind == "feature":
                self.conn.execute(
                    "INSERT INTO features (feature_id, status, updated_at) VALUES (?, ?, ?) "
                    "ON CONFLICT(feature_id) DO UPDATE SET status=excluded.status, updated_at=excluded.updated_at",
                    (id, to, _now()),
                )
            else:
                self.conn.execute("UPDATE contracts SET status=?, updated_at=? WHERE contract_id=?", (to, _now(), id))

    def transitions(self, kind: Kind, id: str) -> list[dict[str, Any]]:
        rows = self.conn.execute("SELECT * FROM transitions WHERE kind=? AND id=? ORDER BY seq", (kind, id))
        return [dict(r) for r in rows]

    # -- attempts and telemetry --------------------------------------------

    def start_attempt(self, contract_id: str, role: str, tier: str, model: str, predicted: dict[str, str]) -> int:
        with self.conn:
            cur = self.conn.execute(
                "INSERT INTO attempts (contract_id, role, tier, model, predicted_complexity, predicted_risk, "
                "predicted_uncertainty, started_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (contract_id, role, tier, model, predicted.get("complexity"), predicted.get("risk"),
                 predicted.get("uncertainty"), _now()),
            )
            return int(cur.lastrowid)

    def finish_attempt(
        self, attempt_id: int, outcome: str, failure_class: str | None, tokens_in: int, tokens_out: int,
        cost_usd: float, turns: int, wall_s: float, session_id: str | None,
    ) -> None:
        with self.conn:
            self.conn.execute(
                "UPDATE attempts SET finished_at=?, outcome=?, failure_class=?, tokens_in=?, tokens_out=?, "
                "cost_usd=?, turns=?, wall_s=?, session_id=? WHERE attempt_id=?",
                (_now(), outcome, failure_class, tokens_in, tokens_out, cost_usd, turns, wall_s, session_id, attempt_id),
            )

    def latest_attempt(self, contract_id: str) -> dict[str, Any] | None:
        row = self.conn.execute(
            "SELECT * FROM attempts WHERE contract_id=? ORDER BY attempt_id DESC LIMIT 1", (contract_id,)
        ).fetchone()
        return dict(row) if row else None

    def telemetry_rows(self) -> list[dict[str, Any]]:
        return [dict(r) for r in self.conn.execute("SELECT * FROM attempts ORDER BY attempt_id")]

    # -- checkpoints --------------------------------------------------------

    def set_checkpoint(self, contract_id: str, attempt_id: int | None, facts: dict[str, Any]) -> None:
        with self.conn:
            self.conn.execute(
                "INSERT INTO checkpoints (contract_id, attempt_id, facts, at) VALUES (?, ?, ?, ?)",
                (contract_id, attempt_id, json.dumps(facts, default=str), _now()),
            )

    def latest_checkpoint(self, contract_id: str) -> dict[str, Any] | None:
        row = self.conn.execute(
            "SELECT facts FROM checkpoints WHERE contract_id=? ORDER BY seq DESC LIMIT 1", (contract_id,)
        ).fetchone()
        return json.loads(row["facts"]) if row else None

    # -- run lock -----------------------------------------------------------

    def acquire_run_lock(self, plan_id: str) -> bool:
        """True when this plan holds the single run lock. Exclusive across processes via the singleton row."""
        import os

        try:
            with self.conn:
                self.conn.execute(
                    "INSERT INTO run_lock (singleton, plan_id, acquired_at, pid) VALUES (1, ?, ?, ?)",
                    (plan_id, _now(), os.getpid()),
                )
            return True
        except sqlite3.IntegrityError:
            return self.run_lock_holder() == plan_id

    def release_run_lock(self, plan_id: str) -> None:
        with self.conn:
            self.conn.execute("DELETE FROM run_lock WHERE plan_id=?", (plan_id,))

    def run_lock_holder(self) -> str | None:
        row = self.conn.execute("SELECT plan_id FROM run_lock LIMIT 1").fetchone()
        return row["plan_id"] if row else None
