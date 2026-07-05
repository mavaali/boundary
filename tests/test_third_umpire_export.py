"""The Third Umpire verdict as an exportable, standard-shaped artifact.

Auditors and CI want *evidence of runtime enforcement*, not a policy PDF. A
spec-relative verdict is exactly that evidence — but only if it exports as a
stable, machine-readable document. as_dict()/to_json() are that contract.
"""
from __future__ import annotations

import json

from boundary.third_umpire import ThirdUmpire


def _tx(tmp_path, *, refused=False):
    events = [{"kind": "write_refused", "tool": "write_file", "detail": "path=x", "iteration": 1}] if refused else []
    records = [
        {"type": "envelope_start", "writable_paths": ["out.md"], "require_staging": False, "min_writes": 1},
        {"type": "assistant", "iteration": 1, "content": "[DATA] done", "tool_calls": []},
        {"type": "envelope_end", "writes_attempted": 1, "writes_executed": 1,
         "external_calls": 0, "commit_attempted": 0, "commit_executed": 0,
         "input_tokens": 1000, "output_tokens": 500, "estimated_dollars": 0.01,
         "events": events},
        {"type": "end", "iterations": 2},
    ]
    p = tmp_path / "t.jsonl"
    p.write_text("\n".join(json.dumps(r) for r in records) + "\n")
    return p


def test_as_dict_has_verdict_summary_and_checks(tmp_path):
    report = ThirdUmpire.grade(_tx(tmp_path))
    d = report.as_dict()
    assert d["verdict"] == report.verdict
    assert d["schema"].startswith("boundary.third-umpire/")
    assert isinstance(d["summary"], dict) and d["summary"]
    assert d["checks"] and all(
        {"name", "passed", "severity", "detail"} <= set(c) for c in d["checks"]
    )


def test_as_dict_verdict_reflects_a_failing_check(tmp_path):
    d = ThirdUmpire.grade(_tx(tmp_path, refused=True)).as_dict()
    assert d["verdict"] == "FAIL"
    assert any(c["name"] == "writes_inside_allowlist" and not c["passed"] for c in d["checks"])


def test_to_json_roundtrips_to_as_dict(tmp_path):
    report = ThirdUmpire.grade(_tx(tmp_path))
    assert json.loads(report.to_json()) == report.as_dict()


def test_cli_third_umpire_json_format(tmp_path, capsys):
    from boundary.cli import main
    rc = main(["third-umpire", str(_tx(tmp_path)), "--format", "json"])
    out = capsys.readouterr().out
    doc = json.loads(out)  # stdout must be valid JSON, nothing else
    assert doc["schema"].startswith("boundary.third-umpire/")
    assert doc["verdict"] in ("PASS", "WARN", "FAIL")
    assert rc in (0, 2)


def test_cli_third_umpire_defaults_to_markdown(tmp_path, capsys):
    from boundary.cli import main
    main(["third-umpire", str(_tx(tmp_path))])
    assert "# Third Umpire report" in capsys.readouterr().out
