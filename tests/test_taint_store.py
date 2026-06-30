from boundary.taint import TaintStore, _ledger_root


def test_ledger_root_honors_boundary_home(tmp_path, monkeypatch):
    monkeypatch.setenv("BOUNDARY_HOME", str(tmp_path / "bh"))
    assert _ledger_root() == (tmp_path / "bh" / "taint")


def test_roundtrip_sources_and_files(tmp_path, monkeypatch):
    monkeypatch.setenv("BOUNDARY_HOME", str(tmp_path / "bh"))
    ws = tmp_path / "ws"; ws.mkdir()
    s = TaintStore.load(ws)
    assert s.has_any() is False
    s.mark_source("http://evil.test")
    s.mark_file("notes/a.md")
    s2 = TaintStore.load(ws)
    assert s2.has_any() is True
    assert s2.is_tainted("notes/a.md") is True
    assert s2.is_tainted(ws / "notes/a.md") is True
    assert s2.is_tainted("notes/b.md") is False


def test_ledger_lives_outside_workspace(tmp_path, monkeypatch):
    monkeypatch.setenv("BOUNDARY_HOME", str(tmp_path / "bh"))
    ws = tmp_path / "ws"; ws.mkdir()
    TaintStore.load(ws).mark_file("x.md")
    assert not (ws / ".boundary").exists()
    assert list((tmp_path / "bh" / "taint").glob("*.json"))


def test_clear(tmp_path, monkeypatch):
    monkeypatch.setenv("BOUNDARY_HOME", str(tmp_path / "bh"))
    ws = tmp_path / "ws"; ws.mkdir()
    s = TaintStore.load(ws); s.mark_file("x.md")
    s.clear()
    assert TaintStore.load(ws).has_any() is False
