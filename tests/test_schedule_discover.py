from pathlib import Path

from boundary.schedule import ScheduleConfig


def test_discover_block_parses(tmp_path):
    y = tmp_path / "s.yaml"
    y.write_text("""
name: t
schedule: "weekly mon 08:00"
persona: vision
workspace: /ws
discover:
  source: fabricspecs_questions
  owner: mihirwagle
  max_tasks: 12
envelope:
  writable_paths: ["out-{date}.md"]
  max_writes: 1
task: do it
""")
    cfg = ScheduleConfig.load(y)
    assert cfg.discover["source"] == "fabricspecs_questions"
    assert cfg.discover["owner"] == "mihirwagle"
    assert cfg.discover["max_tasks"] == 12


def test_discover_absent_defaults_none(tmp_path):
    y = tmp_path / "s.yaml"
    y.write_text("""
name: t
schedule: "weekly mon 08:00"
persona: vision
workspace: /ws
envelope: {writable_paths: [o.md], max_writes: 1}
task: do it
""")
    assert ScheduleConfig.load(y).discover is None


def test_shipped_example_is_valid():
    p = Path(__file__).resolve().parents[1] / "examples/schedules/fabricspecs-open-questions-weekly.yaml"
    cfg = ScheduleConfig.load(p)
    assert cfg.discover and cfg.discover["source"] == "fabricspecs_questions"
    assert cfg.on_commit == "refuse"  # safe default for headless
    assert cfg.max_writes == 1
