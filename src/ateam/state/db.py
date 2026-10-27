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
    "investigating": {"plan_proposed", "plan_covered", "plan_rejected"},   # plan_rejected: interrupted, or an unusable report
    "plan_proposed": {"plan_approved", "plan_rejected", "superseded"},
    "plan_approved": {"executing", "superseded", "plan_rejected"},   # superseded or rejected only before any of its contracts has started
    "executing": {"paused", "integrating"},
    "paused": {"executing", "abandoned"},
    "integrating": {"merge_proposed", "paused"},             # paused: a conflict, a setup failure or failed final checks
    "merge_proposed": {"merged", "plan_rejected", "integrating"},   # integrating: the target moved after the proposal
    "plan_rejected": set(),
    "merged": set(),
    "abandoned": set(),
    "plan_covered": set(),   # terminal; entered from investigating by `ateam plan`
    "superseded": set(),     # terminal; entered from plan_proposed, or plan_approved before work starts, when a replacing plan is approved
}

CONTRACT_TRANSITIONS: dict[str | None, set[str]] = {
    None: {"draft", "ready"},
    "draft": {"ready", "superseded"},
    "ready": {"building", "superseded"},
    "building": {"verifying", "failed", "paused"},
    "verifying": {"verified", "failed", "paused"},
    "failed": {"building", "paused"},
    "paused": {"building", "ready"},
    "verified": {"integrated", "building"},   # building: only the rebase BUILD after a merge conflict (owner, 2026-10-27)
    "integrated": set(),
    "superseded": set(),     # terminal; its plan was superseded before the contract started
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
CREATE TABLE IF NOT EXISTS pauses (
    pause_id INTEGER PRIMARY KEY AUTOINCREMENT, plan_id TEXT NOT NULL, reason TEXT NOT NULL, detail TEXT NOT NULL,
    conflict INTEGER NOT NULL DEFAULT 0, at TEXT NOT NULL, resumed_at TEXT);
CREATE TABLE IF NOT EXISTS pause_contracts (
    pause_id INTEGER NOT NULL, contract_id TEXT NOT NULL, reason TEXT NOT NULL, detail TEXT NOT NULL,
    contract_hash TEXT NOT NULL, evidence TEXT, attempt_id INTEGER, findings TEXT, note TEXT,
    PRIMARY KEY (pause_id, contract_id));
CREATE TABLE IF NOT EXISTS attempt_windows (
    window_id INTEGER PRIMARY KEY AUTOINCREMENT, contract_id TEXT NOT NULL, after_attempt_id INTEGER NOT NULL,
    pause_id INTEGER, at TEXT NOT NULL);
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
        self.conn.execute("PRAGMA busy_timeout=10000")   # worker processes and the scheduler write attempts concurrently
        self._closed = False

    def close(self) -> None:
        """Close the connection; a second call is a no-op. Only the thread that opened it may (check_same_thread)."""
        if not self._closed:
            self.conn.close()
            self._closed = True

    def __enter__(self) -> StateDB:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

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

    def delete_meta(self, *keys: str) -> None:
        """Remove every key in one transaction."""
        with self.conn:
            for key in keys:
                self.conn.execute("DELETE FROM meta WHERE key=?", (key,))

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

    def features_with_status(self, status: str) -> list[str]:
        return [r["feature_id"] for r in self.conn.execute("SELECT feature_id FROM features WHERE status=? ORDER BY feature_id", (status,))]

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

    def finish_verification(
        self, contract_id: str, start: str, steps: list[str], by: str, reason: str, checkpoint: dict[str, Any],
        clear_keys: list[str],
    ) -> None:
        """Record a verification in one transaction: the transitions, the final status, the checkpoint, and the
        removal of the verify markers in `clear_keys`. Steps are checked from `start`, the status seen before the checks ran, so a
        store row rewritten behind the verifier's back cannot change the walk; nothing is written if a step is invalid."""
        current = start
        for to in steps:
            if to not in CONTRACT_TRANSITIONS.get(current, set()):
                raise InvalidTransition(f"contract {contract_id}: {current} -> {to} is not allowed")
            current = to
        with self.conn:
            current = start
            for to in steps:
                self.conn.execute(
                    "INSERT INTO transitions (kind, id, from_status, to_status, by, reason, at) VALUES (?, ?, ?, ?, ?, ?, ?)",
                    ("contract", contract_id, current, to, by, reason, _now()),
                )
                current = to
            self.conn.execute("UPDATE contracts SET status=?, updated_at=? WHERE contract_id=?", (current, _now(), contract_id))
            self.conn.execute(
                "INSERT INTO checkpoints (contract_id, attempt_id, facts, at) VALUES (?, ?, ?, ?)",
                (contract_id, None, json.dumps(checkpoint, default=str), _now()),
            )
            for key in clear_keys:
                self.conn.execute("DELETE FROM meta WHERE key=?", (key,))

    def supersede(self, plan_id: str, plan_from: str, contract_steps: dict[str, str], by: str, reason: str) -> None:
        """Supersede a plan and its contracts in one transaction (see retire)."""
        self.retire(plan_id, plan_from, contract_steps, by, reason, to="superseded")

    def retire(self, plan_id: str, plan_from: str, contract_steps: dict[str, str], by: str, reason: str,
               to: str = "superseded") -> None:
        """Move a plan to `to` and its contracts to superseded in one transaction. `plan_from` and each value of
        `contract_steps` are the statuses seen before the intent commit; nothing is written if the store no longer
        agrees or a step is invalid."""
        steps = [("feature", plan_id, plan_from, to), *(("contract", cid, frm, "superseded") for cid, frm in contract_steps.items())]
        for kind, id, frm, dest in steps:
            current = self.feature_status(id) if kind == "feature" else self.contract_status(id)
            table = FEATURE_TRANSITIONS if kind == "feature" else CONTRACT_TRANSITIONS
            if current != frm or dest not in table.get(current, set()):
                raise InvalidTransition(f"{kind} {id}: {current} -> {dest} is not allowed (expected {frm})")
        with self.conn:
            for kind, id, frm, dest in reversed(steps):   # contracts first, the feature last
                self.conn.execute(
                    "INSERT INTO transitions (kind, id, from_status, to_status, by, reason, at) VALUES (?, ?, ?, ?, ?, ?, ?)",
                    (kind, id, frm, dest, by, reason, _now()),
                )
                if kind == "feature":
                    self.conn.execute("UPDATE features SET status=?, updated_at=? WHERE feature_id=?", (dest, _now(), id))
                else:
                    self.conn.execute("UPDATE contracts SET status=?, updated_at=? WHERE contract_id=?", (dest, _now(), id))

    def work_traces(self, contract_id: str) -> list[str]:
        """What the store holds that shows work started on a contract. Freezing leaves no trace; anything else does."""
        traces = []
        if self.latest_attempt(contract_id) is not None:
            traces.append("an attempt")
        if self.transitions("contract", contract_id):
            traces.append("a contract transition")
        if self.latest_checkpoint(contract_id) is not None:
            traces.append("a checkpoint")
        if self.get_meta(f"verify-pending:{contract_id}") or self.get_meta(f"verify-running:{contract_id}"):
            traces.append("a verify marker")
        return traces

    def fingerprint(self, contract_id: str, records: bool = True) -> str:
        """A hash of everything the store holds about one contract, plus every record hash unless `records` is False,
        as committed (writes by other connections included). Other contracts' rows are left out, so parallel work
        does not change it."""
        import hashlib

        queries = [
            ("SELECT * FROM contracts WHERE contract_id=?", (contract_id,)),
            ("SELECT * FROM transitions WHERE kind='contract' AND id=? ORDER BY seq", (contract_id,)),
            ("SELECT * FROM checkpoints WHERE contract_id=? ORDER BY seq", (contract_id,)),
            ("SELECT * FROM attempts WHERE contract_id=? ORDER BY attempt_id", (contract_id,)),
            ("SELECT * FROM meta WHERE key LIKE 'verify-%:' || ? ORDER BY key", (contract_id,)),
        ]
        if records:
            queries.append(("SELECT record_id, hash FROM record_hashes ORDER BY record_id", ()))
        h = hashlib.sha256()
        for sql, args in queries:
            for row in self.conn.execute(sql, args):
                h.update(repr(tuple(row)).encode())
            h.update(b"|")
        return h.hexdigest()

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

    def attempts_for(self, contract_id: str, role: str | None = None) -> list[dict[str, Any]]:
        sql, args = "SELECT * FROM attempts WHERE contract_id=?", [contract_id]
        if role is not None:
            sql, args = sql + " AND role=?", [*args, role]
        return [dict(r) for r in self.conn.execute(sql + " ORDER BY attempt_id", args)]

    def open_attempts(self, contract_ids: list[str]) -> list[dict[str, Any]]:
        """Attempts of these contracts that never finished (a killed or interrupted session)."""
        marks = ",".join("?" * len(contract_ids)) or "NULL"
        rows = self.conn.execute(
            f"SELECT * FROM attempts WHERE finished_at IS NULL AND contract_id IN ({marks}) ORDER BY attempt_id", contract_ids)
        return [dict(r) for r in rows]

    def interrupt_attempt(self, attempt_id: int) -> None:
        """Close an unfinished attempt as interrupted; a finished one is left as it is."""
        with self.conn:
            self.conn.execute("UPDATE attempts SET finished_at=?, outcome='interrupted', failure_class=NULL "
                              "WHERE attempt_id=? AND finished_at IS NULL", (_now(), attempt_id))

    def telemetry_rows(self) -> list[dict[str, Any]]:
        return [dict(r) for r in self.conn.execute("SELECT * FROM attempts ORDER BY attempt_id")]

    # -- checkpoints --------------------------------------------------------

    def set_checkpoint(self, contract_id: str, attempt_id: int | None, facts: dict[str, Any]) -> None:
        with self.conn:
            self.conn.execute(
                "INSERT INTO checkpoints (contract_id, attempt_id, facts, at) VALUES (?, ?, ?, ?)",
                (contract_id, attempt_id, json.dumps(facts, default=str), _now()),
            )

    def latest_checkpoint(self, contract_id: str, role: str | None = None) -> dict[str, Any] | None:
        """The latest checkpoint of a contract, or of the attempts of one role (joined through attempt_id)."""
        if role is None:
            row = self.conn.execute(
                "SELECT facts FROM checkpoints WHERE contract_id=? ORDER BY seq DESC LIMIT 1", (contract_id,)
            ).fetchone()
        else:
            row = self.conn.execute(
                "SELECT c.facts FROM checkpoints c JOIN attempts a ON a.attempt_id = c.attempt_id "
                "WHERE c.contract_id=? AND a.role=? ORDER BY c.seq DESC LIMIT 1", (contract_id, role),
            ).fetchone()
        return json.loads(row["facts"]) if row else None

    # -- run lock -----------------------------------------------------------

    def acquire_run_lock(self, plan_id: str) -> bool:
        """True when this process holds the single run lock for this plan. Exclusive across processes via the
        singleton row; a row held by another pid, or by an unknown one, is never taken over (a stale lock after
        kill -9 is removed by hand until doctor exists; owner, 2026-10-26)."""
        import os

        try:
            with self.conn:
                self.conn.execute(
                    "INSERT INTO run_lock (singleton, plan_id, acquired_at, pid) VALUES (1, ?, ?, ?)",
                    (plan_id, _now(), os.getpid()),
                )
            return True
        except sqlite3.IntegrityError:
            info = self.run_lock_info()
            return info is not None and info["plan_id"] == plan_id and info["pid"] == os.getpid()

    def run_lock_info(self) -> dict[str, Any] | None:
        row = self.conn.execute("SELECT plan_id, pid, acquired_at FROM run_lock LIMIT 1").fetchone()
        return dict(row) if row else None

    def release_run_lock(self, plan_id: str) -> None:
        with self.conn:
            self.conn.execute("DELETE FROM run_lock WHERE plan_id=?", (plan_id,))

    def run_lock_holder(self) -> str | None:
        row = self.conn.execute("SELECT plan_id FROM run_lock LIMIT 1").fetchone()
        return row["plan_id"] if row else None

    # -- pauses, attempt windows, stop requests -----------------------------

    def record_pause(self, plan_id: str, reason: str, detail: str, contracts: list[dict[str, Any]],
                     conflict: bool = False) -> int:
        with self.conn:
            cur = self.conn.execute(
                "INSERT INTO pauses (plan_id, reason, detail, conflict, at) VALUES (?, ?, ?, ?, ?)",
                (plan_id, reason, detail, 1 if conflict else 0, _now()),
            )
            pause_id = int(cur.lastrowid)
            for c in contracts:
                ev = c.get("evidence")
                self.conn.execute(
                    "INSERT INTO pause_contracts (pause_id, contract_id, reason, detail, contract_hash, evidence) "
                    "VALUES (?, ?, ?, ?, ?, ?)",
                    (pause_id, c["contract_id"], c["reason"], c["detail"], c["contract_hash"],
                     None if ev is None else json.dumps(ev, default=str)),
                )
            return pause_id

    def open_pause(self, plan_id: str) -> dict[str, Any] | None:
        row = self.conn.execute(
            "SELECT * FROM pauses WHERE plan_id=? AND resumed_at IS NULL ORDER BY pause_id DESC LIMIT 1", (plan_id,),
        ).fetchone()
        if row is None:
            return None
        out = {k: row[k] for k in ("pause_id", "plan_id", "reason", "detail", "at")}
        out["conflict"] = bool(row["conflict"])
        contracts = []
        for r in self.conn.execute(
            "SELECT * FROM pause_contracts WHERE pause_id=? ORDER BY contract_id", (row["pause_id"],),
        ):
            contracts.append({
                "contract_id": r["contract_id"], "reason": r["reason"], "detail": r["detail"],
                "contract_hash": r["contract_hash"],
                "evidence": None if r["evidence"] is None else json.loads(r["evidence"]),
                "attempt_id": r["attempt_id"],
                "findings": None if r["findings"] is None else json.loads(r["findings"]),
                "note": r["note"],
            })
        out["contracts"] = contracts
        return out

    def set_pause_findings(self, pause_id: int, contract_id: str, attempt_id: int | None,
                           findings: dict[str, Any] | None, note: str | None) -> None:
        with self.conn:
            self.conn.execute(
                "UPDATE pause_contracts SET attempt_id=?, findings=?, note=? WHERE pause_id=? AND contract_id=?",
                (attempt_id, None if findings is None else json.dumps(findings, default=str), note,
                 pause_id, contract_id),
            )

    def resume_pause(self, pause_id: int | None, contract_ids: list[str]) -> None:
        with self.conn:
            newest = self.conn.execute("SELECT COALESCE(MAX(attempt_id), 0) FROM attempts").fetchone()[0]
            now = _now()
            for cid in contract_ids:
                self.conn.execute(
                    "INSERT INTO attempt_windows (contract_id, after_attempt_id, pause_id, at) VALUES (?, ?, ?, ?)",
                    (cid, newest, pause_id, now),
                )
            if pause_id is not None:
                self.conn.execute("UPDATE pauses SET resumed_at=? WHERE pause_id=?", (now, pause_id))

    def window_start(self, contract_id: str) -> int:
        row = self.conn.execute(
            "SELECT after_attempt_id FROM attempt_windows WHERE contract_id=? ORDER BY window_id DESC LIMIT 1",
            (contract_id,),
        ).fetchone()
        return int(row["after_attempt_id"]) if row else 0

    def request_stop(self, plan_id: str) -> None:
        self.set_meta(f"stop-request:{plan_id}", _now())

    def stop_requested(self, plan_id: str) -> bool:
        return self.get_meta(f"stop-request:{plan_id}") is not None

    def clear_stop(self, plan_id: str) -> None:
        self.delete_meta(f"stop-request:{plan_id}")
