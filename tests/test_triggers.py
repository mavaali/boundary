from dataclasses import dataclass

from boundary.triggers import (
    TriggerRule, NewTask, RunOutcome, evaluate_triggers, load_rules,
)
from boundary.history import History


@dataclass
class _D:  # stand-in for DiscoveredTask
    title: str
    detail: str = "d"
    origin: str = "spec.md"


# --- pure trigger evaluation -------------------------------------------------

def test_verdict_fail_emits_followup():
    rules = [TriggerRule(on="verdict", when="FAIL", action="enqueue_followup", priority=1)]
    out = evaluate_triggers(rules, RunOutcome(verdict="FAIL", error="boom"))
    assert len(out) == 1 and out[0].priority == 1
    assert "boom" in out[0].detail and "verdict:FAIL->enqueue_followup" == out[0].trigger_rule


def test_verdict_when_none_matches_any():
    rules = [TriggerRule(on="verdict", action="enqueue_followup")]
    assert len(evaluate_triggers(rules, RunOutcome(verdict="WARN"))) == 1


def test_verdict_mismatch_no_emit():
    rules = [TriggerRule(on="verdict", when="FAIL", action="enqueue_followup")]
    assert evaluate_triggers(rules, RunOutcome(verdict="PASS")) == []


def test_discovery_enqueues_each_item_capped():
    rules = [TriggerRule(on="discovery", action="enqueue_discovered", max_emit=2)]
    out = evaluate_triggers(rules, RunOutcome(discovered=[_D("a"), _D("b"), _D("c")]))
    assert len(out) == 2 and out[0].title == "a" and out[0].origin == "spec.md"


def test_always_matches_even_without_verdict():
    rules = [TriggerRule(on="always", action="enqueue_followup")]
    assert len(evaluate_triggers(rules, RunOutcome())) == 1


def test_load_rules_from_dicts():
    rules = load_rules([{"on": "verdict", "when": "FAIL", "action": "enqueue_followup", "priority": 1}])
    assert rules[0].on == "verdict" and rules[0].priority == 1


def test_yaml_bool_on_key_gotcha():
    # YAML 1.1 parses bare `on:` as boolean True. The loader must recover it.
    rules = load_rules([{True: "discovery", "action": "enqueue_discovered"}])
    assert rules[0].on == "discovery"


# --- task store --------------------------------------------------------------

def test_task_queue_priority_order(tmp_path):
    h = History(db_path=tmp_path / "h.db")
    h.add_task(title="low", priority=3)
    h.add_task(title="high", priority=1)
    h.add_task(title="mid", priority=2)
    ready = h.list_tasks(status="pending")
    assert [t["title"] for t in ready] == ["high", "mid", "low"]


def test_task_status_transitions(tmp_path):
    h = History(db_path=tmp_path / "h.db")
    tid = h.add_task(title="x")
    h.set_task_status(tid, "ready")
    assert h.list_tasks(status="ready")[0]["id"] == tid
    assert h.list_tasks(status="pending") == []


def test_causal_edge_parent_run(tmp_path):
    h = History(db_path=tmp_path / "h.db")
    tid = h.add_task(title="child", parent_run_id=42, trigger_rule="verdict:FAIL->enqueue_followup")
    row = h.list_tasks()[0]
    assert row["parent_run_id"] == 42 and row["trigger_rule"].endswith("enqueue_followup")
