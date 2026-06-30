
from boundary.fielding_coach import EnvelopeProposal
from boundary.retry import dispatch_with_retry, tighten
from boundary.third_umpire import CheckResult, ThirdUmpireReport


def _prop():
    return EnvelopeProposal(
        restated_intent="x", persona="natasha", writable_paths=["a.md"],
        max_writes=5, min_writes=1, max_iters=10, task="do x", rationale="r",
    )


def _report(verdict):
    if verdict == "FAIL":
        checks = [CheckResult("scope", False, "wrote outside allowlist", "fail")]
    elif verdict == "WARN":
        checks = [CheckResult("taint", False, "taint flow", "warn")]
    else:
        checks = [CheckResult("ok", True, "fine", "info")]
    return ThirdUmpireReport(transcript_path="t", checks=checks)


def test_tighten_shrinks_and_feeds_back():
    p = tighten(_prop(), _report("FAIL"))
    assert p.max_writes == 4
    assert "PRIOR ATTEMPT FAILED" in p.task
    assert "wrote outside allowlist" in p.task


def test_tighten_floors_at_min():
    p = _prop(); p.max_writes = 1
    assert tighten(p, _report("FAIL")).max_writes == 1


def test_pass_first_try_no_retry():
    calls = []
    r = dispatch_with_retry(_prop(), "/ws", max_attempts=3,
                            dispatch_fn=lambda pr, ws, **k: calls.append(pr) or "run",
                            grade_fn=lambda run: _report("PASS"))
    assert r.final_verdict == "PASS" and len(calls) == 1


def test_fail_then_pass_retightens():
    seq = ["FAIL", "PASS"]; seen = []
    r = dispatch_with_retry(_prop(), "/ws", max_attempts=3,
                            dispatch_fn=lambda pr, ws, **k: seen.append(pr.max_writes) or "run",
                            grade_fn=lambda run: _report(seq.pop(0)))
    assert r.final_verdict == "PASS" and seen == [5, 4]
    assert len(r.attempts) == 2


def test_exhausts_attempts_on_persistent_fail():
    r = dispatch_with_retry(_prop(), "/ws", max_attempts=2,
                            dispatch_fn=lambda pr, ws, **k: "run",
                            grade_fn=lambda run: _report("FAIL"))
    assert r.final_verdict == "FAIL" and len(r.attempts) == 2
