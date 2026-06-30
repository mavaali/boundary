"""launchd plist generator for headless schedules (macOS).

Produces user-level LaunchAgents in ~/Library/LaunchAgents/. These run as the
user and survive reboot (loaded via launchctl bootstrap).
"""
from __future__ import annotations

import os
import plistlib
import shutil
import subprocess
import sys
from pathlib import Path

from boundary.pipeline import PipelineConfig
from boundary.schedule import ScheduleConfig, parse_schedule

LAUNCH_AGENTS_DIR = Path("~/Library/LaunchAgents").expanduser()
LABEL_PREFIX = "io.boundary.schedule."


def label_for(name: str) -> str:
    safe = name.replace("/", "_").replace(" ", "_")
    return LABEL_PREFIX + safe


def plist_path_for(name: str) -> Path:
    return LAUNCH_AGENTS_DIR / f"{label_for(name)}.plist"


def _boundary_bin() -> str:
    """Find the boundary CLI script (prefer the active venv)."""
    explicit = os.environ.get("BOUNDARY_BIN")
    if explicit:
        return explicit
    found = shutil.which("boundary")
    if found:
        return found
    # fallback to python -m boundary.cli
    return f"{sys.executable} -m boundary.cli"


def generate_plist(schedule_path: Path, config: ScheduleConfig) -> dict:
    return _generate_plist(
        config_path=schedule_path,
        name=config.name,
        schedule=config.schedule,
        command="schedule-run",
    )


def generate_pipeline_plist(pipeline_path: Path, config: PipelineConfig) -> dict:
    if not config.schedule:
        raise ValueError("pipeline install requires a schedule field")
    return _generate_plist(
        config_path=pipeline_path,
        name=config.name,
        schedule=config.schedule,
        command="pipeline-run",
    )


def _generate_plist(*, config_path: Path, name: str, schedule: str, command: str) -> dict:
    parsed = parse_schedule(schedule)
    label = label_for(name)
    log_base = Path("~/.boundary/scheduler-logs").expanduser()
    log_base.mkdir(parents=True, exist_ok=True)

    bin_invocation = _boundary_bin()
    program_args = (bin_invocation.split() if " " in bin_invocation else [bin_invocation]) + [
        command, str(config_path),
    ]

    plist: dict = {
        "Label": label,
        "ProgramArguments": program_args,
        "RunAtLoad": False,
        "StandardOutPath": str(log_base / f"{label}.out.log"),
        "StandardErrorPath": str(log_base / f"{label}.err.log"),
        # Inherit PATH so gh, git, etc. are findable
        "EnvironmentVariables": {
            "PATH": os.environ.get("PATH", "/usr/local/bin:/usr/bin:/bin"),
            "HOME": str(Path.home()),
        },
    }
    if parsed["kind"] == "interval":
        plist["StartInterval"] = parsed["seconds"]
    elif parsed["kind"] == "calendar":
        cal: dict = {"Hour": parsed["hour"], "Minute": parsed["minute"]}
        if parsed.get("weekday") is not None:
            cal["Weekday"] = parsed["weekday"]
        plist["StartCalendarInterval"] = cal
    elif parsed["kind"] == "cron":
        # launchd doesn't speak cron; user must use natural schedule strings.
        raise ValueError(f"raw cron not supported on macOS launchd: {parsed['expr']}")
    return plist


def install(schedule_path: str | Path) -> Path:
    schedule_path = Path(schedule_path).expanduser().resolve()
    config = ScheduleConfig.load(schedule_path)
    # Validate commit policy before touching the filesystem.
    cp_errs = config.validate_commit_policy()
    if cp_errs:
        raise ValueError(
            "Invalid commit policy in schedule:\n  - " + "\n  - ".join(cp_errs)
        )
    LAUNCH_AGENTS_DIR.mkdir(parents=True, exist_ok=True)
    plist_data = generate_plist(schedule_path, config)
    out_path = plist_path_for(config.name)
    with open(out_path, "wb") as f:
        plistlib.dump(plist_data, f)
    # bootstrap into the user's launchd domain
    uid = os.getuid()
    domain = f"gui/{uid}"
    # uninstall first (idempotent)
    subprocess.run(["launchctl", "bootout", domain, str(out_path)],
                   capture_output=True, check=False)
    r = subprocess.run(["launchctl", "bootstrap", domain, str(out_path)],
                       capture_output=True, text=True)
    if r.returncode != 0:
        raise RuntimeError(f"launchctl bootstrap failed: {r.stderr or r.stdout}")
    return out_path


def install_pipeline(pipeline_path: str | Path) -> Path:
    pipeline_path = Path(pipeline_path).expanduser().resolve()
    config = PipelineConfig.load(pipeline_path)
    errs = config.validate()
    if errs:
        raise ValueError("Invalid pipeline:\n  - " + "\n  - ".join(errs))
    LAUNCH_AGENTS_DIR.mkdir(parents=True, exist_ok=True)
    plist_data = generate_pipeline_plist(pipeline_path, config)
    out_path = plist_path_for(config.name)
    with open(out_path, "wb") as f:
        plistlib.dump(plist_data, f)
    uid = os.getuid()
    domain = f"gui/{uid}"
    subprocess.run(["launchctl", "bootout", domain, str(out_path)],
                   capture_output=True, check=False)
    r = subprocess.run(["launchctl", "bootstrap", domain, str(out_path)],
                       capture_output=True, text=True)
    if r.returncode != 0:
        raise RuntimeError(f"launchctl bootstrap failed: {r.stderr or r.stdout}")
    return out_path


def uninstall(schedule_name: str) -> Path:
    out_path = plist_path_for(schedule_name)
    if out_path.exists():
        uid = os.getuid()
        domain = f"gui/{uid}"
        subprocess.run(["launchctl", "bootout", domain, str(out_path)],
                       capture_output=True, check=False)
        out_path.unlink()
    return out_path


def list_installed() -> list[Path]:
    if not LAUNCH_AGENTS_DIR.exists():
        return []
    return sorted(LAUNCH_AGENTS_DIR.glob(f"{LABEL_PREFIX}*.plist"))
