"""Headless runner — executes a ScheduleConfig with no human in the loop."""
from __future__ import annotations
import hashlib
import os
import time
import traceback
from pathlib import Path

from agent_kit.adapters.clawpilot import load_persona
from agent_kit.envelope import Envelope, EnvelopeRunner
from agent_kit.fury import Fury
from agent_kit.history import History
from agent_kit.schedule import ScheduleConfig

LOCK_DIR = Path("~/.agent-kit/locks").expanduser()


def _acquire_lock(name: str) -> Path | None:
    """Return the lock path on success, None if another run holds it.

    Stale-lock detection: if the PID in the lock file isn't alive, we steal it.
    """
    LOCK_DIR.mkdir(parents=True, exist_ok=True)
    safe = name.replace("/", "_").replace(" ", "_")
    lock_path = LOCK_DIR / f"{safe}.lock"
    if lock_path.exists():
        try:
            existing_pid = int(lock_path.read_text().strip())
            # Signal 0 = "is this PID alive?" — no actual signal sent.
            try:
                os.kill(existing_pid, 0)
                return None  # alive, lock held
            except ProcessLookupError:
                pass  # stale, fall through to steal
            except PermissionError:
                # Process exists but we can't signal it — assume alive
                return None
        except (ValueError, OSError):
            pass  # corrupt lock, steal
    lock_path.write_text(str(os.getpid()))
    return lock_path


def _release_lock(lock_path: Path | None) -> None:
    if lock_path and lock_path.exists():
        try:
            lock_path.unlink()
        except OSError:
            pass


def _last_question_from_transcript(transcript_path: Path) -> tuple[str, list]:
    import json
    if not transcript_path or not Path(transcript_path).exists():
        return "(no transcript)", []
    last_q = ""
    last_opts: list = []
    with open(transcript_path) as f:
        for line in f:
            try:
                rec = json.loads(line)
            except Exception:
                continue
            if rec.get("type") == "assistant":
                for tc in rec.get("tool_calls") or []:
                    if tc.get("name") == "ask_human":
                        args = tc.get("arguments") or {}
                        last_q = args.get("question", "") or ""
                        last_opts = args.get("options", []) or []
    return last_q, last_opts


def run_headless(config: ScheduleConfig, *, db_path: str | Path | None = None,
                 verbose: bool = False) -> dict:
    # Run-lock: prevent same schedule from double-firing
    lock_path = _acquire_lock(config.name)
    if lock_path is None:
        return {
            "run_id": None, "review_id": None, "stop_reason": "skipped_locked",
            "fury_verdict": None, "transcript": None, "writes": 0,
            "tokens_in": 0, "tokens_out": 0, "dollars": 0.0, "wall_seconds": 0.0,
            "written_files": [], "error": f"another run of '{config.name}' is in progress",
        }

    started_at = time.time()
    history = History(db_path) if db_path else History()
    transcript_path: str | None = None
    written_files: list = []
    fury_verdict: str | None = None
    fury_summary: dict | None = None
    stop_reason = "error"
    iterations = 0
    writes_executed = 0
    input_tokens = output_tokens = cached_input_tokens = 0
    estimated_dollars = 0.0
    wall_seconds = 0.0
    error_text: str | None = None
    review_id: int | None = None

    try:
        workspace = Path(config.workspace).expanduser()
        squad_dir = workspace / ".squad" / "agents"
        charter = squad_dir / config.persona / "charter.md"
        if not charter.exists():
            raise FileNotFoundError(f"persona charter not found: {charter}")

        # Charter hash — bucket transcripts by version when comparing later
        charter_bytes = charter.read_bytes()
        charter_sha = hashlib.sha256(charter_bytes).hexdigest()[:12]

        rendered_paths = config.rendered_writable_paths()
        rendered_task = config.rendered_task()

        extra_system = None
        stop_on_ambiguity = True
        if config.on_ambiguity == "best_effort":
            extra_system = (
                "## HEADLESS MODE — no human available\n\n"
                "If you hit ambiguity, do NOT call ask_human. Make the most "
                "reasonable interpretation, label your assumption [HYPOTHESIS], "
                "and proceed. Explain the interpretation in your final message."
            )
            stop_on_ambiguity = False

        agent = load_persona(
            charter=charter,
            workspace=workspace,
            client=config.client,
            model=config.model,
            enable_clawpilot=True,
            max_iters=config.max_iters,
            extra_system=extra_system,
        )
        # Stamp charter hash into the transcript header for post-hoc grouping
        if agent.transcript:
            agent.transcript.log("charter_version",
                schedule_name=config.name, persona=config.persona,
                charter_path=str(charter), charter_sha=charter_sha,
                charter_bytes=len(charter_bytes),
            )

        env = Envelope(
            writable_paths=rendered_paths,
            max_writes=config.max_writes,
            min_writes=config.min_writes,
            max_input_tokens=config.max_input_tokens,
            max_output_tokens=config.max_output_tokens,
            max_dollars=config.max_dollars,
            max_wall_seconds=config.max_wall_seconds,
            stop_on_ambiguity=stop_on_ambiguity,
        )
        runner = EnvelopeRunner(agent, env)
        result = runner.run(rendered_task, verbose=verbose)

        stop_reason = result.loop_result.stop_reason
        iterations = result.loop_result.iterations
        writes_executed = result.writes_executed
        input_tokens = result.input_tokens
        output_tokens = result.output_tokens
        cached_input_tokens = result.cached_input_tokens
        estimated_dollars = result.estimated_dollars
        wall_seconds = result.wall_seconds
        transcript_path = str(agent.transcript.path) if agent.transcript else None

        for rp in rendered_paths:
            full = workspace / rp
            if full.exists():
                written_files.append(str(full))

        if transcript_path:
            try:
                report = Fury.grade(transcript_path)
                fury_verdict = report.verdict
                fury_summary = report.summary
            except Exception as e:
                fury_verdict = "ERROR"
                fury_summary = {"error": str(e)}

        agent.close()
    except Exception as e:
        error_text = f"{type(e).__name__}: {e}\n{traceback.format_exc()}"

    ended_at = time.time()
    run_id = history.record_run(
        schedule_name=config.name, persona=config.persona,
        workspace=str(config.workspace), started_at=started_at, ended_at=ended_at,
        stop_reason=stop_reason, iterations=iterations,
        writes_executed=writes_executed,
        input_tokens=input_tokens, output_tokens=output_tokens,
        cached_input_tokens=cached_input_tokens,
        estimated_dollars=estimated_dollars, wall_seconds=wall_seconds,
        fury_verdict=fury_verdict, fury_summary=fury_summary,
        transcript_path=transcript_path, written_files=written_files,
        error=error_text,
    )

    if stop_reason == "ambiguity_halt" and config.on_ambiguity == "queue" and transcript_path:
        q, opts = _last_question_from_transcript(Path(transcript_path))
        review_id = history.queue_review(
            schedule_name=config.name, persona=config.persona,
            question=q, options=opts, transcript_path=transcript_path, run_id=run_id,
        )

    history.close()
    _release_lock(lock_path)
    return {
        "run_id": run_id, "review_id": review_id, "stop_reason": stop_reason,
        "fury_verdict": fury_verdict, "transcript": transcript_path,
        "writes": writes_executed, "tokens_in": input_tokens, "tokens_out": output_tokens,
        "dollars": estimated_dollars, "wall_seconds": wall_seconds,
        "written_files": written_files, "error": error_text,
    }
