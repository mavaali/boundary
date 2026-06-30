from boundary.discover import discover, run_discovery, scan_markers


def _ws(tmp_path):
    (tmp_path / "a.md").write_text("intro\nBOUNDARY-TASK: write the changelog\nmore\n")
    (tmp_path / "b.py").write_text("# BOUNDARY-TASK: add retry test\nx=1\n")
    (tmp_path / "skip.md").write_text("nothing here\n")
    d = tmp_path / ".venv"; d.mkdir(); (d / "c.md").write_text("BOUNDARY-TASK: ignored\n")
    return tmp_path


def test_scan_finds_markers_skips_vendor(tmp_path):
    tasks = scan_markers(_ws(tmp_path))
    titles = {t.title for t in tasks}
    assert "write the changelog" in titles
    assert "add retry test" in titles
    assert all("ignored" not in t.detail for t in tasks)


def test_origin_is_file_line(tmp_path):
    t = scan_markers(_ws(tmp_path))[0]
    assert ":" in t.origin and t.origin.endswith(":2")


def test_max_tasks_cap(tmp_path):
    for i in range(10):
        (tmp_path / f"f{i}.md").write_text("BOUNDARY-TASK: t\n")
    assert len(scan_markers(tmp_path, max_tasks=3)) == 3


def test_unknown_source_raises(tmp_path):
    try:
        discover(tmp_path, source="nope")
        assert False
    except ValueError as e:
        assert "unknown source" in str(e)


def test_run_discovery_dryrun_vs_fanout(tmp_path):
    _ws(tmp_path)
    r = run_discovery(tmp_path)
    assert r.dispatched == [] and len(r.tasks) == 2
    seen = []
    r2 = run_discovery(tmp_path, dispatch_fn=lambda t: seen.append(t.title) or {"ok": True})
    assert len(r2.dispatched) == 2 and len(seen) == 2
