"""Pipeline config and runner for multi-persona Boundary jobs."""
from __future__ import annotations

import hashlib
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal

import yaml

from boundary.agent import Agent
from boundary.envelope import Envelope, EnvelopeRunner
from boundary.headless import run_headless
from boundary.history import History
from boundary.schedule import ScheduleConfig
from boundary.third_umpire import ThirdUmpire


PipelineStopPolicy = Literal["fail", "warn_fail", "never"]

SQUAD_PLANNER_SYSTEM = """You are Boundary's Squad Planner.

You do not execute the delivery work. You create the shared squad plan that the
persona steps must execute inside their own Boundary envelopes.

You are given the selected personas' charters, the pipeline steps, and the
workspace. Produce one written plan that:
- states the shared objective and non-goals
- assigns each persona an explicit lane
- defines the evidence each persona should gather
- defines handoffs between personas
- lists kill criteria that should stop or change the pipeline
- names the allowed output path for each step

Use the enforced `stage_proposal` pivot before deep reads or writing. The plan is
the squad-level staging gate; downstream personas will still perform their own
per-step Boundary staging, but they must follow or explicitly invalidate this
plan."""


@dataclass
class SquadPlanningConfig:
    enabled: bool = False
    output_path: str = "scratch/{name}-squad-plan-{date}.md"
    max_writes: int = 1
    min_writes: int = 1
    require_staging: bool = True
    max_unstaged_reads: int = 3
    max_iters: int = 20
    max_input_tokens: int = 500_000
    max_output_tokens: int = 50_000
    max_dollars: float | None = 0.75
    max_wall_seconds: float = 900.0
    on_ambiguity: str = "queue"
    on_commit: str = "refuse"
    on_taint: str = "warn"
    commit_allowlist: list[str] = field(default_factory=list)
    model: str | None = None
    required: bool = True


@dataclass
class PipelineStep:
    name: str
    persona: str
    task: str
    writable_paths: list[str] = field(default_factory=list)
    max_writes: int | None = None
    min_writes: int | None = None
    require_staging: bool | None = None
    max_unstaged_reads: int | None = None
    max_iters: int | None = None
    max_input_tokens: int | None = None
    max_output_tokens: int | None = None
    max_dollars: float | None = None
    max_wall_seconds: float | None = None
    on_ambiguity: str | None = None
    on_commit: str | None = None
    on_taint: str | None = None
    commit_allowlist: list[str] = field(default_factory=list)
    model: str | None = None
    notify: Any = None


