import json
from boundary.third_umpire import ThirdUmpire


def _grade(tmp_path, end_extra, events=None):
    evs = [
        {"type": "envelope_start", "require_staging": True, "writable_paths": ["out.md"]},
        {"type": "envelope_end", "on_commit": "refuse", "on_taint": "warn",
         "tainted_reads": end_extra.get("tainted_reads", 0),
         "events": events or [], **end_extra},
        {"type": "end", "iterations": 2},
    ]
    p = tmp_path / "t.jsonl"
    p.write_text("\n".join(json.dumps(e) for e in evs), encoding="utf-8")
    return ThirdUmpire.grade(p)


def test_tainted_run_without_srt_fails_egress_uncontained(tmp_path):
    r = _grade(tmp_path, {"tainted_reads": 1, "sandbox_driver": "seatbelt"})
    c = [c for c in r.checks if c.name == "egress_uncontained"]
    assert len(c) == 1 and not c[0].passed and c[0].severity == "fail"
    assert r.verdict == "FAIL"


def test_tainted_run_with_srt_has_no_egress_fail(tmp_path):
    r = _grade(tmp_path, {"tainted_reads": 1, "sandbox_driver": "srt"})
    assert [c for c in r.checks if c.name == "egress_uncontained"] == []


def test_clean_run_no_egress_fail(tmp_path):
    r = _grade(tmp_path, {"tainted_reads": 0, "sandbox_driver": "seatbelt"})
    assert [c for c in r.checks if c.name == "egress_uncontained"] == []


def test_old_transcript_without_driver_is_skipped(tmp_path):
    r = _grade(tmp_path, {"tainted_reads": 1})   # no sandbox_driver key
    assert [c for c in r.checks if c.name == "egress_uncontained"] == []


def test_taint_egress_event_surfaced(tmp_path):
    r = _grade(tmp_path, {"tainted_reads": 1, "sandbox_driver": "srt"},
               events=[{"kind": "taint_egress", "tool": "fetch_url", "detail": "host=exfil.test off-allowlist", "iteration": 3}])
    c = [c for c in r.checks if c.name == "taint_egress"]
    assert len(c) == 1 and not c[0].passed and c[0].severity == "warn"
