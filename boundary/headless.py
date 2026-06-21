"""Headless runner — executes a ScheduleConfig with no human in the loop."""
from __future__ import annotations
import hashlib
import json
import os
import time
import traceback
from pathlib import Path

from boundary.adapters.clawpilot import load_persona
from boundary.envelope import Envelope, EnvelopeRunner
from boundary.third_umpire import ThirdUmpire
from boundary.history import History
from boundary.schedule import ScheduleConfig

LOCK_DIR = Path("~/.boundary/locks").expanduser()
EVENT_PENDING_DIR = Path("~/.boundary/events/pending").expanduser()


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


def _last_commit_attempt_from_transcript(transcript_path: Path) -> tuple[str, list]:
    """Find the most recent commit-tool call attempted in the transcript.
    Returns (question, options) shaped like _last_question_from_transcript so
    the same review_queue API works.
    """
    import json
    if not transcript_path or not Path(transcript_path).exists():
        return "(no transcript)", []
    last_tool = ""
    last_args: dict = {}
    last_reason = ""
    with open(transcript_path) as f:
        for line in f:
            try:
                rec = json.loads(line)
            except Exception:
                continue
            if rec.get("type") == "envelope_end":
                for ev in rec.get("events") or []:
                    if ev.get("kind") == "commit_halt":
                        last_tool = ev.get("tool", "")
            if rec.get("type") == "assistant":
                for tc in rec.get("tool_calls") or []:
                    if tc.get("name") == last_tool:
                        last_args = tc.get("arguments") or {}
                        last_reason = last_args.get("reason", "") or ""
    if not last_tool:
        return "(no commit attempt found in transcript)", []
    arg_preview = json.dumps({k: v for k, v in last_args.items() if k != "reason"})[:400]
    q = (
        f"Commit tool '{last_tool}' was attempted and halted for human approval.\n"
        f"Reason given by agent: {last_reason}\n"
        f"Args: {arg_preview}"
    )
    return q, ["approve", "deny", "rescope"]


def _scout_hook_config(config: ScheduleConfig) -> dict | None:
    notify = config.notify
    if not isinstance(notify, dict):
        return None
    hook = notify.get("scout_hook")
    return hook if isinstance(hook, dict) else None


def _should_emit_scout_hook(hook: dict, *, verdict: str | None, error: str | None) -> bool:
    mode = str(hook.get("on", "warn_fail")).lower()
    if mode == "always":
        return True
    if mode == "failure":
        return bool(error) or verdict in {"FAIL", "ERROR"}
    if mode == "warn_fail":
        return bool(error) or verdict in {"WARN", "FAIL", "ERROR"}
    return False