@dataclass
class PipelineConfig:
    name: str
    workspace: str
    steps: list[PipelineStep]
    schedule: str | None = None
    stop_on: PipelineStopPolicy = "fail"
    client: str = "copilot"
    model: str | None = None
    notify: Any = "digest_daily"
    enabled: bool = True
    defaults: dict[str, Any] = field(default_factory=dict)
    planning: SquadPlanningConfig = field(default_factory=SquadPlanningConfig)

    @classmethod
    def load(cls, path: str | Path) -> "PipelineConfig":
        data = yaml.safe_load(Path(path).expanduser().read_text(encoding="utf-8"))
        defaults = dict(data.get("defaults", {}) or {})
        steps = [_load_step(raw) for raw in data.get("steps", [])]
        if not steps:
            raise ValueError("pipeline must define at least one step")
        return cls(
            name=data["name"],
            schedule=data.get("schedule"),
            workspace=data["workspace"],
            steps=steps,
            stop_on=data.get("stop_on", "fail"),
            client=data.get("client", "copilot"),
            model=data.get("model"),
            notify=data.get("notify", "digest_daily"),
            enabled=bool(data.get("enabled", True)),
            defaults=defaults,
            planning=_load_planning(data.get("planning", {})),
        )

    def to_schedule_config(self, step: PipelineStep, *, squad_plan: str | None = None,
                           squad_plan_path: str | None = None) -> ScheduleConfig:
        env_defaults = dict(self.defaults.get("envelope", {}) or {})
        task = step.task
        if squad_plan:
            task = _task_with_squad_plan(task, squad_plan=squad_plan, squad_plan_path=squad_plan_path)
        return ScheduleConfig(
            name=f"{self.name}/{step.name}",
            schedule=self.schedule or "manual",
            persona=step.persona,
            workspace=self.workspace,
            task=task,
            writable_paths=step.writable_paths or list(env_defaults.get("writable_paths", []) or []),
            max_writes=_coalesce(step.max_writes, env_defaults.get("max_writes"), 3),
            min_writes=_coalesce(step.min_writes, env_defaults.get("min_writes"), 1),
            require_staging=_coalesce(step.require_staging, env_defaults.get("require_staging"), True),
            max_unstaged_reads=_coalesce(step.max_unstaged_reads, env_defaults.get("max_unstaged_reads"), 3),
            max_iters=_coalesce(step.max_iters, env_defaults.get("max_iters"), 25),
            max_input_tokens=_coalesce(step.max_input_tokens, env_defaults.get("max_input_tokens"), 500_000),
            max_output_tokens=_coalesce(step.max_output_tokens, env_defaults.get("max_output_tokens"), 50_000),
            max_dollars=_coalesce_nullable(step.max_dollars, env_defaults.get("max_dollars"), 1.00),
            max_wall_seconds=_coalesce(step.max_wall_seconds, env_defaults.get("max_wall_seconds"), 900.0),
            on_ambiguity=step.on_ambiguity or self.defaults.get("on_ambiguity", "queue"),
            on_commit=step.on_commit or self.defaults.get("on_commit", "refuse"),
            on_taint=step.on_taint or self.defaults.get("on_taint", "warn"),
            commit_allowlist=step.commit_allowlist or list(self.defaults.get("commit_allowlist", []) or []),
            client=self.client,
            model=step.model or self.model,
            notify=step.notify if step.notify is not None else self.notify,
            enabled=self.enabled,
        )

    def validate(self) -> list[str]:
        errors: list[str] = []
        if self.stop_on not in ("fail", "warn_fail", "never"):
            errors.append(f"stop_on must be one of fail|warn_fail|never, got {self.stop_on!r}")
        if self.planning.enabled:
            try:
                _workspace_path(
                    Path(self.workspace).expanduser(),
                    _render_pipeline_template(self, self.planning.output_path),
                )
            except ValueError as e:
                errors.append(f"planning.output_path: {e}")
            if self.planning.on_commit == "allow" and not self.planning.commit_allowlist:
                errors.append(
                    "planning.on_commit: allow with empty commit_allowlist allows ALL commit tools"
                )
            if self.planning.commit_allowlist and self.planning.on_commit != "allow":
                errors.append(
                    "planning.commit_allowlist is set but planning.on_commit is not allow"
                )
        for step in self.steps:
            cfg = self.to_schedule_config(step)
            for err in cfg.validate_commit_policy():
                errors.append(f"{step.name}: {err}")
        return errors


def run_pipeline(config: PipelineConfig, *, db_path: str | Path | None = None,
                 verbose: bool = False) -> dict:
    started_at = time.time()
    step_results: list[dict[str, Any]] = []
    plan_result: dict[str, Any] | None = None
    squad_plan: str | None = None
    squad_plan_path: str | None = None
    if config.planning.enabled:
        plan_result = run_squad_planning(config, db_path=db_path, verbose=verbose)
        squad_plan_path = plan_result.get("plan_path")
        if squad_plan_path and Path(squad_plan_path).exists():
            squad_plan = Path(squad_plan_path).read_text(encoding="utf-8")
        if _planning_failed(config.planning, plan_result):
            return {
                "pipeline": config.name,
                "planning": plan_result,
                "steps": step_results,
                "stop_reason": "planning_failed",
                "wall_seconds": time.time() - started_at,
            }
    for step in config.steps:
        step_config = config.to_schedule_config(
            step, squad_plan=squad_plan, squad_plan_path=squad_plan_path,
        )
        out = run_headless(step_config, db_path=db_path, verbose=verbose)
        step_results.append({"step": step.name, "persona": step.persona, **out})
        if _should_stop(config.stop_on, out):
            break
    return {
        "pipeline": config.name,
        "planning": plan_result,
        "steps": step_results,
        "stop_reason": _pipeline_stop_reason(config, step_results),
        "wall_seconds": time.time() - started_at,
    }


