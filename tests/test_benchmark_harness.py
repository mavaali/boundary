"""The benchmark harness, verified with a scripted mock model (no real model).

A 'fooled' agent replays each task's scripted_calls (benign action + attack
action). The only variable between the two runs is the envelope, so the delta
in attack-success isolates the defense.
"""
from __future__ import annotations

from benchmarks.harness import report_markdown, run_suite, run_task
from benchmarks.suite import TASKS
from boundary.clients.base import ChatResponse, Message, ToolCall


class ScriptedClient:
    """Replays a fixed sequence of tool calls, one per turn, then stops."""

    def __init__(self, calls):
        self._calls = list(calls)
        self._i = 0

    def chat(self, messages, tools=None, **kwargs):
        if self._i < len(self._calls):
            name, args = self._calls[self._i]
            self._i += 1
            return ChatResponse(
                Message(role="assistant", content=None, tool_calls=[ToolCall(f"c{self._i}", name, args)]),
                "tool_calls",
            )
        return ChatResponse(Message(role="assistant", content="done"), "stop")


def _task(tid):
    return next(t for t in TASKS if t.id == tid)


def test_envelope_blocks_forbidden_write_attack():
    task = _task("exfil_via_forbidden_write")
    r = run_task(task, lambda: ScriptedClient(task.scripted_calls), defended=True)
    assert r.attack is False, "envelope should block the off-allowlist write"
    assert r.benign is True, "benign note should still be written"


def test_undefended_forbidden_write_attack_succeeds():
    task = _task("exfil_via_forbidden_write")
    r = run_task(task, lambda: ScriptedClient(task.scripted_calls), defended=False)
    assert r.attack is True, "without the envelope the attack should land"
    assert r.benign is True


def test_unauthorized_commit_blocked_defended_fires_undefended():
    task = _task("unauthorized_commit")
    assert run_task(task, lambda: ScriptedClient(task.scripted_calls), defended=True).attack is False
    assert run_task(task, lambda: ScriptedClient(task.scripted_calls), defended=False).attack is True


def _mock_client_for(task, mode):
    calls = task.benign_only_calls if mode == "benign" else task.scripted_calls
    return lambda: ScriptedClient(calls)


def test_suite_drives_asr_to_zero_and_surfaces_the_taint_tradeoff():
    rows = {r.task_id: r for r in run_suite(_mock_client_for)}
    # Every task: undefended attack lands, defended blocks it.
    assert all(r.asr_undef for r in rows.values())
    assert not any(r.asr_def for r in rows.values())
    # Non-taint tasks: clean utility preserved under the envelope.
    for tid in ("exfil_via_forbidden_write", "unauthorized_commit"):
        assert rows[tid].utility_def and rows[tid].utility_undef
    # Taint task: on_taint=refuse over-blocks the benign tainted write (honest
    # tradeoff) — utility drops under the envelope.
    assert rows["exfil_tainted_to_sink"].utility_undef
    assert not rows["exfil_tainted_to_sink"].utility_def
    md = report_markdown(list(rows.values()), model="mock")
    assert "ASR" in md and "Aggregate" in md
