"""Run history — SQLite ledger of every headless run + Third Umpire verdict."""
from __future__ import annotations
import json
import sqlite3
import time
from pathlib import Path
from typing import Any

DEFAULT_DB = Path.home() / ".boundary" / "history.db"

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
    tainted INTEGER DEFAULT 0,
    taint_sources_json TEXT,
    taint_cleared INTEGER DEFAULT 0
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
"""


class History:
    def __init__(self, db_path: str | Path = DEFAULT_DB):
        self.db_path = Path(db_path).expanduser()
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(str(self.db_path))
        self._migrate_legacy_columns()
        self._conn.executescript(SCHEMA)
        self._migrate_taint_columns()
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
        self._conn.commit()

    def _migrate_taint_columns(self) -> None:
        """Add taint-lineage columns (v2 Item 1) to pre-existing DBs. Idempotent."""
        existing = {
            row[1]
            for row in self._conn.execute("PRAGMA table_info(runs)").fetchall()
        }
        additions = {
            "tainted": "INTEGER DEFAULT 0",
            "taint_sources_json": "TEXT",
            "taint_cleared": "INTEGER DEFAULT 0",
        }
        for col, decl in additions.items():
            if col not in existing:
                self._conn.execute(f"ALTER TABLE runs ADD COLUMN {col} {decl}")
        self._conn.commit()

    def record_run(self, *, schedule_name: str | None, persona: str | None,
                   workspace: str | None, started_at: float, ended_at: float,
                   stop_reason: str, iterations: int, writes_executed: int,
                   input_tokens: int, output_tokens: int, cached_input_tokens: int,
                   estimated_dollars: float, wall_seconds: float,
                   third_umpire_verdict: str | None, third_umpire_summary: dict | None,
                   transcript_path: str | None, written_files: list[str],
                   error: str | None = None,
                   tainted: bool = False, taint_sources: list | None = None) -> int:
        cur = self._conn.execute(
            """INSERT INTO runs(
                started_at, ended_at, schedule_name, persona, workspace,
                stop_reason, iterations, writes_executed,
                input_tokens, output_tokens, cached_input_tokens,
                estimated_dollars, wall_seconds,
                third_umpire_verdict, third_umpire_summary_json, transcript_path,
                written_files_json, error,
                tainted, taint_sources_json
            ) VALUES (?,?,?,?,?, ?,?,?, ?,?,?, ?,?, ?,?,?, ?,?, ?,?)""",
            (started_at, ended_at, schedule_name, persona, workspace,
             stop_reason, iterations, writes_executed,
             input_tokens, output_tokens, cached_input_tokens,
             estimated_dollars, wall_seconds,
             third_umpire_verdict, json.dumps(third_umpire_summary or {}), transcript_path,
             json.dumps(written_files), error,
             1 if tainted else 0, json.dumps(taint_sources or [])),
        )
        self._conn.commit()
        return cur.lastrowid

    # -- cross-run taint lineage (v2 Item 1) ---------------------------------

    def taint_provenance(self, path: str) -> dict | None:
        """Lineage of `path`'s most recent recorded writer run.

        Returns {run_id, schedule_name, sources} if that run was tainted and
        has not been human-cleared; None otherwise. The most recent writer
        governs: a later clean run rewriting the file declassifies it.
        """
        rows = self._conn.execute(
            "SELECT id, schedule_name, tainted, taint_cleared, "
            "taint_sources_json, written_files_json FROM runs "
            "WHERE written_files_json IS NOT NULL "
            "ORDER BY started_at DESC, id DESC LIMIT 500",
        ).fetchall()
        for run_id, schedule_name, tainted, cleared, sources_json, files_json in rows:
            try:
                files = json.loads(files_json or "[]")
            except json.JSONDecodeError:
                continue
            if path in files:
                if tainted and not cleared:
                    return {
                        "run_id": run_id,
                        "schedule_name": schedule_name,
                        "sources": json.loads(sources_json or "[]"),
                    }
                return None
        return None

    def make_provenance(self, workspace: str | Path):
        """A provenance oracle for the kernel: maps a read path (as the tool
        receives it, usually workspace-relative) to taint lineage or None."""
        ws = Path(workspace).expanduser()

        def resolve(path: str) -> dict | None:
            candidates = [str(path)]
            p = Path(path)
            if not p.is_absolute():
                candidates.append(str(ws / p))
            for candidate in candidates:
                prov = self.taint_provenance(candidate)
                if prov:
                    return prov
            return None

        return resolve

    def clear_taint(self, run_id: int) -> None:
        """Human declassifier: mark a run's outputs reviewed-and-trusted so
        future reads of its files no longer inherit taint."""
        self._conn.execute(
            "UPDATE runs SET taint_cleared=1 WHERE id=?", (run_id,))
        self._conn.commit()

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
        return [dict(zip(cols, r)) for r in rows]

    def list_open_reviews(self, limit: int = 50) -> list[dict]:
        rows = self._conn.execute(
            "SELECT * FROM review_queue WHERE resolved=0 ORDER BY queued_at DESC LIMIT ?",
            (limit,)).fetchall()
        cols = [c[0] for c in self._conn.execute("SELECT * FROM review_queue LIMIT 0").description]
        return [dict(zip(cols, r)) for r in rows]

    def resolve_review(self, review_id: int, resolution: str) -> None:
        self._conn.execute(
            "UPDATE review_queue SET resolved=1, resolved_at=?, resolution=? WHERE id=?",
            (time.time(), resolution, review_id))
        self._conn.commit()

    def close(self):
        try:
            self._conn.close()
        except Exception:
            pass
