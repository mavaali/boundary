from boundary.schedule import ScheduleConfig


def test_schedule_load_parses_driver_and_egress(tmp_path):
    y = tmp_path / "s.yaml"
    y.write_text(
        "name: x\nschedule: hourly\npersona: banner\nworkspace: /tmp/ws\n"
        "task: do\nsandbox_driver: srt\negress_allow: [api.example.com]\n",
        encoding="utf-8")
    cfg = ScheduleConfig.load(y)
    assert cfg.sandbox_driver == "srt"
    assert cfg.egress_allowlist == ["api.example.com"]


def test_schedule_defaults_seatbelt(tmp_path):
    y = tmp_path / "s.yaml"
    y.write_text("name: x\nschedule: hourly\npersona: banner\nworkspace: /tmp/ws\ntask: do\n",
                 encoding="utf-8")
    cfg = ScheduleConfig.load(y)
    assert cfg.sandbox_driver == "seatbelt"
    assert cfg.egress_allowlist == []
