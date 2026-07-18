"""Run history — SQLite ledger of every headless run + Third Umpire verdict."""
from __future__ import annotations

import json
import re
import sqlite3
import time
from pathlib import Path

DEFAULT_DB = Path.home() / ".boundary" / "history.db"
_SAFE_TAG_KEY = re.compile(r"^[A-Za-z0-9_]+$")

SCHEMA = """
CREATE TABLE IF NOT EXISTS runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    started_at REAL NOT NULL,
    ended_at REAL,
    schedule_name TEXT,
    persona TEXT,
    workspace TEXT,
    stop_reason TEXT,
    iterations INTEGER,
    writes_executed INTEGER,
    input_tokens INTEGER,
    output_tokens INTEGER,
    cached_input_tokens INTEGER,
    estimated_dollars REAL,
    wall_seconds REAL,
    third_umpire_verdict TEXT,
    third_umpire_summary_json TEXT,
    transcript_path TEXT,
    written_files_json TEXT,
    error TEXT,
    attribution_json TEXT,
    receipt_json TEXT
);
CREATE INDEX IF NOT EXISTS runs_started_idx ON runs(started_at);
CREATE INDEX IF NOT EXISTS runs_schedule_idx ON runs(schedule_name, started_at);

CREATE TABLE IF NOT EXISTS review_queue (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    queued_at REAL NOT NULL,
    schedule_name TEXT,
    persona TEXT,
    question TEXT NOT NULL,
    options_json TEXT,
    transcript_path TEXT,
    run_id INTEGER,
    resolved INTEGER DEFAULT 0,
    resolved_at REAL,
    resolution TEXT,
    FOREIGN KEY (run_id) REFERENCES runs(id)
);
CREATE INDEX IF NOT EXISTS review_open_idx ON review_queue(resolved, queued_at);

CREATE TABLE IF NOT EXISTS tasks (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at REAL NOT NULL,
    title TEXT NOT NULL,
    detail TEXT,
    priority INTEGER DEFAULT 2,
    status TEXT DEFAULT 'pending',          -- pending | ready | done | rejected
    parent_run_id INTEGER,                  -- causal edge: which run spawned this task
    schedule_name TEXT,
    origin TEXT,                            -- source spec path / discovery origin
    trigger_rule TEXT,                      -- which trigger produced it
    FOREIGN KEY (parent_run_id) REFERENCES runs(id)
);
CREATE INDEX IF NOT EXISTS tasks_status_idx ON tasks(status, priority, created_at);
"""