def run_squad_planning(config: PipelineConfig, *, db_path: str | Path | None = None,
                       verbose: bool = False) -> dict[str, Any]:
    planning = config.planning
    started_at = time.time()
    history = History(db_path) if db_path else History()
    workspace = Path(config.workspace).expanduser()
    plan_path_rel = _render_pipeline_template(config, planning.output_path)
    plan_path = _workspace_path(workspace, plan_path_rel)
    plan_path.parent.mkdir(parents=True, exist_ok=True)
    transcript_path: str | None = None
    third_umpire_verdict: str | None = None
    third_umpire_summary: dict | None = None
    stop_reason = "error"
    iterations = 0
    writes_executed = 0
    input_tokens = output_tokens = cached_input_tokens = 0
    estimated_dollars = 0.0
    wall_seconds = 0.0
    error_text: str | None = None
    written_files: list[str] = []

    agent: Agent | None = None
    try:
        agent = Agent(
            name="squad-planner",
            system_prompt=_squad_planner_system(config),
            workspace=workspace,
            client=config.client,
            client_kwargs={"model": planning.model or config.model} if (planning.model or config.model) else {},
            enable_clawpilot=True,
            max_iters=planning.max_iters,
        )
        if agent.transcript:
            agent.transcript.log(
                "squad_plan_start",
                pipeline=config.name,
                personas=[step.persona for step in config.steps],
                output_path=plan_path_rel,
            )
        env = Envelope(
            writable_paths=[plan_path_rel],
            max_writes=planning.max_writes,
            min_writes=planning.min_writes,
            require_staging=planning.require_staging,
            max_unstaged_reads=planning.max_unstaged_reads,
            max_input_tokens=planning.max_input_tokens,
            max_output_tokens=planning.max_output_tokens,
            max_dollars=planning.max_dollars,
            max_wall_seconds=planning.max_wall_seconds,
            stop_on_ambiguity=(planning.on_ambiguity != "best_effort"),
            on_commit=planning.on_commit,
            commit_allowlist=list(planning.commit_allowlist or []),
            on_taint=planning.on_taint,
        )
        result = EnvelopeRunner(agent, env).run(
            _squad_planner_task(config, output_path=plan_path_rel),
            verbose=verbose,
        )
        stop_reason = result.loop_result.stop_reason
        iterations = result.loop_result.iterations
        writes_executed = result.writes_executed
        input_tokens = result.input_tokens
        output_tokens = result.output_tokens
        cached_input_tokens = result.cached_input_tokens
        estimated_dollars = result.estimated_dollars
        wall_seconds = result.wall_seconds
        transcript_path = str(agent.transcript.path) if agent.transcript else None
        if plan_path.exists():
            written_files.append(str(plan_path))
        if transcript_path:
            report = ThirdUmpire.grade(transcript_path)
            third_umpire_verdict = report.verdict
            third_umpire_summary = report.summary
    except Exception as e:
        import traceback
        error_text = f"{type(e).__name__}: {e}\n{traceback.format_exc()}"
    finally:
        if agent is not None:
            agent.close()

    ended_at = time.time()
    run_id = history.record_run(
        schedule_name=f"{config.name}/squad-plan",
        persona="squad-planner",
        workspace=str(config.workspace),
        started_at=started_at,
        ended_at=ended_at,
        stop_reason=stop_reason,
        iterations=iterations,
        writes_executed=writes_executed,
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        cached_input_tokens=cached_input_tokens,
        estimated_dollars=estimated_dollars,
        wall_seconds=wall_seconds,
        third_umpire_verdict=third_umpire_verdict,
        third_umpire_summary=third_umpire_summary,
        transcript_path=transcript_path,
        written_files=written_files,
        error=error_text,
    )
    history.close()
    return {
        "run_id": run_id,
        "persona": "squad-planner",
        "plan_path": str(plan_path),
        "stop_reason": stop_reason,
        "third_umpire_verdict": third_umpire_verdict,
        "transcript": transcript_path,
        "writes": writes_executed,
        "tokens_in": input_tokens,
        "tokens_out": output_tokens,
        "dollars": estimated_dollars,
        "wall_seconds": wall_seconds,
        "written_files": written_files,
        "error": error_text,
    }


