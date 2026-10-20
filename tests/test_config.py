import json
from datetime import date
from pathlib import Path

import pytest

from exam_poller import Config, ConfigError

ROOT = Path(__file__).resolve().parent.parent


def good() -> dict:
    return json.loads((ROOT / "config.example.json").read_text())


def test_example_config_loads():
    cfg = Config.load(ROOT / "config.example.json")
    assert cfg.candidate_id == "123456789"
    assert cfg.birth_date == "12.09.1985"
    assert cfg.notify_after is None
    assert cfg.notify_before is None
    assert cfg.locations == ["Albisguetli"]
    assert cfg.smtp.host == "smtp.example.ch"
    assert cfg.smtp.port == 587
    assert cfg.smtp.sender == "me@example.ch"
    assert cfg.smtp.starttls is True
    assert cfg.poll_interval_minutes == 60
    assert cfg.max_weeks == 12


def test_missing_file_is_config_error(tmp_path):
    with pytest.raises(ConfigError, match="not found"):
        Config.load(tmp_path / "nope.json")


def test_invalid_json_is_config_error(tmp_path):
    p = tmp_path / "config.json"
    p.write_text("{not json")
    with pytest.raises(ConfigError, match="valid JSON"):
        Config.load(p)


def test_candidate_id_must_be_digits():
    raw = good()
    raw["credentials"]["candidate_id"] = "12AB"
    with pytest.raises(ConfigError, match="digits"):
        Config.from_dict(raw)


def test_birth_date_must_be_dd_mm_yyyy():
    raw = good()
    raw["credentials"]["birth_date"] = "1985-09-12"
    with pytest.raises(ConfigError, match="birth_date"):
        Config.from_dict(raw)


def test_notify_before_parsed():
    raw = good()
    raw["filter"]["notify_before"] = "31.12.2026"
    assert Config.from_dict(raw).notify_before == date(2026, 12, 31)


def test_notify_after_parsed():
    raw = good()
    raw["filter"]["notify_after"] = "01.11.2026"
    assert Config.from_dict(raw).notify_after == date(2026, 11, 1)


def test_notify_after_must_be_dd_mm_yyyy():
    raw = good()
    raw["filter"]["notify_after"] = "2026-11-01"
    with pytest.raises(ConfigError, match="notify_after"):
        Config.from_dict(raw)


def test_missing_smtp_section():
    raw = good()
    del raw["smtp"]
    with pytest.raises(ConfigError, match="smtp"):
        Config.from_dict(raw)


def test_defaults_when_optional_fields_absent():
    raw = good()
    del raw["poll_interval_minutes"]
    del raw["max_weeks"]
    del raw["filter"]
    cfg = Config.from_dict(raw)
    assert cfg.poll_interval_minutes == 60
    assert cfg.max_weeks == 12
    assert cfg.locations == []
    assert cfg.notify_after is None
    assert cfg.notify_before is None


def test_interval_must_be_positive():
    raw = good()
    raw["poll_interval_minutes"] = 0
    with pytest.raises(ConfigError, match="poll_interval_minutes"):
        Config.from_dict(raw)
