"""Path-collision guards: best-of-K run isolation and pipeline step-name uniqueness.

Both catch a silent-clobber class of bug — two runs (or two steps) writing the
same place so one overwrites the other with no error.
"""
from __future__ import annotations

import pytest

from boundary.multirun import template_run_paths, validate_run_path_isolation
from boundary.pipeline import PipelineConfig, PipelineStep

# --- best-of-K run-path isolation ---------------------------------------------

def test_literal_paths_isolate_cleanly():
    assert validate_run_path_isolation(["out.md", "scratch/x.md"], k=3) == []


def test_single_run_needs_no_isolation():
    assert validate_run_path_isolation(["scratch/*.md"], k=1) == []


def test_glob_path_cannot_be_isolated_across_runs():
    problems = validate_run_path_isolation(["scratch/*.md"], k=3)
    assert len(problems) == 1
    assert "glob" in problems[0]
    # the underlying templating is what makes it unsafe: glob maps to itself
    assert template_run_paths(["scratch/*.md"], 1) == {"scratch/*.md": "scratch/*.md"}
    assert template_run_paths(["scratch/*.md"], 2) == {"scratch/*.md": "scratch/*.md"}


def test_mixed_literal_and_glob_reports_only_the_glob():
    problems = validate_run_path_isolation(["out.md", "scratch/*.md"], k=2)
    assert len(problems) == 1
    assert "scratch/*.md" in problems[0]


def test_empty_writable_paths_is_fine():
    assert validate_run_path_isolation([], k=5) == []


# --- pipeline step-name uniqueness --------------------------------------------

def _cfg(step_names):
    return PipelineConfig(
        name="p",
        workspace="/tmp/ws",
        steps=[PipelineStep(name=n, persona="x", task="t") for n in step_names],
    )


def test_unique_step_names_validate_clean():
    assert _cfg(["a", "b", "c"]).validate() == []


def test_duplicate_step_names_are_flagged():
    errors = _cfg(["review", "review", "draft"]).validate()
    assert any("duplicate step name" in e and "review" in e for e in errors)


def test_run_best_of_k_raises_on_unisolatable_paths():
    from boundary.envelope import Envelope
    from boundary.multirun import run_best_of_k

    env = Envelope(writable_paths=["scratch/*.md"])
    with pytest.raises(ValueError, match="cannot be isolated"):
        run_best_of_k(
            agent_factory=lambda k: None,
            base_envelope=env,
            task="t",
            workspace_root="/tmp/ws",
            k=3,
        )
