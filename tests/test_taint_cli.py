from boundary.cli import main
from boundary.taint import TaintStore


def test_taint_show_and_clear(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("BOUNDARY_HOME", str(tmp_path / "bh"))
    ws = tmp_path / "ws"; ws.mkdir()
    TaintStore.load(ws).mark_file("intel.md")
    assert main(["taint", "--show", str(ws)]) == 0
    assert "intel.md" in capsys.readouterr().out
    assert main(["taint", "--clear", str(ws)]) == 0
    assert TaintStore.load(ws).has_any() is False