def _load_step(raw: dict[str, Any]) -> PipelineStep:
    env = dict(raw.get("envelope", {}) or {})
    return PipelineStep(
        name=raw["name"],
        persona=raw["persona"],
        task=raw["task"],
        writable_paths=list(env.get("writable_paths", []) or []),
        max_writes=env.get("max_writes"),
        min_writes=env.get("min_writes"),
        require_staging=env.get("require_staging"),
        max_unstaged_reads=env.get("max_unstaged_reads"),
        max_iters=env.get("max_iters"),
        max_input_tokens=env.get("max_input_tokens"),
        max_output_tokens=env.get("max_output_tokens"),
        max_dollars=env.get("max_dollars"),
        max_wall_seconds=env.get("max_wall_seconds"),
        on_ambiguity=raw.get("on_ambiguity"),
        on_commit=raw.get("on_commit"),
        on_taint=raw.get("on_taint"),
        commit_allowlist=list(raw.get("commit_allowlist", []) or []),
        model=raw.get("model"),
        notify=raw.get("notify"),
    )


def _load_planning(raw: Any) -> SquadPlanningConfig:
    if raw is True:
        return SquadPlanningConfig(enabled=True)
    if not raw:
        return SquadPlanningConfig(enabled=False)
    env = dict(raw.get("envelope", {}) or {})
    return SquadPlanningConfig(
        enabled=bool(raw.get("enabled", True)),
        output_path=raw.get("output_path", "scratch/{name}-squad-plan-{date}.md"),
        max_writes=_coalesce(raw.get("max_writes"), env.get("max_writes"), 1),
        min_writes=_coalesce(raw.get("min_writes"), env.get("min_writes"), 1),
        require_staging=_coalesce(raw.get("require_staging"), env.get("require_staging"), True),
        max_unstaged_reads=_coalesce(raw.get("max_unstaged_reads"), env.get("max_unstaged_reads"), 3),
        max_iters=_coalesce(raw.get("max_iters"), env.get("max_iters"), 20),
        max_input_tokens=_coalesce(raw.get("max_input_tokens"), env.get("max_input_tokens"), 500_000),
        max_output_tokens=_coalesce(raw.get("max_output_tokens"), env.get("max_output_tokens"), 50_000),
        max_dollars=_coalesce_nullable(raw.get("max_dollars"), env.get("max_dollars"), 0.75),
        max_wall_seconds=_coalesce(raw.get("max_wall_seconds"), env.get("max_wall_seconds"), 900.0),
        on_ambiguity=raw.get("on_ambiguity", "queue"),
        on_commit=raw.get("on_commit", "refuse"),
        on_taint=raw.get("on_taint", "warn"),
        commit_allowlist=list(raw.get("commit_allowlist", []) or []),
        model=raw.get("model"),
        required=bool(raw.get("required", True)),
    )


def _coalesce(value: Any, default_value: Any, fallback: Any) -> Any:
    if value is not None:
        return value
    if default_value is not None:
        return default_value
    return fallback


def _coalesce_nullable(value: Any, default_value: Any, fallback: Any) -> Any:
    if value is not None:
        return float(value)
    if default_value is not None:
        return None if default_value is None else float(default_value)
    return fallback


