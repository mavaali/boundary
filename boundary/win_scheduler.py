"""Windows headless scheduler backend (Task Scheduler via schtasks.exe).

Mirrors the launchd backend's API surface. Tasks are user-scope (no admin
elevation needed). Logs are appended to ``%USERPROFILE%\\.boundary\\scheduler-logs\\``.

The action is wrapped as ``cmd /c "<bin> <subcmd> <yaml> >> out.log 2>> err.log"``
because schtasks does not natively redirect stdout/stderr.
"""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path

from boundary.pipeline import PipelineConfig
from boundary.schedule import ScheduleConfig, parse_schedule

TASK_FOLDER = "\\boundary\\"  # schtasks-style folder; created on first install
LABEL_PREFIX = "io.boundary.schedule."
SCHEDULER_LOGS_DIR = Path("~/.boundary/scheduler-logs").expanduser()
TASK_LIST_DIR = Path("~/.boundary/scheduler-tasks").expanduser()
WEEKDAY_MAP = {0: "SUN", 1: "MON", 2: "TUE", 3: "WED", 4: "THU", 5: "FRI", 6: "SAT"}


def label_for(name: str) -> str:
    safe = name.replace("/", "_").replace(" ", "_")
    return LABEL_PREFIX + safe


def task_path_for(name: str) -> Path:
    """Marker file path used by list_installed() so we can enumerate our own tasks
    without parsing schtasks /query output (which is locale-dependent)."""
    return TASK_LIST_DIR / f"{label_for(name)}.task"


def _boundary_bin() -> str:
    explicit = os.environ.get("BOUNDARY_BIN")
    if explicit:
        return explicit
    found = shutil.which("boundary") or shutil.which("boundary.exe")
    if found:
        return found
    return f"{sys.executable} -m boundary.cli"


def _build_action(config_path: Path, command: str, label: str) -> str:
    bin_invocation = _boundary_bin()
    SCHEDULER_LOGS_DIR.mkdir(parents=True, exist_ok=True)
    out_log = SCHEDULER_LOGS_DIR / f"{label}.out.log"
    err_log = SCHEDULER_LOGS_DIR / f"{label}.err.log"
    # cmd /c so the redirection happens; quoting matters because paths can have spaces.
    return (
        f'cmd /c ""{bin_invocation}" {command} "{config_path}" '
        f'>> "{out_log}" 2>> "{err_log}""'
    )


def _schtasks_args_for_schedule(schedule: str) -> list[str]:
    parsed = parse_schedule(schedule)
    if parsed["kind"] == "interval":
        minutes = max(1, int(round(parsed["seconds"] / 60)))
        return ["/sc", "MINUTE", "/mo", str(minutes)]
    if parsed["kind"] == "calendar":
        hh = f"{parsed['hour']:02d}:{parsed['minute']:02d}"
        if parsed.get("weekday") is not None:
            day = WEEKDAY_MAP[parsed["weekday"]]
            return ["/sc", "WEEKLY", "/d", day, "/st", hh]
        return ["/sc", "DAILY", "/st", hh]
    if parsed["kind"] == "cron":
        raise ValueError(
            f"raw cron not supported on Windows Task Scheduler: {parsed['expr']}"
        )
    raise ValueError(f"unrecognized schedule kind: {parsed['kind']}")


def _run_schtasks(args: list[str]) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["schtasks", *args], capture_output=True, text=True, check=False
    )


def _install_common(config_path: Path, name: str, schedule: str, command: str) -> Path:
    label = label_for(name)
    task_name = TASK_FOLDER + label  # e.g. \boundary\io.boundary.schedule.foo
    action = _build_action(config_path, command, label)
    sched_args = _schtasks_args_for_schedule(schedule)

    # Idempotent: delete first if present (ignore failure).
    _run_schtasks(["/delete", "/tn", task_name, "/f"])

    create_args = [
        "/create",
        "/tn", task_name,
        "/tr", action,
        *sched_args,
        "/f",  # force overwrite
    ]
    r = _run_schtasks(create_args)
    if r.returncode != 0:
        raise RuntimeError(f"schtasks /create failed: {r.stderr or r.stdout}")

    TASK_LIST_DIR.mkdir(parents=True, exist_ok=True)
    marker = task_path_for(name)
    marker.write_text(
        f"{task_name}\n{config_path}\n{command}\n{schedule}\n",
        encoding="utf-8",
    )
    return marker


def install(schedule_path: str | Path) -> Path:
    schedule_path = Path(schedule_path).expanduser().resolve()
    config = ScheduleConfig.load(schedule_path)
    cp_errs = config.validate_commit_policy()
    if cp_errs:
        raise ValueError(
            "Invalid commit policy in schedule:\n  - " + "\n  - ".join(cp_errs)
        )
    return _install_common(schedule_path, config.name, config.schedule, "schedule-run")


def install_pipeline(pipeline_path: str | Path) -> Path:
    pipeline_path = Path(pipeline_path).expanduser().resolve()
    config = PipelineConfig.load(pipeline_path)
    if not config.schedule:
        raise ValueError("pipeline install requires a schedule field")
    errs = config.validate()
    if errs:
        raise ValueError("Invalid pipeline:\n  - " + "\n  - ".join(errs))
    return _install_common(pipeline_path, config.name, config.schedule, "pipeline-run")


def uninstall(schedule_name: str) -> Path:
    label = label_for(schedule_name)
    task_name = TASK_FOLDER + label
    _run_schtasks(["/delete", "/tn", task_name, "/f"])
    marker = task_path_for(schedule_name)
    if marker.exists():
        marker.unlink()
    return marker


def list_installed() -> list[Path]:
    if not TASK_LIST_DIR.exists():
        return []
    return sorted(TASK_LIST_DIR.glob(f"{LABEL_PREFIX}*.task"))