def _emit_scout_hook_event(
    config: ScheduleConfig,
    *,
    run_id: int,
    review_id: int | None,
    stop_reason: str,
    third_umpire_verdict: str | None,
    transcript_path: str | None,
    written_files: list,
    error_text: str | None,
    rendered_paths: list[str],
    wall_seconds: float,
    estimated_dollars: float,
) -> str | None:
    hook = _scout_hook_config(config)
    if not hook or not _should_emit_scout_hook(
        hook, verdict=third_umpire_verdict, error=error_text,
    ):
        return None

    workspace = Path(config.workspace).expanduser()
    summary_template = hook.get("summary_file")
    summary_file = None
    if isinstance(summary_template, str) and summary_template:
        summary_file = str(workspace / config.render_template(summary_template))
    elif rendered_paths:
        summary_file = str(workspace / rendered_paths[0])

    event = {
        "type": "boundary.schedule.completed",
        "version": 1,
        "schedule": config.name,
        "persona": config.persona,
        "workspace": str(workspace),
        "run_id": run_id,
        "review_id": review_id,
        "stop_reason": stop_reason,
        "third_umpire_verdict": third_umpire_verdict,
        "transcript": transcript_path,
        "summary_file": summary_file,
        "written_files": written_files,
        "error": error_text,
        "wall_seconds": wall_seconds,
        "estimated_dollars": estimated_dollars,
        "channel": hook.get("channel", "teams_dm"),
        "created_at": int(time.time()),
    }

    safe = config.name.replace("/", "_").replace(" ", "_")
    EVENT_PENDING_DIR.mkdir(parents=True, exist_ok=True)
    path = EVENT_PENDING_DIR / f"{safe}-{run_id}.json"
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(event, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    tmp.replace(path)
    return str(path)


def run_headless(config: ScheduleConfig, *, db_path: str | Path | None = None,
                 verbose: bool = False) -> dict:
    # Run-lock: prevent same schedule from double-firing
    lock_path = _acquire_lock(config.name)
    if lock_path is None:
        return {
            "run_id": None, "review_id": None, "stop_reason": "skipped_locked",
            "third_umpire_verdict": None, "transcript": None, "writes": 0,
            "tokens_in": 0, "tokens_out": 0, "dollars": 0.0, "wall_seconds": 0.0,
            "written_files": [], "error": f"another run of '{config.name}' is in progress",
        }

    started_at = time.time()
    history = History(db_path) if db_path else History()
    transcript_path: str | None = None
    written_files: list = []
    third_umpire_verdict: str | None = None
    third_umpire_summary: dict | None = None
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
            sandbox_driver=config.sandbox_driver,
            egress_allowlist=config.egress_allowlist,
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
            require_staging=config.require_staging,
            max_unstaged_reads=config.max_unstaged_reads,
            max_input_tokens=config.max_input_tokens,
            max_output_tokens=config.max_output_tokens,
            max_dollars=config.max_dollars,
            max_wall_seconds=config.max_wall_seconds,
            stop_on_ambiguity=stop_on_ambiguity,
            on_commit=config.on_commit,
            commit_allowlist=list(config.commit_allowlist or []),
            on_taint=config.on_taint,
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
                report = ThirdUmpire.grade(transcript_path)
                third_umpire_verdict = report.verdict
                third_umpire_summary = report.summary
            except Exception as e:
                third_umpire_verdict = "ERROR"
                third_umpire_summary = {"error": str(e)}

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
        third_umpire_verdict=third_umpire_verdict, third_umpire_summary=third_umpire_summary,
        transcript_path=transcript_path, written_files=written_files,
        error=error_text,
    )

    if stop_reason == "ambiguity_halt" and config.on_ambiguity == "queue" and transcript_path:
        q, opts = _last_question_from_transcript(Path(transcript_path))
        review_id = history.queue_review(
            schedule_name=config.name, persona=config.persona,
            question=q, options=opts, transcript_path=transcript_path, run_id=run_id,
        )
    elif stop_reason == "commit_halt" and config.on_commit == "queue" and transcript_path:
        q, opts = _last_commit_attempt_from_transcript(Path(transcript_path))
        review_id = history.queue_review(
            schedule_name=config.name, persona=config.persona,
            question=q, options=opts, transcript_path=transcript_path, run_id=run_id,
        )

    history.close()
    event_path = _emit_scout_hook_event(
        config,
        run_id=run_id,
        review_id=review_id,
        stop_reason=stop_reason,
        third_umpire_verdict=third_umpire_verdict,
        transcript_path=transcript_path,
        written_files=written_files,
        error_text=error_text,
        rendered_paths=config.rendered_writable_paths(),
        wall_seconds=wall_seconds,
        estimated_dollars=estimated_dollars,
    )
    _release_lock(lock_path)
    return {
        "run_id": run_id, "review_id": review_id, "stop_reason": stop_reason,
        "third_umpire_verdict": third_umpire_verdict, "transcript": transcript_path,
        "writes": writes_executed, "tokens_in": input_tokens, "tokens_out": output_tokens,
        "dollars": estimated_dollars, "wall_seconds": wall_seconds,
        "event_path": event_path,
        "written_files": written_files, "error": error_text,
    }
