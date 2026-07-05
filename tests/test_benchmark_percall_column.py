"""run_suite publishes the per-call baseline alongside undefended and envelope,
so the accumulation task's three-way delta (undef=1, per-call=1, envelope=0) is
visible in the report — that contrast is the whole point of the task.
"""
from __future__ import annotations

from benchmarks.harness import report_markdown, run_suite
from benchmarks.suite import TASKS
from boundary.clients.base import ChatResponse, Message, ToolCall


class ScriptedClient:
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


def _mock_client_for(task, mode):
    calls = task.benign_only_calls if mode == "benign" else task.scripted_calls
    return lambda: ScriptedClient(calls)


def test_suite_reports_per_call_baseline():
    rows = {r.task_id: r for r in run_suite(_mock_client_for)}
    drip = rows["drip_exfil_over_writes"]
    # The defining three-way result.
    assert drip.asr_undef is True
    assert drip.asr_percall is True     # per-call mediation is blind to the drip
    assert drip.asr_def is False        # write cardinality halts it
    # A single-stage exfil: per-call mediation DOES catch it (control).
    assert rows["exfil_via_forbidden_write"].asr_percall is False


def test_report_shows_per_call_column():
    rows = run_suite(_mock_client_for)
    md = report_markdown(rows, model="mock")
    assert "per-call" in md.lower()