def _should_stop(stop_on: PipelineStopPolicy, out: dict[str, Any]) -> bool:
    if stop_on == "never":
        return False
    verdict = out.get("third_umpire_verdict")
    failed = bool(out.get("error")) or verdict in {"FAIL", "ERROR"}
    if stop_on == "fail":
        return failed
    return failed or verdict == "WARN"


def _pipeline_stop_reason(config: PipelineConfig, results: list[dict[str, Any]]) -> str:
    if len(results) == len(config.steps):
        return "completed"
    if not results:
        return "not_started"
    return f"stopped_after_{results[-1]['step']}"


def _planning_failed(planning: SquadPlanningConfig, result: dict[str, Any]) -> bool:
    if not planning.required:
        return False
    verdict = result.get("third_umpire_verdict")
    return bool(result.get("error")) or verdict in {"FAIL", "ERROR"} or not result.get("written_files")


def _render_pipeline_template(config: PipelineConfig, value: str) -> str:
    return ScheduleConfig(
        name=config.name,
        schedule=config.schedule or "manual",
        persona="squad-planner",
        workspace=config.workspace,
        task="",
    ).render_template(value)


def _workspace_path(workspace: Path, rel_path: str) -> Path:
    root = workspace.expanduser().resolve()
    full = (root / rel_path).resolve()
    if full != root and root not in full.parents:
        raise ValueError(f"path escapes workspace: {rel_path}")
    return full


def _task_with_squad_plan(task: str, *, squad_plan: str, squad_plan_path: str | None) -> str:
    plan_ref = f" at `{squad_plan_path}`" if squad_plan_path else ""
    return f"""## Squad plan gate

A squad-level Boundary plan was produced before this step{plan_ref}. Treat it as
the governing plan for this persona run.

Your own per-step Boundary staging gate is still enforced. In your
`stage_proposal`, explicitly state which part of the squad plan you are executing
or what new evidence would invalidate that part of the plan. Do not work outside
your assigned lane unless the evidence requires escalation.

### Squad plan

{squad_plan[:24000]}

---

## Persona task

{task}
"""


def _squad_planner_system(config: PipelineConfig) -> str:
    workspace = Path(config.workspace).expanduser()
    personas = []
    seen: set[str] = set()
    for step in config.steps:
        if step.persona in seen:
            continue
        seen.add(step.persona)
        charter = workspace / ".squad" / "agents" / step.persona / "charter.md"
        if charter.exists():
            text = charter.read_text(encoding="utf-8")
            digest = hashlib.sha256(text.encode("utf-8")).hexdigest()[:12]
            personas.append(
                f"## {step.persona}\n"
                f"_charter: {charter} sha={digest}_\n\n"
                f"{text[:8000]}"
            )
        else:
            personas.append(f"## {step.persona}\n_charter missing: {charter}_")
    persona_block = "\n\n---\n\n".join(personas)
    return f"""{SQUAD_PLANNER_SYSTEM}

## Workspace

{workspace}

## Selected persona charters

{persona_block}
"""


def _squad_planner_task(config: PipelineConfig, *, output_path: str) -> str:
    step_lines = []
    for step in config.steps:
        step_cfg = config.to_schedule_config(step)
        step_lines.append(
            f"## Step: {step.name}\n"
            f"- Persona: {step.persona}\n"
            f"- Writable paths: {step_cfg.writable_paths}\n"
            f"- Max writes: {step_cfg.max_writes}; max iters: {step_cfg.max_iters}\n"
            f"- Task:\n{step.task}"
        )
    return f"""Create the squad-level plan for pipeline `{config.name}`.

Write the plan to `{output_path}`. Include these sections exactly:

# Squad Plan — {config.name}

## Objective
## Persona Lanes
## Evidence Plan
## Handoffs
## Kill Criteria
## Step Instructions

Pipeline steps:

{chr(10).join(step_lines)}
"""
