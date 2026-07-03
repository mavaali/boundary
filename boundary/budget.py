"""Cross-run spend budgets.

The envelope's per-run caps (max_dollars, max_input/output_tokens) bound a
single run. A schedule that fires many times a day places no ceiling on the SUM
of those runs — 100 runs at $1.00 each is a $100 day nobody signed off on.
`SpendBudget` adds that ceiling: calendar windows (daily / weekly / monthly,
each resetting on the local calendar boundary) and a trailing rolling window,
aggregated over the run-history ledger. That ledger is the existing `runs`
table (see history.py) — the source of truth for per-run spend — so there is no
second store to drift out of sync.

Two effects at run time, both wired in run_headless:

  1. Pre-run gate. If any active window is already at or over its cap, the run
     is skipped before any spend is incurred (stop_reason "skipped_budget").
  2. Headroom clamp. Otherwise the run's per-run max_dollars is clamped to the
     tightest remaining headroom across the active windows, so the envelope's
     own spend gradient and hard halt enforce the cross-run ceiling from inside
     the run — a run can never carry a window past its cap, only up to it.

Time is injected (`now`) rather than read from a frozen clock, mirroring the
schedule.render_template(now=...) convention, so window boundaries are testable
without patching the clock.
"""
from __future__ import annotations

import datetime as _dt
from dataclasses import dataclass, replace
from typing import Protocol


class SpendSource(Protocol):
    """The slice of History that a budget reads. Anything with this method
    (the real History, or a fake in tests) can back a budget check."""
    def spend_since(self, workspace: str | None, since: float,
                    tag: tuple[str, str | None] | None = None) -> float: ...


def _nn(v: object) -> float | None:
    return None if v is None else float(v)


@dataclass(frozen=True)
class SpendBudget:
    """A cross-run spend ceiling. Any subset of windows may be set; an unset
    window imposes no limit. `scope` decides whether spend is summed for this
    workspace only ("workspace", default) or across all of them ("global")."""
    daily: float | None = None
    weekly: float | None = None
    monthly: float | None = None
    rolling: float | None = None          # $ cap over the trailing window
    rolling_hours: float = 24.0           # length of the rolling window
    scope: str = "workspace"              # "workspace" | "global" | "tag"
    scope_tag: str | None = None          # attribution key when scope == "tag"

    def is_active(self) -> bool:
        return any(c is not None for c in
                   (self.daily, self.weekly, self.monthly, self.rolling))

    @classmethod
    def from_config(cls, d: dict | None) -> SpendBudget | None:
        """Build from a YAML `budget:` block, or None if absent/empty.

        `scope` may be "workspace" (default), "global", or "tag". For "tag",
        `scope_tag` names the attribution dimension the budget is per-value of
        (e.g. scope: tag / scope_tag: tenant => one budget per tenant)."""
        if not d:
            return None
        scope = str(d.get("scope", "workspace"))
        b = cls(
            daily=_nn(d.get("daily")),
            weekly=_nn(d.get("weekly")),
            monthly=_nn(d.get("monthly")),
            rolling=_nn(d.get("rolling")),
            rolling_hours=float(d.get("rolling_hours", 24.0)),
            scope=scope,
            scope_tag=d.get("scope_tag") or (d.get("scope").split(":", 1)[1]
                                             if scope.startswith("tag:") else None),
        )
        # Normalize the "tag:key" shorthand to scope="tag", scope_tag="key".
        if scope.startswith("tag:"):
            b = replace(b, scope="tag")
        return b if b.is_active() else None


def _window_starts(now: _dt.datetime) -> dict[str, float]:
    """Calendar window start epochs: local midnight, Monday 00:00, 1st 00:00."""
    day = now.replace(hour=0, minute=0, second=0, microsecond=0)
    week = day - _dt.timedelta(days=now.weekday())   # Monday
    month = day.replace(day=1)
    return {"daily": day.timestamp(), "weekly": week.timestamp(),
            "monthly": month.timestamp()}


@dataclass
class BudgetStatus:
    active: bool
    exhausted: bool
    remaining: float                 # tightest headroom across windows (clamped >= 0)
    binding: str | None              # the window with least headroom
    windows: dict                    # name -> {spent, cap, remaining, since}

    @property
    def spent_binding(self) -> float:
        return self.windows[self.binding]["spent"] if self.binding else 0.0

    @property
    def cap_binding(self) -> float:
        return self.windows[self.binding]["cap"] if self.binding else 0.0

    def as_dict(self) -> dict:
        return {
            "exhausted": self.exhausted,
            "remaining": round(self.remaining, 6) if self.remaining != float("inf") else None,
            "binding": self.binding,
            "windows": self.windows,
        }

    def summary(self) -> str:
        if not self.active:
            return "no budget"
        parts = [f"{n}=${w['spent']:.4f}/${w['cap']:.2f}" for n, w in self.windows.items()]
        return " ".join(parts)


def evaluate_budget(budget: SpendBudget, source: SpendSource, workspace: str,
                    now: _dt.datetime | None = None,
                    attribution: dict | None = None) -> BudgetStatus:
    """Aggregate prior spend over each active window and report headroom.

    `exhausted` is True when any window is already at/over its cap. `remaining`
    is the least headroom across windows (>= 0), which is what a run's per-run
    max_dollars should be clamped to. For a tag-scoped budget, `attribution`
    supplies the current run's tag values so spend is summed per tag value."""
    now = now or _dt.datetime.now()
    tag: tuple[str, str | None] | None = None
    if budget.scope == "global":
        ws = None
    elif budget.scope == "tag" and budget.scope_tag:
        # Per-value-of-tag budget: sum across all workspaces sharing this run's
        # value for the scope tag (e.g. everything tagged tenant=acme).
        ws = None
        tag = (budget.scope_tag, (attribution or {}).get(budget.scope_tag))
    else:
        ws = workspace
    starts = _window_starts(now)
    specs: list[tuple[str, float, float]] = []
    if budget.daily is not None:
        specs.append(("daily", starts["daily"], budget.daily))
    if budget.weekly is not None:
        specs.append(("weekly", starts["weekly"], budget.weekly))
    if budget.monthly is not None:
        specs.append(("monthly", starts["monthly"], budget.monthly))
    if budget.rolling is not None:
        specs.append(("rolling", now.timestamp() - budget.rolling_hours * 3600.0, budget.rolling))

    if not specs:
        return BudgetStatus(active=False, exhausted=False,
                            remaining=float("inf"), binding=None, windows={})

    windows: dict = {}
    remaining = float("inf")
    binding: str | None = None
    for name, since, cap in specs:
        spent = source.spend_since(ws, since, tag=tag)
        head = cap - spent
        windows[name] = {"spent": round(spent, 6), "cap": cap,
                         "remaining": round(head, 6), "since": since}
        if head < remaining:
            remaining = head
            binding = name

    return BudgetStatus(active=True, exhausted=remaining <= 0,
                        remaining=max(remaining, 0.0), binding=binding, windows=windows)
