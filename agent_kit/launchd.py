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

from agent_kit.schedule import ScheduleConfig, parse_schedule

LAUNCH_AGENTS_DIR = Path("~/Library/LaunchAgents").expanduser()
LABEL_PREFIX = "io.agent-kit.schedule."


def label_for(name: str) -> str:
    safe = name.replace("/", "_").replace(" ", "_")
    return LABEL_PREFIX + safe


def plist_path_for(name: str) -> Path:
    return LAUNCH_AGENTS_DIR / f"{label_for(name)}.plist"


def _agent_kit_bin() -> str:
    """Find the agent-kit CLI script (prefer the active venv)."""
    explicit = os.environ.get("AGENT_KIT_BIN")
    if explicit:
        return explicit
    found = shutil.which("agent-kit")
    if found:
        return found
    # fallback to python -m agent_kit.cli
    return f"{sys.executable} -m agent_kit.cli"


def generate_plist(schedule_path: Path, config: ScheduleConfig) -> dict:
    parsed = parse_schedule(config.schedule)
    label = label_for(config.name)
    log_base = Path("~/.agent-kit/launchd-logs").expanduser()
    log_base.mkdir(parents=True, exist_ok=True)

    bin_invocation = _agent_kit_bin()
    program_args = (bin_invocation.split() if " " in bin_invocation else [bin_invocation]) + [
        "schedule-run", str(schedule_path),
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
