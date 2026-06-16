"""Schedule config — declarative YAML for headless agent runs."""
from __future__ import annotations
import datetime as _dt
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal

import yaml


AmbiguityPolicy = Literal["queue", "fail", "best_effort"]
FailurePolicy = Literal["digest", "silent", "email"]
CommitPolicy = Literal["refuse", "queue", "allow"]


@dataclass
class ScheduleConfig:
    name: str
    schedule: str               # "weekly mon 09:00" | "daily 09:00" | "hourly" | "cron:0 9 * * 1"
    persona: str                # banner | vision | etc.
    workspace: str              # absolute or ~-expanded path
    task: str                   # the prompt
    writable_paths: list[str] = field(default_factory=list)
    max_writes: int = 3
    min_writes: int = 1
    require_staging: bool = True
    max_unstaged_reads: int = 3
    max_iters: int = 25
    max_input_tokens: int = 500_000
    max_output_tokens: int = 50_000
    max_dollars: float | None = 1.00
    max_wall_seconds: float = 900.0
    on_ambiguity: AmbiguityPolicy = "queue"
    on_failure: FailurePolicy = "digest"
    # Commit-tool policy. "refuse" is the default and the safe choice for
    # scheduled headless runs. "queue" halts and routes to /boundary-review.
    # "allow" requires commit_allowlist to enumerate which commit tools are
    # permitted (empty list under "allow" means all — only use if you really
    # mean it).
    on_commit: CommitPolicy = "refuse"
    commit_allowlist: list[str] = field(default_factory=list)
    on_taint: str = "warn"
    client: str = "copilot"
    model: str | None = None
    notify: Any = "digest_daily"        # informational, or a notify config block
    enabled: bool = True

    @classmethod
    def load(cls, path: str | Path) -> "ScheduleConfig":
        data = yaml.safe_load(Path(path).expanduser().read_text())
        env = data.get("envelope", {})
        return cls(
            name=data["name"],
            schedule=data["schedule"],
            persona=data["persona"],
            workspace=data["workspace"],
            task=data["task"],
            writable_paths=env.get("writable_paths", []),
            max_writes=int(env.get("max_writes", 3)),
            min_writes=int(env.get("min_writes", 1)),
            require_staging=bool(env.get("require_staging", True)),
            max_unstaged_reads=int(env.get("max_unstaged_reads", 3)),
            max_iters=int(env.get("max_iters", 25)),
            max_input_tokens=int(env.get("max_input_tokens", 500_000)),
            max_output_tokens=int(env.get("max_output_tokens", 50_000)),
            max_dollars=(float(env["max_dollars"]) if env.get("max_dollars") is not None else None),
            max_wall_seconds=float(env.get("max_wall_seconds", 900.0)),
            on_ambiguity=data.get("on_ambiguity", "queue"),
            on_failure=data.get("on_failure", "digest"),
            on_commit=data.get("on_commit", "refuse"),
            on_taint=data.get("on_taint", "warn"),
            commit_allowlist=list(data.get("commit_allowlist", []) or []),
            client=data.get("client", "copilot"),
            model=data.get("model"),
            notify=data.get("notify", "digest_daily"),
            enabled=bool(data.get("enabled", True)),
        )

    def render_template(self, s: str, now: _dt.datetime | None = None) -> str:
        """Substitute {date}, {datetime}, {name} in strings."""
        now = now or _dt.datetime.now()
        return (s
            .replace("{date}", now.strftime("%Y-%m-%d"))
            .replace("{datetime}", now.strftime("%Y-%m-%dT%H%M"))
            .replace("{name}", self.name)
        )

    def rendered_writable_paths(self, now: _dt.datetime | None = None) -> list[str]:
        return [self.render_template(p, now) for p in self.writable_paths]

    def rendered_task(self, now: _dt.datetime | None = None) -> str:
        return self.render_template(self.task, now)

    def validate_commit_policy(self) -> list[str]:
        """Return a list of validation errors (empty = ok).

        Called at install time. Surfaces obvious foot-guns:
          - on_commit not in {refuse, queue, allow}
          - on_commit == "allow" with empty commit_allowlist (probably a mistake)
          - commit_allowlist set but on_commit != "allow" (ignored, warn)
        """
        errs: list[str] = []
        if self.on_commit not in ("refuse", "queue", "allow"):
            errs.append(
                f"on_commit must be one of refuse|queue|allow, got {self.on_commit!r}"
            )
        if self.on_commit == "allow" and not self.commit_allowlist:
            errs.append(
                "on_commit: allow with empty commit_allowlist allows ALL commit "
                "tools — this is almost never what you want. List the specific "
                "tool names you intend to permit (e.g. commit_allowlist: [bash_commit])."
            )
        if self.commit_allowlist and self.on_commit != "allow":
            errs.append(
                f"commit_allowlist is set but on_commit is {self.on_commit!r} — "
                f"the allowlist is only consulted under on_commit: allow."
            )
        return errs


# --- schedule string → launchd-compatible structure -------------------------

DAY_NAMES = {"mon": 1, "tue": 2, "wed": 3, "thu": 4, "fri": 5, "sat": 6, "sun": 0}


def parse_schedule(schedule_str: str) -> dict:
    """Parse a human schedule string into a normalized dict.

    Returns one of:
      {"kind": "interval", "seconds": N}                    # hourly, every 2h
      {"kind": "calendar", "weekday": int|None, "hour": H, "minute": M}
    """
    s = schedule_str.strip().lower()
    if s == "hourly":
        return {"kind": "interval", "seconds": 3600}
    m = re.match(r"every\s+(\d+)\s*(h|hours?|m|min|minutes?)", s)
    if m:
        n = int(m.group(1))
        unit = m.group(2)
        sec = n * (3600 if unit.startswith("h") else 60)
        return {"kind": "interval", "seconds": sec}
    # daily HH:MM
    m = re.match(r"daily\s+(\d{1,2}):(\d{2})", s)
    if m:
        return {"kind": "calendar", "weekday": None,
                "hour": int(m.group(1)), "minute": int(m.group(2))}
    # weekly <day> HH:MM
    m = re.match(r"weekly\s+(\w{3})\s+(\d{1,2}):(\d{2})", s)
    if m:
        day = m.group(1)
        if day not in DAY_NAMES:
            raise ValueError(f"unknown day: {day}")
        return {"kind": "calendar", "weekday": DAY_NAMES[day],
                "hour": int(m.group(2)), "minute": int(m.group(3))}
    # raw cron passthrough: "cron:0 9 * * 1"
    if s.startswith("cron:"):
        return {"kind": "cron", "expr": s[5:].strip()}
    raise ValueError(f"unparseable schedule: {schedule_str!r}")
