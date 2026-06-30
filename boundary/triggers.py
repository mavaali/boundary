"""Triggers — declarative rules that turn a finished run's outcome into new
queued tasks. The BabyAGI results->tasks->reprioritize loop, made bounded.

A run completes; its outcome (Third Umpire verdict, discovered items, error) is
matched against a list of TriggerRules; matching rules emit NewTask records.
Crucially these are *enqueued* (status=pending), not executed — the human
ratifies before any dispatch. That is the line between Boundary and BabyAGI:
the loop may propose its own next work, it may not run it unasked.

evaluate_triggers is pure (no I/O) so the rule semantics are unit-testable; the
caller (headless) persists the returned NewTasks via History.add_task.
"""
from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class TriggerRule:
    on: str                       # "verdict" | "discovery" | "always"
    action: str                   # "enqueue_discovered" | "enqueue_followup"
    when: str | None = None       # for on=verdict: PASS|WARN|FAIL (None = any)
    priority: int = 2             # priority stamped on emitted tasks
    max_emit: int = 10            # cap tasks emitted per rule per run

    @classmethod
    def from_dict(cls, d: dict) -> TriggerRule:
        # YAML 1.1 parses a bare `on:` key as the boolean True (likewise off/yes/no).
        # Accept that gracefully so `on: discovery` works unquoted in schedule YAML.
        on = d.get("on", d.get(True, d.get(False)))
        when = d.get("when")
        if isinstance(when, bool):  # `when: off`/`on` -> normalize back to text is N/A here
            when = str(when)
        return cls(
            on=str(on), action=d["action"], when=when,
            priority=int(d.get("priority", 2)), max_emit=int(d.get("max_emit", 10)),
        )


@dataclass
class NewTask:
    title: str
    detail: str
    priority: int
    origin: str | None = None
    trigger_rule: str = ""


@dataclass
class RunOutcome:
    verdict: str | None = None              # PASS | WARN | FAIL | None
    discovered: list = field(default_factory=list)  # list[DiscoveredTask]
    error: str | None = None
    schedule_name: str | None = None


def _matches(rule: TriggerRule, outcome: RunOutcome) -> bool:
    if rule.on == "always":
        return True
    if rule.on == "discovery":
        return bool(outcome.discovered)
    if rule.on == "verdict":
        if outcome.verdict is None:
            return False
        return rule.when is None or rule.when.upper() == outcome.verdict.upper()
    return False


def evaluate_triggers(rules: list[TriggerRule], outcome: RunOutcome) -> list[NewTask]:
    """Pure: match rules against a run outcome, return tasks to enqueue."""
    emitted: list[NewTask] = []
    def label(r):
        return f"{r.on}:{r.when or '*'}->{r.action}"
    for rule in rules:
        if not _matches(rule, outcome):
            continue
        if rule.action == "enqueue_discovered":
            for d in outcome.discovered[: rule.max_emit]:
                emitted.append(NewTask(
                    title=getattr(d, "title", str(d))[:120],
                    detail=getattr(d, "detail", ""),
                    priority=rule.priority,
                    origin=getattr(d, "origin", None),
                    trigger_rule=label(rule),
                ))
        elif rule.action == "enqueue_followup":
            v = outcome.verdict or "?"
            why = (outcome.error or "").strip()
            detail = (f"Follow-up from a {v} run"
                      + (f" (schedule: {outcome.schedule_name})" if outcome.schedule_name else "")
                      + (f".\n\nError/reason:\n{why}" if why else ".")
                      + "\n\nInvestigate and propose a bounded fix.")
            emitted.append(NewTask(
                title=f"Follow-up: {v} run needs attention",
                detail=detail, priority=rule.priority, trigger_rule=label(rule),
            ))
    return emitted


def load_rules(raw: list[dict] | None) -> list[TriggerRule]:
    return [TriggerRule.from_dict(d) for d in (raw or [])]
