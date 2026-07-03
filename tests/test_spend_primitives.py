"""Tests for the spending primitives on Envelope.

Two behaviours:
- Fail-closed pricing: a model absent from token_rates must NOT be priced at
  $0.0 (which would let it slip past max_dollars entirely). Under the default
  on_unpriced_model="max_rate" policy it is priced at a conservative upper
  bound so the dollar cap still binds. "zero" restores the fail-open legacy.
- Spend-pressure gradient: before the hard budget_halt at 100% of a spend cap,
  the agent is nudged to converge at spend_pressure_at fractions of whichever
  cap is closest to breach. Mirrors the iteration budget_pressure nudge.
"""
from __future__ import annotations

from boundary.agent import Agent
from boundary.envelope import Envelope, EnvelopeRunner
from boundary.tools.registry import Tool


# --- Fail-closed pricing (pure, no loop) ---------------------------------

def test_known_model_prices_unchanged():
    e = Envelope()
    # 1M input @ $3, 1M output @ $15 for sonnet-4.5 -> $18 exactly.
    assert abs(e.estimate_cost("claude-sonnet-4.5", 1_000_000, 1_000_000) - 18.0) < 1e-9


def test_unpriced_model_is_not_free_by_default():
    e = Envelope()
    assert not e.is_priced("some-new-model-v9")
    cost = e.estimate_cost("some-new-model-v9", 1_000_000, 1_000_000)
    # Must be > 0 so max_dollars still binds for an unlisted model.
    assert cost > 0.0


def test_unpriced_default_is_conservative_upper_bound():
    e = Envelope()
    unpriced = e.estimate_cost("some-new-model-v9", 500_000, 100_000)
    # No known model should be more expensive than the fallback estimate.
    for model in e.token_rates:
        assert e.estimate_cost(model, 500_000, 100_000) <= unpriced + 1e-9


def test_zero_policy_restores_fail_open():
    e = Envelope(on_unpriced_model="zero")
    assert e.rate_for("some-new-model-v9") is None
    assert e.estimate_cost("some-new-model-v9", 1_000_000, 1_000_000) == 0.0


def test_borrow_policy_uses_named_model_rate():
    e = Envelope(on_unpriced_model="claude-haiku-4.5")
    borrowed = e.estimate_cost("mystery-model", 1_000_000, 1_000_000)
    haiku = e.estimate_cost("claude-haiku-4.5", 1_000_000, 1_000_000)
    assert abs(borrowed - haiku) < 1e-9


def test_borrow_policy_falls_back_to_max_rate_when_absent():
    e = Envelope(on_unpriced_model="also-not-in-card")
    # Neither the target model nor the borrow target is priced -> max_rate.
    assert e.estimate_cost("mystery", 1_000_000, 0) > 0.0


def test_fail_closed_makes_dollar_cap_bind():
    # Tokens cheap enough to stay under the cap on the cheapest model, but the
    # conservative fallback prices them over it -> the cap now bites.
    e = Envelope(max_dollars=1.0)
    tokens_in, tokens_out = 400_000, 20_000
    cheap = Envelope().estimate_cost("claude-haiku-4.5", tokens_in, tokens_out)
    assert cheap < 1.0  # would sail under the cap if priced as haiku
    assert e.estimate_cost("unlisted-model", tokens_in, tokens_out) >= 1.0


# --- Spend-pressure gradient (drives the full loop) ----------------------

class _TokenClient:
    """Emits a harmless read call each turn with fixed token usage, so the loop
    accrues spend and marches toward the caps without ever stopping itself."""
    model = "claude-sonnet-4.6"

    def __init__(self, out_per_call: int):
        self.out = out_per_call

    def chat(self, messages, tools=None, **kw):
        from boundary.clients.base import ChatResponse, Message, ToolCall
        tc = ToolCall(id="r", name="noop", arguments={"x": "read"})
        return ChatResponse(
            message=Message(role="assistant", content="", tool_calls=[tc]),
            finish_reason="tool_calls",
            input_tokens=0, output_tokens=self.out, cached_input_tokens=0,
        )


def _noop_tool():
    def noop(x: str = "") -> str:
        return "constant-result"
    return Tool(name="noop", description="x",
                parameters={"type": "object", "properties": {"x": {"type": "string"}}, "required": []},
                fn=noop, kind="read")


def _agent(ws, client):
    a = Agent(name="s", system_prompt="x", workspace=str(ws), client=client,
              enable_fs=True, enable_shell=False, enable_web=False, transcript=False)
    a.tools.register(_noop_tool())
    return a


def test_spend_pressure_fires_before_hard_halt(tmp_path):
    # Cap 100 output tokens, 30/turn. Gate sees cumulative totals: crosses 50%
    # then 90% (spend_pressure) before hitting 100% (budget_halt).
    client = _TokenClient(out_per_call=30)
    env = Envelope(writable_paths=["out.md"], require_staging=False,
                   max_input_tokens=None, max_output_tokens=100, max_dollars=None,
                   spend_pressure_at=(0.5, 0.9))
    res = EnvelopeRunner(_agent(tmp_path, client), env).run("go")
    assert res.loop_result.stop_reason == "budget_halt"
    pressures = [e for e in res.events if e.kind == "spend_pressure"]
    assert pressures, "expected at least one spend_pressure event before the halt"
    # Every pressure event must precede the terminal budget_halt.
    halt_iter = next(e.iteration for e in res.events if e.kind == "budget_halt")
    assert all(e.iteration < halt_iter for e in pressures)


def test_each_threshold_fires_at_most_once(tmp_path):
    client = _TokenClient(out_per_call=30)
    env = Envelope(writable_paths=["out.md"], require_staging=False,
                   max_input_tokens=None, max_output_tokens=100, max_dollars=None,
                   spend_pressure_at=(0.5, 0.9))
    res = EnvelopeRunner(_agent(tmp_path, client), env).run("go")
    pressures = [e for e in res.events if e.kind == "spend_pressure"]
    # Two distinct thresholds -> at most two nudges, never a repeat storm.
    assert 1 <= len(pressures) <= 2


def test_gradient_disabled_when_empty(tmp_path):
    client = _TokenClient(out_per_call=30)
    env = Envelope(writable_paths=["out.md"], require_staging=False,
                   max_input_tokens=None, max_output_tokens=100, max_dollars=None,
                   spend_pressure_at=())
    res = EnvelopeRunner(_agent(tmp_path, client), env).run("go")
    assert res.loop_result.stop_reason == "budget_halt"
    assert not any(e.kind == "spend_pressure" for e in res.events)