class History:
    def __init__(self, db_path: str | Path = DEFAULT_DB):
        self.db_path = Path(db_path).expanduser()
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(str(self.db_path))
        self._migrate_legacy_columns()
        self._conn.executescript(SCHEMA)
        self._conn.commit()

    def _migrate_legacy_columns(self) -> None:
        """Rename pre-rename columns (fury_*) on existing DBs so old history.db
        keeps working after the Third Umpire rename. Idempotent; no-op on fresh
        DBs and on DBs already migrated."""
        existing = {
            row[1]
            for row in self._conn.execute("PRAGMA table_info(runs)").fetchall()
        }
        renames = {
            "fury_verdict": "third_umpire_verdict",
            "fury_summary_json": "third_umpire_summary_json",
        }
        for old, new in renames.items():
            if old in existing and new not in existing:
                self._conn.execute(f"ALTER TABLE runs RENAME COLUMN {old} TO {new}")
        # Additive columns on older DBs (idempotent). `existing` is empty on a
        # fresh DB where `runs` doesn't exist yet — SCHEMA creates it with the
        # column already present, so only ALTER an existing table.
        if existing and "attribution_json" not in existing:
            self._conn.execute("ALTER TABLE runs ADD COLUMN attribution_json TEXT")
        if existing and "receipt_json" not in existing:
            self._conn.execute("ALTER TABLE runs ADD COLUMN receipt_json TEXT")
        self._conn.commit()

    def record_run(self, *, schedule_name: str | None, persona: str | None,
                   workspace: str | None, started_at: float, ended_at: float,
                   stop_reason: str, iterations: int, writes_executed: int,
                   input_tokens: int, output_tokens: int, cached_input_tokens: int,
                   estimated_dollars: float, wall_seconds: float,
                   third_umpire_verdict: str | None, third_umpire_summary: dict | None,
                   transcript_path: str | None, written_files: list[str],
                   error: str | None = None, attribution: dict | None = None,
                   receipt: dict | None = None) -> int:
        cur = self._conn.execute(
            """INSERT INTO runs(
                started_at, ended_at, schedule_name, persona, workspace,
                stop_reason, iterations, writes_executed,
                input_tokens, output_tokens, cached_input_tokens,
                estimated_dollars, wall_seconds,
                third_umpire_verdict, third_umpire_summary_json, transcript_path,
                written_files_json, error, attribution_json, receipt_json
            ) VALUES (?,?,?,?,?, ?,?,?, ?,?,?, ?,?, ?,?,?, ?,?,?,?)""",
            (started_at, ended_at, schedule_name, persona, workspace,
             stop_reason, iterations, writes_executed,
             input_tokens, output_tokens, cached_input_tokens,
             estimated_dollars, wall_seconds,
             third_umpire_verdict, json.dumps(third_umpire_summary or {}), transcript_path,
             json.dumps(written_files), error, json.dumps(attribution or {}),
             json.dumps(receipt) if receipt is not None else None),
        )
        self._conn.commit()
        return cur.lastrowid

    def set_receipt(self, run_id: int, receipt: dict) -> None:
        """Attach a receipt to an already-recorded run (the receipt embeds the
        run_id, which is only known after the row is inserted)."""
        self._conn.execute(
            "UPDATE runs SET receipt_json=? WHERE id=?",
            (json.dumps(receipt), run_id))
        self._conn.commit()

    def get_receipt(self, run_id: int) -> dict | None:
        """The stored run receipt (boundary.receipt/v1) for a run, or None."""
        row = self._conn.execute(
            "SELECT receipt_json FROM runs WHERE id=?", (run_id,)).fetchone()
        if not row or not row[0]:
            return None
        try:
            return json.loads(row[0])
        except (json.JSONDecodeError, TypeError):
            return None

    def queue_review(self, *, schedule_name: str | None, persona: str | None,
                     question: str, options: list | None, transcript_path: str | None,
                     run_id: int | None) -> int:
        cur = self._conn.execute(
            """INSERT INTO review_queue(
                queued_at, schedule_name, persona, question,
                options_json, transcript_path, run_id
            ) VALUES (?,?,?,?,?,?,?)""",
            (time.time(), schedule_name, persona, question,
             json.dumps(options or []), transcript_path, run_id),
        )
        self._conn.commit()
        return cur.lastrowid

    def list_runs(self, limit: int = 20, schedule_name: str | None = None) -> list[dict]:
        if schedule_name:
            rows = self._conn.execute(
                "SELECT * FROM runs WHERE schedule_name=? ORDER BY started_at DESC LIMIT ?",
                (schedule_name, limit)).fetchall()
        else:
            rows = self._conn.execute(
                "SELECT * FROM runs ORDER BY started_at DESC LIMIT ?", (limit,)).fetchall()
        cols = [c[0] for c in self._conn.execute("SELECT * FROM runs LIMIT 0").description]
        return [dict(zip(cols, r, strict=True)) for r in rows]

    def spend_since(self, workspace: str | None, since: float,
                    tag: tuple[str, str | None] | None = None) -> float:
        """Sum estimated_dollars for runs with started_at >= since. The `runs`
        table is the spend ledger — cross-run budgets aggregate over it rather
        than a second store, so there is nothing to keep in sync. workspace=None
        sums across every workspace (a global budget). `tag` is an optional
        (key, value) attribution filter — e.g. ("tenant", "acme") sums only runs
        tagged for that tenant, so budgets can be scoped per project/tenant."""
        clauses = ["started_at >= ?"]
        params: list = [since]
        if workspace is not None:
            clauses.append("workspace = ?")
            params.append(workspace)
        if tag is not None:
            key, val = tag
            # Attribution keys come from trusted config; guard the JSON path anyway.
            if not _SAFE_TAG_KEY.match(key):
                raise ValueError(f"invalid attribution tag key: {key!r}")
            path = f"$.{key}"
            if val is None:
                clauses.append(f"json_extract(attribution_json, '{path}') IS NULL")
            else:
                clauses.append(f"json_extract(attribution_json, '{path}') = ?")
                params.append(val)
        row = self._conn.execute(
            f"SELECT COALESCE(SUM(estimated_dollars), 0) FROM runs WHERE {' AND '.join(clauses)}",
            params).fetchone()
        return float(row[0] or 0.0)

    def spend_by_tag(self, key: str, since: float = 0.0) -> dict[str | None, dict]:
        """Roll up estimated_dollars grouped by the distinct values of an
        attribution key — the chargeback read side. Returns
        {value: {"cost": float, "runs": int}}, ordered by cost descending; runs
        missing the key bucket under None. `since` windows by started_at
        (0.0 = all time). This is the counterpart to tag-scoped budgets: budgets
        bound one tenant's spend, this reports every tenant's."""
        if not _SAFE_TAG_KEY.match(key):
            raise ValueError(f"invalid attribution tag key: {key!r}")
        path = f"$.{key}"
        rows = self._conn.execute(
            f"SELECT json_extract(attribution_json, '{path}') AS v, "
            f"COALESCE(SUM(estimated_dollars), 0), COUNT(*) "
            f"FROM runs WHERE started_at >= ? GROUP BY v ORDER BY 2 DESC",
            (since,)).fetchall()
        return {r[0]: {"cost": float(r[1] or 0.0), "runs": int(r[2])} for r in rows}

    def runs_for_workspace(self, workspace: str, limit: int = 20) -> list[dict]:
        rows = self._conn.execute(
            "SELECT * FROM runs WHERE workspace=? ORDER BY started_at DESC LIMIT ?",
            (workspace, limit)).fetchall()
        cols = [c[0] for c in self._conn.execute("SELECT * FROM runs LIMIT 0").description]
        return [dict(zip(cols, r, strict=True)) for r in rows]

    def list_open_reviews(self, limit: int = 50) -> list[dict]:
        rows = self._conn.execute(
            "SELECT * FROM review_queue WHERE resolved=0 ORDER BY queued_at DESC LIMIT ?",
            (limit,)).fetchall()
        cols = [c[0] for c in self._conn.execute("SELECT * FROM review_queue LIMIT 0").description]
        return [dict(zip(cols, r, strict=True)) for r in rows]

    def resolve_review(self, review_id: int, resolution: str) -> None:
        self._conn.execute(
            "UPDATE review_queue SET resolved=1, resolved_at=?, resolution=? WHERE id=?",
            (time.time(), resolution, review_id))
        self._conn.commit()

    # --- task queue (BabyAGI-style results->tasks loop, human-gated) ----------
    def add_task(self, *, title: str, detail: str | None = None, priority: int = 2,
                 parent_run_id: int | None = None, schedule_name: str | None = None,
                 origin: str | None = None, trigger_rule: str | None = None,
                 status: str = "pending") -> int:
        cur = self._conn.execute(
            """INSERT INTO tasks(created_at, title, detail, priority, status,
                parent_run_id, schedule_name, origin, trigger_rule)
               VALUES (?,?,?,?,?,?,?,?,?)""",
            (time.time(), title, detail, priority, status,
             parent_run_id, schedule_name, origin, trigger_rule))
        self._conn.commit()
        return cur.lastrowid

    def list_tasks(self, status: str | None = None, limit: int = 50) -> list[dict]:
        if status:
            rows = self._conn.execute(
                "SELECT * FROM tasks WHERE status=? ORDER BY priority ASC, created_at ASC LIMIT ?",
                (status, limit)).fetchall()
        else:
            rows = self._conn.execute(
                "SELECT * FROM tasks ORDER BY priority ASC, created_at ASC LIMIT ?",
                (limit,)).fetchall()
        cols = [c[0] for c in self._conn.execute("SELECT * FROM tasks LIMIT 0").description]
        return [dict(zip(cols, r, strict=True)) for r in rows]

    def set_task_status(self, task_id: int, status: str) -> None:
        self._conn.execute("UPDATE tasks SET status=? WHERE id=?", (status, task_id))
        self._conn.commit()

    def close(self):
        try:
            self._conn.close()
        except Exception:
            pass
