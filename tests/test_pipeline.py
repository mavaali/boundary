from __future__ import annotations

from boundary.pipeline import PipelineConfig, PipelineStep, SquadPlanningConfig, run_pipeline


def test_pipeline_loads_squad_planning(tmp_path):
    path = tmp_path / "pipeline.yaml"
    path.write_text(
        """
name: squad-check
workspace: /tmp/workspace
planning:
  enabled: true
  output_path: scratch/squad-plan-{date}.md
  envelope:
    max_iters: 12
defaults:
  envelope:
    max_writes: 2
  on_taint: refuse
steps:
  - name: repo-review
    persona: repo-reviewer
    envelope:
      writable_paths:
        - scratch/review.md
    task: audit
""",
        encoding="utf-8",
    )

    cfg = PipelineConfig.load(path)

    assert cfg.planning.enabled is True
    assert cfg.planning.max_iters == 12
    assert cfg.planning.output_path == "scratch/squad-plan-{date}.md"
    step_cfg = cfg.to_schedule_config(cfg.steps[0])
    assert step_cfg.max_writes == 2
    assert step_cfg.on_taint == "refuse"


def test_pipeline_rejects_planning_output_path_escape(tmp_path):
    path = tmp_path / "pipeline.yaml"
    path.write_text(
        """
name: squad-check
workspace: /tmp/workspace
planning:
  enabled: true
  output_path: ../outside.md
steps:
  - name: repo-review
    persona: repo-reviewer
    task: audit
""",
        encoding="utf-8",
    )

    cfg = PipelineConfig.load(path)

    assert any("path escapes workspace" in err for err in cfg.validate())


def test_pipeline_injects_squad_plan_and_runs_steps(tmp_path, monkeypatch):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    plan_path = workspace / "scratch" / "plan.md"
    plan_path.parent.mkdir()
    config = PipelineConfig(
        name="squad-check",
        workspace=str(workspace),
        planning=SquadPlanningConfig(enabled=True),
        steps=[
            PipelineStep(
                name="repo-review",
                persona="repo-reviewer",
                task="Audit the repo.",
                writable_paths=["scratch/review.md"],
            )
        ],
    )
    seen_tasks: list[str] = []

    def fake_plan(cfg, *, db_path=None, verbose=False):
        plan_path.write_text("# Squad Plan\n\nRepo reviewer owns risk audit.\n", encoding="utf-8")
        return {
            "run_id": 1,
            "persona": "squad-planner",
            "plan_path": str(plan_path),
            "stop_reason": "stop",
            "third_umpire_verdict": "PASS",
            "writes": 1,
            "dollars": 0.0,
            "wall_seconds": 0.0,
            "written_files": [str(plan_path)],
            "error": None,
        }

    def fake_headless(step_config, *, db_path=None, verbose=False):
        seen_tasks.append(step_config.task)
        return {
            "run_id": 2,
            "review_id": None,
            "stop_reason": "stop",
            "third_umpire_verdict": "PASS",
            "transcript": None,
            "writes": 1,
            "tokens_in": 0,
            "tokens_out": 0,
            "dollars": 0.0,
            "wall_seconds": 0.0,
            "event_path": None,
            "written_files": [],
            "error": None,
        }

    monkeypatch.setattr("boundary.pipeline.run_squad_planning", fake_plan)
    monkeypatch.setattr("boundary.pipeline.run_headless", fake_headless)

    out = run_pipeline(config)

    assert out["stop_reason"] == "completed"
    assert len(out["steps"]) == 1
    assert "## Squad plan gate" in seen_tasks[0]
    assert "Repo reviewer owns risk audit" in seen_tasks[0]
    assert "Audit the repo." in seen_tasks[0]


def test_pipeline_stops_when_required_planning_fails(tmp_path, monkeypatch):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    config = PipelineConfig(
        name="squad-check",
        workspace=str(workspace),
        planning=SquadPlanningConfig(enabled=True),
        steps=[
            PipelineStep(
                name="repo-review",
                persona="repo-reviewer",
                task="Audit the repo.",
                writable_paths=["scratch/review.md"],
            )
        ],
    )
    step_calls = 0

    def fake_plan(cfg, *, db_path=None, verbose=False):
        return {
            "run_id": 1,
            "persona": "squad-planner",
            "plan_path": str(workspace / "missing.md"),
            "stop_reason": "stop",
            "third_umpire_verdict": "FAIL",
            "writes": 0,
            "dollars": 0.0,
            "wall_seconds": 0.0,
            "written_files": [],
            "error": None,
        }

    def fake_headless(step_config, *, db_path=None, verbose=False):
        nonlocal step_calls
        step_calls += 1
        return {}

    monkeypatch.setattr("boundary.pipeline.run_squad_planning", fake_plan)
    monkeypatch.setattr("boundary.pipeline.run_headless", fake_headless)

    out = run_pipeline(config)

    assert out["stop_reason"] == "planning_failed"
    assert out["steps"] == []
    assert step_calls == 0
