# Exam Slot Poller Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** A single Python script that logs into the Strassenverkehrsamt Zürich portal, pages through free driving-exam slots week by week, and emails the user when a new slot matches a location and date filter.

**Architecture:** One file `exam_poller.py` with five small units: `Config` (validated JSON), `Portal` (a `requests.Session` wrapper around five JSON endpoints), `SlotFilter` (pure matching), `State` (dedupe file), `Notifier` plus `Alerts` (SMTP). `run_cycle()` composes them; `main()` adds the CLI and the polling loop. Parsers for the portal's responses are written from the bundle analysis and then verified against a real `--discover` capture.

**Tech Stack:** Python 3.14, `requests`, stdlib `smtplib`, `pytest` for tests, `zoneinfo` for Europe/Zurich.

**Spec:** `docs/superpowers/specs/2026-10-16-exam-slot-poller-design.md`

## Global Constraints

- Backend base URL `https://portal.stva.zh.ch/ecari-dispoweb`; every request carries a full Chrome `User-Agent`, `Referer: https://portal.stva.zh.ch/ecari-dispoweb/ui/app/init/`, `Origin: https://portal.stva.zh.ch`, `Accept: application/json`, `Content-Type: application/json`.
- Login body is exactly `{"noRegistre": "<digits>", "dateNaissance": "dd.mm.yyyy", "captcha": ""}` posted to `/login/loginConduitePrive/de`.
- Credentials, SMTP password, `state.json` and `discovery/` never enter git.
- Default poll interval 60 minutes with up to 10 % jitter; backoff on errors doubles up to 6 hours.
- Location matching is case-insensitive substring after `ä/ö/ü/ß → ae/oe/ue/ss` on both sides.
- `notify_before` is inclusive. Empty means no cutoff, bounded by `max_weeks` (default 12).
- Operational emails fire once per condition and re-arm only after the condition clears.
- Tests never touch the network or a real SMTP server.

## Review Focus

- A `jour` value arriving as epoch milliseconds or as UTC midnight (`...T22:00:00.000Z` for a Zurich date) must map to the Zurich calendar date. Test in Task 6.
- A location name with a combining diaeresis (`u` + U+0308) must still match `Albisguetli`. Test in Task 2.
- A corrupt or truncated `state.json` must be treated as empty, not crash the loop. Test in Task 3.
- An SMTP failure while sending a slot email must leave `state.json` untouched so the slots re-alert next cycle. Test in Task 7.
- A 401 on a mid-cycle call after the session expired must trigger exactly one re-login and succeed; a second 401 must surface as an error, not loop. Test in Task 5.

---

### Task 1: Scaffolding and config loading

**Files:**
- Create: `requirements.txt`, `.gitignore`, `config.example.json`, `exam_poller.py`, `tests/__init__.py`, `tests/test_config.py`

**Interfaces:**
- Produces: `ConfigError(Exception)`, `SmtpConfig` dataclass (`host, port, user, password, sender, to, starttls`), `Config` dataclass (`candidate_id: str, birth_date: str, notify_before: date | None, locations: list[str], smtp: SmtpConfig, poll_interval_minutes: int, max_weeks: int`), `Config.load(path) -> Config`, `Config.from_dict(raw) -> Config`, `DATE_FMT = "%d.%m.%Y"`, `parse_date(s) -> date`.

- [ ] **Step 1: Create the environment and support files**

```bash
cd /Users/chenyilu/work/drive_script_repo
python3 -m venv .venv && .venv/bin/pip install -q requests pytest
```

`requirements.txt`:
```
requests>=2.32
```

`.gitignore`:
```
config.json
state.json
discovery/
.venv/
__pycache__/
.pytest_cache/
```

`config.example.json`:
```json
{
  "credentials": {"candidate_id": "123456789", "birth_date": "12.09.1985"},
  "filter": {"notify_before": "", "locations": ["Albisguetli"]},
  "smtp": {
    "host": "smtp.example.ch",
    "port": 587,
    "user": "me@example.ch",
    "password": "app-password",
    "from": "me@example.ch",
    "to": "me@example.ch",
    "starttls": true
  },
  "poll_interval_minutes": 60,
  "max_weeks": 12
}
```

`tests/__init__.py`: empty file.

- [ ] **Step 2: Write the failing tests**

`tests/test_config.py`:
```python
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
    assert cfg.notify_before is None


def test_interval_must_be_positive():
    raw = good()
    raw["poll_interval_minutes"] = 0
    with pytest.raises(ConfigError, match="poll_interval_minutes"):
        Config.from_dict(raw)
```

- [ ] **Step 3: Run tests to verify they fail**

Run: `.venv/bin/pytest tests/test_config.py -q`
Expected: ImportError, `exam_poller` has no `Config`.

- [ ] **Step 4: Write the implementation**

`exam_poller.py` (first section):
```python
#!/usr/bin/env python3
"""Poll the Strassenverkehrsamt Zürich portal for free driving-exam slots and
email when a new slot matches the configured location and date filter.

Usage:
    python exam_poller.py                 # poll forever
    python exam_poller.py --once          # one cycle, for cron
    python exam_poller.py --dry-run       # print emails instead of sending
    python exam_poller.py --discover      # log in once, save raw responses
"""
from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path

log = logging.getLogger("exam_poller")

DATE_FMT = "%d.%m.%Y"


class ConfigError(Exception):
    """The config file is missing, malformed, or has a bad value."""


def parse_date(text: str) -> date:
    return datetime.strptime(text, DATE_FMT).date()


@dataclass
class SmtpConfig:
    host: str
    port: int
    user: str
    password: str
    sender: str
    to: str
    starttls: bool = True


@dataclass
class Config:
    candidate_id: str
    birth_date: str
    notify_before: date | None
    locations: list[str]
    smtp: SmtpConfig
    poll_interval_minutes: int = 60
    max_weeks: int = 12

    @classmethod
    def load(cls, path: Path | str) -> "Config":
        path = Path(path)
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            raise ConfigError(f"config file not found: {path}") from None
        except json.JSONDecodeError as exc:
            raise ConfigError(f"config is not valid JSON: {exc}") from None
        return cls.from_dict(raw)

    @classmethod
    def from_dict(cls, raw: dict) -> "Config":
        cred = _section(raw, "credentials")
        candidate_id = str(_require(cred, "candidate_id", "credentials"))
        if not candidate_id.isdigit():
            raise ConfigError("credentials.candidate_id must contain digits only")
        birth_date = str(_require(cred, "birth_date", "credentials"))
        _check_date(birth_date, "credentials.birth_date")

        filt = raw.get("filter") or {}
        if not isinstance(filt, dict):
            raise ConfigError("filter must be an object")
        cutoff_text = str(filt.get("notify_before") or "").strip()
        notify_before = None
        if cutoff_text:
            _check_date(cutoff_text, "filter.notify_before")
            notify_before = parse_date(cutoff_text)
        locations = filt.get("locations") or []
        if not isinstance(locations, list) or not all(isinstance(x, str) for x in locations):
            raise ConfigError("filter.locations must be a list of strings")

        smtp_raw = _section(raw, "smtp")
        smtp = SmtpConfig(
            host=str(_require(smtp_raw, "host", "smtp")),
            port=_int(_require(smtp_raw, "port", "smtp"), "smtp.port"),
            user=str(smtp_raw.get("user") or ""),
            password=str(smtp_raw.get("password") or ""),
            sender=str(_require(smtp_raw, "from", "smtp")),
            to=str(_require(smtp_raw, "to", "smtp")),
            starttls=bool(smtp_raw.get("starttls", True)),
        )

        interval = _int(raw.get("poll_interval_minutes", 60), "poll_interval_minutes")
        if interval < 1:
            raise ConfigError("poll_interval_minutes must be at least 1")
        max_weeks = _int(raw.get("max_weeks", 12), "max_weeks")
        if max_weeks < 1:
            raise ConfigError("max_weeks must be at least 1")

        return cls(
            candidate_id=candidate_id,
            birth_date=birth_date,
            notify_before=notify_before,
            locations=locations,
            smtp=smtp,
            poll_interval_minutes=interval,
            max_weeks=max_weeks,
        )


def _section(raw: dict, name: str) -> dict:
    value = raw.get(name)
    if not isinstance(value, dict):
        raise ConfigError(f"missing or invalid '{name}' section")
    return value


def _require(section: dict, key: str, section_name: str):
    value = section.get(key)
    if value is None or value == "":
        raise ConfigError(f"missing '{section_name}.{key}'")
    return value


def _int(value, name: str) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        raise ConfigError(f"{name} must be an integer") from None


def _check_date(text: str, name: str) -> None:
    try:
        parse_date(text)
    except ValueError:
        raise ConfigError(f"{name} must be dd.mm.yyyy, got {text!r}") from None
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `.venv/bin/pytest tests/test_config.py -q`
Expected: 9 passed.

- [ ] **Step 6: Commit**

```bash
git add requirements.txt .gitignore config.example.json exam_poller.py tests/__init__.py tests/test_config.py
git commit -m "Add config loading and project scaffolding for exam poller"
```

---

### Task 2: Slot model and filter

**Files:**
- Modify: `exam_poller.py` (append after config section)
- Create: `tests/test_filter.py`

**Interfaces:**
- Produces: `normalise(text: str) -> str`, `Slot` frozen dataclass (`date: date, time: str, location: str`, property `key -> str`, orderable by date then time then location), `SlotFilter(notify_before: date | None, locations: list[str])` with `location_matches(name: str) -> bool`, `slot_matches(slot: Slot) -> bool`, `apply(slots: Iterable[Slot]) -> list[Slot]` (sorted).

- [ ] **Step 1: Write the failing tests**

`tests/test_filter.py`:
```python
from datetime import date

from exam_poller import Slot, SlotFilter, normalise


def slot(d: date, time: str = "08:00", location: str = "Zürich-Albisgüetli") -> Slot:
    return Slot(date=d, time=time, location=location)


def test_normalise_folds_case_and_umlauts():
    assert normalise("Albisgüetli") == "albisguetli"
    assert normalise("ZÜRICH Öl Straße") == "zuerich oel strasse"


def test_normalise_handles_combining_diaeresis():
    assert normalise("Albisgüetli") == "albisguetli"


def test_location_substring_match_ignores_umlaut_and_case():
    f = SlotFilter(None, ["albisguetli"])
    assert f.location_matches("Zürich-Albisgüetli")
    assert f.location_matches("ALBISGÜETLI")
    assert not f.location_matches("Winterthur")


def test_empty_location_list_matches_everything():
    assert SlotFilter(None, []).location_matches("Winterthur")


def test_cutoff_is_inclusive():
    f = SlotFilter(date(2026, 11, 30), [])
    assert f.slot_matches(slot(date(2026, 11, 30)))
    assert not f.slot_matches(slot(date(2026, 12, 1)))


def test_no_cutoff_passes_any_date():
    assert SlotFilter(None, []).slot_matches(slot(date(2030, 1, 1)))


def test_apply_filters_and_sorts_by_date_then_time():
    f = SlotFilter(date(2026, 11, 30), ["Albisguetli"])
    slots = [
        slot(date(2026, 11, 20), "10:30"),
        slot(date(2026, 11, 20), "08:00"),
        slot(date(2026, 11, 10), "14:00"),
        slot(date(2026, 12, 5), "08:00"),
        slot(date(2026, 11, 15), "08:00", location="Winterthur"),
    ]
    out = f.apply(slots)
    assert [(s.date, s.time) for s in out] == [
        (date(2026, 11, 10), "14:00"),
        (date(2026, 11, 20), "08:00"),
        (date(2026, 11, 20), "10:30"),
    ]


def test_key_is_stable_and_unique_per_slot():
    a = slot(date(2026, 11, 20), "08:00")
    b = slot(date(2026, 11, 20), "08:30")
    assert a.key == "Zürich-Albisgüetli|2026-11-20|08:00"
    assert a.key != b.key
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv/bin/pytest tests/test_filter.py -q`
Expected: ImportError on `Slot`.

- [ ] **Step 3: Write the implementation**

Append to `exam_poller.py` (add `import unicodedata` and `from typing import Iterable` to the imports at the top):
```python
_UMLAUTS = str.maketrans({"ä": "ae", "ö": "oe", "ü": "ue", "ß": "ss"})


def normalise(text: str) -> str:
    """Lowercase, fold German umlauts to ASCII pairs, so 'Albisguetli' == 'Albisgüetli'."""
    return unicodedata.normalize("NFC", text).casefold().translate(_UMLAUTS)


@dataclass(frozen=True, order=True)
class Slot:
    date: date
    time: str
    location: str

    @property
    def key(self) -> str:
        return f"{self.location}|{self.date.isoformat()}|{self.time}"


class SlotFilter:
    def __init__(self, notify_before: date | None, locations: list[str]):
        self.notify_before = notify_before
        self.locations = [normalise(name) for name in locations if name.strip()]

    def location_matches(self, name: str) -> bool:
        if not self.locations:
            return True
        folded = normalise(name)
        return any(wanted in folded for wanted in self.locations)

    def slot_matches(self, slot: Slot) -> bool:
        if self.notify_before is not None and slot.date > self.notify_before:
            return False
        return self.location_matches(slot.location)

    def apply(self, slots: Iterable[Slot]) -> list[Slot]:
        return sorted(s for s in slots if self.slot_matches(s))
```

Note: `casefold()` turns `ß` into `ss` already and leaves `ä/ö/ü` alone, so the translate afterwards handles the umlauts.

- [ ] **Step 4: Run tests to verify they pass**

Run: `.venv/bin/pytest tests/test_filter.py -q`
Expected: 8 passed.

- [ ] **Step 5: Commit**

```bash
git add exam_poller.py tests/test_filter.py
git commit -m "Add slot model and umlaut-aware location/date filter"
```

---

### Task 3: Dedupe state

**Files:**
- Modify: `exam_poller.py` (append)
- Create: `tests/test_state.py`

**Interfaces:**
- Consumes: `Slot`.
- Produces: `State(path: Path)` with `load() -> set[str]`, `save(slots: Iterable[Slot]) -> None`, `new_slots(slots: list[Slot]) -> list[Slot]`.

- [ ] **Step 1: Write the failing tests**

`tests/test_state.py`:
```python
import json
from datetime import date

from exam_poller import Slot, State


def s(day: int, time: str = "08:00") -> Slot:
    return Slot(date=date(2026, 11, day), time=time, location="Albisgüetli")


def test_fresh_state_reports_everything_new(tmp_path):
    st = State(tmp_path / "state.json")
    assert st.new_slots([s(1), s(2)]) == [s(1), s(2)]


def test_saved_slots_are_not_new_next_time(tmp_path):
    st = State(tmp_path / "state.json")
    st.save([s(1), s(2)])
    assert st.new_slots([s(1), s(2), s(3)]) == [s(3)]


def test_slot_that_disappears_and_returns_is_new_again(tmp_path):
    st = State(tmp_path / "state.json")
    st.save([s(1)])
    st.save([])  # slot vanished this cycle
    assert st.new_slots([s(1)]) == [s(1)]


def test_corrupt_state_file_is_treated_as_empty(tmp_path):
    p = tmp_path / "state.json"
    p.write_text("{truncated")
    assert State(p).load() == set()
    p.write_text("[1, 2, 3]")
    assert State(p).load() == set()


def test_save_writes_sorted_keys_and_timestamp(tmp_path):
    p = tmp_path / "state.json"
    State(p).save([s(2), s(1)])
    data = json.loads(p.read_text())
    assert data["notified"] == [s(1).key, s(2).key]
    assert "updated" in data
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv/bin/pytest tests/test_state.py -q`
Expected: ImportError on `State`.

- [ ] **Step 3: Write the implementation**

Append to `exam_poller.py`:
```python
class State:
    """Keys of the slots emailed in the previous cycle, kept in a small JSON file."""

    def __init__(self, path: Path | str):
        self.path = Path(path)

    def load(self) -> set[str]:
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return set()
        except json.JSONDecodeError:
            log.warning("state file %s is not valid JSON, starting fresh", self.path)
            return set()
        if not isinstance(data, dict) or not isinstance(data.get("notified"), list):
            log.warning("state file %s has unexpected shape, starting fresh", self.path)
            return set()
        return {str(k) for k in data["notified"]}

    def save(self, slots: Iterable[Slot]) -> None:
        payload = {
            "notified": sorted(s.key for s in slots),
            "updated": datetime.now().isoformat(timespec="seconds"),
        }
        self.path.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")

    def new_slots(self, slots: list[Slot]) -> list[Slot]:
        seen = self.load()
        return [s for s in slots if s.key not in seen]
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `.venv/bin/pytest tests/test_state.py -q`
Expected: 5 passed.

- [ ] **Step 5: Commit**

```bash
git add exam_poller.py tests/test_state.py
git commit -m "Add dedupe state file for notified slots"
```

---

### Task 4: Email notifier and operational alerts

**Files:**
- Modify: `exam_poller.py` (append)
- Create: `tests/test_notifier.py`

**Interfaces:**
- Consumes: `SmtpConfig`, `Slot`, `DATE_FMT`.
- Produces: `PORTAL_UI_URL`, `format_slot_email(slots) -> tuple[str, str]`, `Notifier(smtp: SmtpConfig, dry_run=False)` with `send(subject, body) -> None` (raises `smtplib.SMTPException` or `OSError` on failure) and `send_slots(slots) -> None`; `Alerts(notifier)` with `raise_(name: str, body: str) -> None` and `clear(name: str) -> None`.

- [ ] **Step 1: Write the failing tests**

`tests/test_notifier.py`:
```python
import smtplib
from datetime import date

import pytest

import exam_poller
from exam_poller import Alerts, Notifier, Slot, SmtpConfig, format_slot_email


def cfg(starttls=True) -> SmtpConfig:
    return SmtpConfig(host="smtp.test", port=587, user="u", password="p",
                      sender="from@test", to="to@test", starttls=starttls)


class FakeSMTP:
    instances: list["FakeSMTP"] = []

    def __init__(self, host, port, timeout=None):
        self.host, self.port = host, port
        self.calls: list[str] = []
        self.sent = []
        FakeSMTP.instances.append(self)

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def ehlo(self):
        self.calls.append("ehlo")

    def starttls(self):
        self.calls.append("starttls")

    def login(self, user, password):
        self.calls.append(f"login:{user}")

    def send_message(self, msg):
        self.sent.append(msg)


@pytest.fixture(autouse=True)
def fake_smtp(monkeypatch):
    FakeSMTP.instances = []
    monkeypatch.setattr(smtplib, "SMTP", FakeSMTP)
    monkeypatch.setattr(smtplib, "SMTP_SSL", FakeSMTP)
    return FakeSMTP


def test_format_slot_email_subject_and_body():
    slots = [
        Slot(date(2026, 11, 20), "10:30", "Zürich-Albisgüetli"),
        Slot(date(2026, 11, 10), "08:00", "Zürich-Albisgüetli"),
    ]
    subject, body = format_slot_email(slots)
    assert subject == "Exam slots: 2 new, earliest 10.11.2026"
    assert "Tue 10.11.2026  08:00  Zürich-Albisgüetli" in body
    assert body.index("10.11.2026") < body.index("20.11.2026")
    assert exam_poller.PORTAL_UI_URL in body


def test_send_uses_starttls_and_login():
    Notifier(cfg()).send("subj", "body")
    smtp = FakeSMTP.instances[0]
    assert (smtp.host, smtp.port) == ("smtp.test", 587)
    assert smtp.calls == ["ehlo", "starttls", "ehlo", "login:u"]
    msg = smtp.sent[0]
    assert msg["Subject"] == "subj"
    assert msg["From"] == "from@test"
    assert msg["To"] == "to@test"
    assert msg.get_content().strip() == "body"


def test_send_without_user_skips_login():
    c = cfg()
    c.user = ""
    Notifier(c).send("s", "b")
    assert "login:u" not in FakeSMTP.instances[0].calls


def test_send_ssl_when_starttls_false():
    Notifier(cfg(starttls=False)).send("s", "b")
    assert "starttls" not in FakeSMTP.instances[0].calls


def test_dry_run_prints_instead_of_sending(capsys):
    Notifier(cfg(), dry_run=True).send("Hello", "World")
    assert FakeSMTP.instances == []
    out = capsys.readouterr().out
    assert "Hello" in out and "World" in out


def test_alerts_send_once_until_cleared():
    n = Notifier(cfg())
    a = Alerts(n)
    a.raise_("captcha", "captcha is on")
    a.raise_("captcha", "captcha is on")
    assert len(FakeSMTP.instances) == 1
    assert FakeSMTP.instances[0].sent[0]["Subject"] == "Exam poller problem: captcha"
    a.clear("captcha")
    a.raise_("captcha", "captcha is on again")
    assert len(FakeSMTP.instances) == 2


def test_alerts_swallow_smtp_errors_and_retry_next_time(monkeypatch):
    class Boom(FakeSMTP):
        def send_message(self, msg):
            raise smtplib.SMTPException("down")

    monkeypatch.setattr(smtplib, "SMTP", Boom)
    a = Alerts(Notifier(cfg()))
    a.raise_("login", "rejected")  # must not raise
    assert "login" not in a.active
    monkeypatch.setattr(smtplib, "SMTP", FakeSMTP)
    a.raise_("login", "rejected")
    assert "login" in a.active
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv/bin/pytest tests/test_notifier.py -q`
Expected: ImportError on `Notifier`.

- [ ] **Step 3: Write the implementation**

Append to `exam_poller.py` (add `import smtplib` and `from email.message import EmailMessage` to the imports):
```python
BASE_URL = "https://portal.stva.zh.ch/ecari-dispoweb"
PORTAL_UI_URL = f"{BASE_URL}/ui/app/init/"


def format_slot_email(slots: Iterable[Slot]) -> tuple[str, str]:
    ordered = sorted(slots)
    earliest = ordered[0].date.strftime(DATE_FMT)
    subject = f"Exam slots: {len(ordered)} new, earliest {earliest}"
    lines = [f"{s.date.strftime('%a %d.%m.%Y')}  {s.time}  {s.location}" for s in ordered]
    body = "New free exam slots:\n\n" + "\n".join(lines) + f"\n\nBook at: {PORTAL_UI_URL}\n"
    return subject, body


class Notifier:
    def __init__(self, smtp: SmtpConfig, dry_run: bool = False):
        self.smtp = smtp
        self.dry_run = dry_run

    def send(self, subject: str, body: str) -> None:
        msg = EmailMessage()
        msg["Subject"] = subject
        msg["From"] = self.smtp.sender
        msg["To"] = self.smtp.to
        msg.set_content(body)
        if self.dry_run:
            print(f"--- DRY RUN email ---\nSubject: {subject}\n\n{body}", flush=True)
            return
        if self.smtp.starttls:
            with smtplib.SMTP(self.smtp.host, self.smtp.port, timeout=30) as conn:
                conn.ehlo()
                conn.starttls()
                conn.ehlo()
                if self.smtp.user:
                    conn.login(self.smtp.user, self.smtp.password)
                conn.send_message(msg)
        else:
            with smtplib.SMTP_SSL(self.smtp.host, self.smtp.port, timeout=30) as conn:
                if self.smtp.user:
                    conn.login(self.smtp.user, self.smtp.password)
                conn.send_message(msg)
        log.info("email sent: %s", subject)

    def send_slots(self, slots: Iterable[Slot]) -> None:
        subject, body = format_slot_email(slots)
        self.send(subject, body)


class Alerts:
    """Operational emails, sent once per condition until that condition clears."""

    def __init__(self, notifier: Notifier):
        self.notifier = notifier
        self.active: set[str] = set()

    def raise_(self, name: str, body: str) -> None:
        if name in self.active:
            return
        log.warning("problem (%s): %s", name, body)
        try:
            self.notifier.send(f"Exam poller problem: {name}", body)
        except (smtplib.SMTPException, OSError) as exc:
            log.error("could not send alert email for %s: %s", name, exc)
            return
        self.active.add(name)

    def clear(self, name: str) -> None:
        if name in self.active:
            log.info("problem cleared: %s", name)
        self.active.discard(name)
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `.venv/bin/pytest tests/test_notifier.py -q`
Expected: 7 passed.

- [ ] **Step 5: Commit**

```bash
git add exam_poller.py tests/test_notifier.py
git commit -m "Add SMTP notifier and once-per-condition alerts"
```

---

### Task 5: Portal client with login, session recovery and discovery mode

**Files:**
- Modify: `exam_poller.py` (append)
- Create: `tests/test_portal.py`, `tests/fakes.py`

**Interfaces:**
- Consumes: `BASE_URL`, `PORTAL_UI_URL`.
- Produces: `HEADERS` dict, `PortalError(Exception)`, `LoginRejected(PortalError)` with `.status: int`, `type_body(cat: dict, **extra) -> dict`, `portal_datetime(day: date) -> str`, `Portal(candidate_id, birth_date, session=None, lang="de", timeout=30)` with `logged_in: bool`, `check_params() -> tuple[bool, bool]` (captcha_on, blocked), `login() -> dict`, `get(path) -> Any`, `post(path, body) -> Any`, `categories() -> list[dict]`, `locations(cat: dict) -> list[dict]`, `free_week(cat: dict, id_lieu, base: date) -> dict`, `discover(out_dir: Path) -> list[Path]`.

- [ ] **Step 1: Write the fake session and failing tests**

`tests/fakes.py`:
```python
"""A stand-in for requests.Session that serves canned responses per (method, path)."""
from collections import deque


class FakeResponse:
    def __init__(self, status: int, payload=None, text: str = ""):
        self.status_code = status
        self._payload = payload
        self.text = text if text else ("" if payload is None else "json")

    def json(self):
        if self._payload is None:
            raise ValueError("no json")
        return self._payload


class FakeSession:
    def __init__(self, routes: dict):
        # routes: {(method, path): [FakeResponse, ...]} served in order; last one repeats.
        self.routes = {k: deque(v) for k, v in routes.items()}
        self.calls: list[tuple[str, str, dict | None]] = []
        self.headers: dict = {}

    def request(self, method, url, json=None, timeout=None):
        path = url.split("/ecari-dispoweb", 1)[1]
        self.calls.append((method, path, json))
        queue = self.routes.get((method, path))
        if not queue:
            raise AssertionError(f"unexpected call {method} {path}")
        resp = queue[0] if len(queue) == 1 else queue.popleft()
        return resp

    def paths(self):
        return [(m, p) for m, p, _ in self.calls]
```

`tests/test_portal.py`:
```python
import json
from datetime import date

import pytest

from exam_poller import HEADERS, LoginRejected, Portal, PortalError, portal_datetime, type_body
from tests.fakes import FakeResponse, FakeSession

LOGIN = ("POST", "/login/loginConduitePrive/de")
PARAM = ("GET", "/param/load")
CATS = ("GET", "/personne/listCatPossible")
LIEUX = ("POST", "/lieu-examen/listeLieu")
WEEK = ("POST", "/lieu-examen/listeJourHeureLibre")

CAT = {"idGroupeExamen": 1, "categorie": "B", "idTypeExamen": 2, "idGenreExamen": 3, "rendezVous": 99}
LOC = {"idLieuExamen": 7, "nom": "Zürich-Albisgüetli"}
PARAMS_OFF = {"captchaParam": {"conduitePublic": False}, "blocking": {"conduitePublic": False}}


def make(routes) -> tuple[Portal, FakeSession]:
    sess = FakeSession(routes)
    return Portal("123456789", "12.09.1985", session=sess), sess


def test_headers_look_like_a_browser():
    p, sess = make({})
    assert "Chrome" in sess.headers["User-Agent"]
    assert sess.headers["Referer"].startswith("https://portal.stva.zh.ch/ecari-dispoweb/ui/app/init/")
    assert sess.headers["Origin"] == "https://portal.stva.zh.ch"
    assert sess.headers["Accept"] == "application/json"
    assert HEADERS["Content-Type"] == "application/json"


def test_check_params_reads_flags():
    p, _ = make({PARAM: [FakeResponse(200, {"captchaParam": {"conduitePublic": True},
                                           "blocking": {"conduitePublic": False}})]})
    assert p.check_params() == (True, False)


def test_login_posts_expected_body_and_sets_logged_in():
    p, sess = make({LOGIN: [FakeResponse(200, {"personneDispo": {"nom": "X"}, "examTypes": []})]})
    data = p.login()
    assert p.logged_in is True
    assert data["personneDispo"] == {"nom": "X"}
    method, path, body = sess.calls[0]
    assert (method, path) == LOGIN
    assert body == {"noRegistre": "123456789", "dateNaissance": "12.09.1985", "captcha": ""}


@pytest.mark.parametrize("status", [401, 403])
def test_login_rejected(status):
    p, _ = make({LOGIN: [FakeResponse(status, {"personneDispo": None})]})
    with pytest.raises(LoginRejected) as info:
        p.login()
    assert info.value.status == status
    assert p.logged_in is False


def test_non_json_response_is_portal_error():
    p, _ = make({PARAM: [FakeResponse(200, None, text="<html>Request Rejected")]})
    with pytest.raises(PortalError, match="non-JSON"):
        p.check_params()


def test_expired_session_relogins_once_and_retries():
    p, sess = make({
        LOGIN: [FakeResponse(200, {"personneDispo": {}})],
        CATS: [FakeResponse(401, {}), FakeResponse(200, [CAT])],
    })
    p.login()
    assert p.categories() == [CAT]
    assert sess.paths() == [LOGIN, CATS, LOGIN, CATS]


def test_second_401_surfaces_as_error_not_loop():
    p, sess = make({
        LOGIN: [FakeResponse(200, {"personneDispo": {}})],
        CATS: [FakeResponse(401, {})],
    })
    p.login()
    with pytest.raises(PortalError, match="401"):
        p.categories()
    assert sess.paths().count(LOGIN) == 2


def test_type_body_maps_category_fields():
    assert type_body(CAT) == {
        "typeCategorieLieuDispo": {"idGroupeExamen": 1, "idCategorie": "B", "idTypeExamen": 2, "idGenreExamen": 3},
        "rendezVous": 99,
    }
    assert type_body(CAT, idLieuExamen=7)["typeCategorieLieuDispo"]["idLieuExamen"] == 7


def test_portal_datetime_mimics_browser_utc_json():
    # Noon Zurich on 2026-10-20 (CEST, UTC+2) is 10:00 UTC.
    assert portal_datetime(date(2026, 10, 20)) == "2026-10-20T10:00:00.000Z"
    # Winter: CET, UTC+1.
    assert portal_datetime(date(2026, 12, 20)) == "2026-12-20T11:00:00.000Z"


def test_locations_and_free_week_send_expected_bodies():
    p, sess = make({
        LOGIN: [FakeResponse(200, {})],
        LIEUX: [FakeResponse(200, [LOC])],
        WEEK: [FakeResponse(200, {"listeJourHeureLibreDispo": [], "nextWeek": False})],
    })
    p.login()
    assert p.locations(CAT) == [LOC]
    assert sess.calls[-1][2] == type_body(CAT)
    p.free_week(CAT, 7, date(2026, 10, 20))
    body = sess.calls[-1][2]
    assert body["typeCategorieLieuDispo"]["idLieuExamen"] == 7
    assert body["typeCategorieLieuDispo"]["dateBase"] == "2026-10-20T10:00:00.000Z"
    assert body["rendezVous"] == 99


def test_discover_writes_each_response(tmp_path):
    p, _ = make({
        PARAM: [FakeResponse(200, PARAMS_OFF)],
        LOGIN: [FakeResponse(200, {"personneDispo": {"nom": "X"}})],
        CATS: [FakeResponse(200, [CAT])],
        LIEUX: [FakeResponse(200, [LOC])],
        WEEK: [FakeResponse(200, {"listeJourHeureLibreDispo": [], "nextWeek": False})],
    })
    files = p.discover(tmp_path)
    names = [f.name for f in files]
    assert names == ["0_param.json", "1_login.json", "2_categories.json",
                     "3_locations_cat0.json", "4_week_cat0_loc0.json"]
    assert json.loads((tmp_path / "2_categories.json").read_text()) == [CAT]
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv/bin/pytest tests/test_portal.py -q`
Expected: ImportError on `Portal`.

- [ ] **Step 3: Write the implementation**

Append to `exam_poller.py` (add `import requests`, `from datetime import time, timedelta, timezone`, `from typing import Any`, `from zoneinfo import ZoneInfo` to the imports; merge with the existing `datetime` import line):
```python
ZURICH = ZoneInfo("Europe/Zurich")

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/129.0.0.0 Safari/537.36"
    ),
    "Referer": PORTAL_UI_URL,
    "Origin": "https://portal.stva.zh.ch",
    "Accept": "application/json",
    "Content-Type": "application/json",
    "Accept-Language": "de-CH,de;q=0.9,en;q=0.8",
}


class PortalError(Exception):
    """The portal answered with an error status or something that is not JSON."""


class LoginRejected(PortalError):
    def __init__(self, status: int):
        super().__init__(f"login rejected with HTTP {status}")
        self.status = status


def type_body(cat: dict, **extra: Any) -> dict:
    """Request body shared by the location and free-slot endpoints, built from one
    entry of /personne/listCatPossible."""
    return {
        "typeCategorieLieuDispo": {
            "idGroupeExamen": cat.get("idGroupeExamen"),
            "idCategorie": cat.get("categorie"),
            "idTypeExamen": cat.get("idTypeExamen"),
            "idGenreExamen": cat.get("idGenreExamen"),
            **extra,
        },
        "rendezVous": cat.get("rendezVous"),
    }


def portal_datetime(day: date) -> str:
    """Serialise a date the way the browser does: noon Zurich time as UTC ISO with Z."""
    local = datetime.combine(day, time(12, 0), tzinfo=ZURICH)
    return local.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000Z")


class Portal:
    def __init__(self, candidate_id: str, birth_date: str, session=None,
                 lang: str = "de", timeout: int = 30):
        self.candidate_id = candidate_id
        self.birth_date = birth_date
        self.lang = lang
        self.timeout = timeout
        self.session = session if session is not None else requests.Session()
        self.session.headers.update(HEADERS)
        self.logged_in = False

    # -- low level -----------------------------------------------------------

    def _raw(self, method: str, path: str, body: dict | None = None):
        return self.session.request(method, BASE_URL + path, json=body, timeout=self.timeout)

    def _json(self, resp, method: str, path: str) -> Any:
        try:
            return resp.json()
        except ValueError:
            raise PortalError(f"{method} {path}: non-JSON response ({resp.text[:120]!r})") from None

    def _call(self, method: str, path: str, body: dict | None = None, retry_login: bool = True) -> Any:
        resp = self._raw(method, path, body)
        if resp.status_code == 401 and retry_login and self.logged_in:
            log.info("session expired, logging in again")
            self.login()
            return self._call(method, path, body, retry_login=False)
        if resp.status_code >= 400:
            raise PortalError(f"{method} {path} -> HTTP {resp.status_code}: {resp.text[:200]}")
        return self._json(resp, method, path)

    def get(self, path: str) -> Any:
        return self._call("GET", path)

    def post(self, path: str, body: dict) -> Any:
        return self._call("POST", path, body)

    # -- endpoints -----------------------------------------------------------

    def check_params(self) -> tuple[bool, bool]:
        data = self._call("GET", "/param/load", retry_login=False)
        captcha = bool((data.get("captchaParam") or {}).get("conduitePublic"))
        blocked = bool((data.get("blocking") or {}).get("conduitePublic"))
        return captcha, blocked

    def login(self) -> dict:
        self.logged_in = False
        path = f"/login/loginConduitePrive/{self.lang}"
        body = {"noRegistre": self.candidate_id, "dateNaissance": self.birth_date, "captcha": ""}
        resp = self._raw("POST", path, body)
        if resp.status_code in (401, 403):
            raise LoginRejected(resp.status_code)
        if resp.status_code >= 400:
            raise PortalError(f"POST {path} -> HTTP {resp.status_code}: {resp.text[:200]}")
        data = self._json(resp, "POST", path)
        self.logged_in = True
        log.info("logged in")
        return data

    def categories(self) -> list[dict]:
        data = self.get("/personne/listCatPossible")
        return data if isinstance(data, list) else [data]

    def locations(self, cat: dict) -> list[dict]:
        data = self.post("/lieu-examen/listeLieu", type_body(cat))
        return data if isinstance(data, list) else [data]

    def free_week(self, cat: dict, id_lieu: Any, base: date) -> dict:
        body = type_body(cat, dateBase=portal_datetime(base), idLieuExamen=id_lieu)
        return self.post("/lieu-examen/listeJourHeureLibre", body)

    # -- discovery -----------------------------------------------------------

    def discover(self, out_dir: Path) -> list[Path]:
        """Run every call once and save the raw JSON so parsers can be checked
        against real data. Output contains personal data; keep it out of git."""
        out_dir = Path(out_dir)
        out_dir.mkdir(parents=True, exist_ok=True)
        written: list[Path] = []

        def dump(name: str, data: Any) -> None:
            p = out_dir / f"{name}.json"
            p.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
            written.append(p)

        dump("0_param", self._call("GET", "/param/load", retry_login=False))
        dump("1_login", self.login())
        cats = self.categories()
        dump("2_categories", cats)
        for i, cat in enumerate(cats):
            locs = self.locations(cat)
            dump(f"3_locations_cat{i}", locs)
            for j, loc in enumerate(locs):
                dump(f"4_week_cat{i}_loc{j}", self.free_week(cat, loc.get("idLieuExamen"), date.today()))
        return written
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `.venv/bin/pytest tests/test_portal.py -q`
Expected: 12 passed.

- [ ] **Step 5: Commit**

```bash
git add exam_poller.py tests/fakes.py tests/test_portal.py
git commit -m "Add portal client with login, session recovery and discovery"
```

---

### Task 6: Response parsers and week paging

**Files:**
- Modify: `exam_poller.py` (append)
- Create: `tests/test_parsers.py`

**Interfaces:**
- Consumes: `Slot`, `Portal.free_week`, `ZURICH`.
- Produces: `parse_portal_date(value) -> date`, `location_name(loc: dict) -> str`, `parse_week(raw: dict, location: str) -> tuple[list[Slot], bool]`, `Portal.free_slots(cat, loc, start: date, until: date | None, max_weeks: int) -> list[Slot]`.

- [ ] **Step 1: Write the failing tests**

`tests/test_parsers.py`:
```python
from datetime import date

import pytest

from exam_poller import Portal, Slot, location_name, parse_portal_date, parse_week
from tests.fakes import FakeResponse, FakeSession

WEEK = ("POST", "/lieu-examen/listeJourHeureLibre")
CAT = {"idGroupeExamen": 1, "categorie": "B", "idTypeExamen": 2, "idGenreExamen": 3, "rendezVous": 99}
LOC = {"idLieuExamen": 7, "nom": "Zürich-Albisgüetli"}


@pytest.mark.parametrize("value, expected", [
    ("2026-10-20", date(2026, 10, 20)),
    ("2026-10-20T00:00:00.000+0200", date(2026, 10, 20)),
    ("2026-10-20T00:00:00.000+02:00", date(2026, 10, 20)),
    ("2026-10-19T22:00:00.000Z", date(2026, 10, 20)),      # UTC midnight = Zurich 20th
    ("2026-10-19T22:00:00.000+0000", date(2026, 10, 20)),
    (1792180800000, date(2026, 10, 16)),                    # epoch ms, 2026-10-16 20:00 UTC
    ("1792180800000", date(2026, 10, 16)),
])
def test_parse_portal_date(value, expected):
    assert parse_portal_date(value) == expected


def test_parse_portal_date_rejects_garbage():
    with pytest.raises(ValueError):
        parse_portal_date("yesterday")


def test_location_name_prefers_known_keys_then_any_string():
    assert location_name({"idLieuExamen": 1, "nom": "Albisgüetli"}) == "Albisgüetli"
    assert location_name({"idLieuExamen": 1, "libelle": "Winterthur"}) == "Winterthur"
    assert location_name({"idLieuExamen": 1, "designation": "Bülach"}) == "Bülach"
    assert location_name({"idLieuExamen": 1, "whatever": "Regensdorf"}) == "Regensdorf"
    assert location_name({"idLieuExamen": 1}) == "lieu 1"


def test_parse_week_builds_slots_and_next_flag():
    raw = {
        "listeJourHeureLibreDispo": [
            {"jour": "2026-10-19T22:00:00.000Z", "heure": 8, "minute": 0},
            {"jour": "2026-10-20T22:00:00.000Z", "heure": 13, "minute": 30},
        ],
        "prevWeek": False,
        "nextWeek": True,
        "weekDay": "2026-10-18T22:00:00.000Z",
    }
    slots, has_next = parse_week(raw, "Albisgüetli")
    assert slots == [
        Slot(date(2026, 10, 20), "08:00", "Albisgüetli"),
        Slot(date(2026, 10, 21), "13:30", "Albisgüetli"),
    ]
    assert has_next is True


def test_parse_week_tolerates_missing_list():
    assert parse_week({"nextWeek": False}, "X") == ([], False)


def week(days: list[tuple[str, int]], has_next: bool) -> FakeResponse:
    entries = [{"jour": d, "heure": h, "minute": 0} for d, h in days]
    return FakeResponse(200, {"listeJourHeureLibreDispo": entries, "nextWeek": has_next})


def test_free_slots_pages_weeks_until_cutoff():
    sess = FakeSession({
        WEEK: [
            week([("2026-10-20", 8)], True),
            week([("2026-10-27", 9)], True),
            week([("2026-11-03", 10)], True),
        ],
    })
    p = Portal("1", "01.01.1990", session=sess)
    p.logged_in = True
    slots = p.free_slots(CAT, LOC, start=date(2026, 10, 19), until=date(2026, 10, 30), max_weeks=12)
    assert [s.date for s in slots] == [date(2026, 10, 20), date(2026, 10, 27)]
    bases = [c[2]["typeCategorieLieuDispo"]["dateBase"] for c in sess.calls]
    assert bases == ["2026-10-19T10:00:00.000Z", "2026-10-26T11:00:00.000Z"]


def test_free_slots_stops_when_portal_has_no_next_week():
    sess = FakeSession({WEEK: [week([("2026-10-20", 8)], False)]})
    p = Portal("1", "01.01.1990", session=sess)
    p.logged_in = True
    slots = p.free_slots(CAT, LOC, start=date(2026, 10, 19), until=None, max_weeks=12)
    assert len(slots) == 1
    assert len(sess.calls) == 1


def test_free_slots_respects_max_weeks_without_cutoff():
    sess = FakeSession({WEEK: [week([], True)]})
    p = Portal("1", "01.01.1990", session=sess)
    p.logged_in = True
    p.free_slots(CAT, LOC, start=date(2026, 10, 19), until=None, max_weeks=3)
    assert len(sess.calls) == 3
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv/bin/pytest tests/test_parsers.py -q`
Expected: ImportError on `parse_portal_date`.

- [ ] **Step 3: Write the implementation**

Append to `exam_poller.py`, and add the `free_slots` method inside `class Portal` right after `free_week`:
```python
_LOCATION_NAME_KEYS = ("nom", "libelle", "designation", "nomLieu", "lieu", "name", "label")


def parse_portal_date(value: Any) -> date:
    """Accept the date encodings a Java backend may emit and return the Zurich calendar date."""
    if isinstance(value, (int, float)) or (isinstance(value, str) and value.strip().isdigit()):
        return datetime.fromtimestamp(int(value) / 1000, tz=timezone.utc).astimezone(ZURICH).date()
    text = str(value).strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    # Jackson's default offset form is +0200; fromisoformat wants +02:00.
    if len(text) >= 5 and text[-5] in "+-" and text[-4:].isdigit():
        text = text[:-2] + ":" + text[-2:]
    parsed = datetime.fromisoformat(text)  # raises ValueError on garbage
    if parsed.tzinfo is not None:
        parsed = parsed.astimezone(ZURICH)
    return parsed.date()


def location_name(loc: dict) -> str:
    for key in _LOCATION_NAME_KEYS:
        value = loc.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    for key, value in loc.items():
        if isinstance(value, str) and value.strip():
            return value.strip()
    return f"lieu {loc.get('idLieuExamen')}"


def parse_week(raw: dict, location: str) -> tuple[list[Slot], bool]:
    slots: list[Slot] = []
    for entry in raw.get("listeJourHeureLibreDispo") or []:
        day = parse_portal_date(entry["jour"])
        hour = int(entry.get("heure") or 0)
        minute = int(entry.get("minute") or 0)
        slots.append(Slot(date=day, time=f"{hour:02d}:{minute:02d}", location=location))
    return slots, bool(raw.get("nextWeek"))
```

Inside `class Portal`, after `free_week`:
```python
    def free_slots(self, cat: dict, loc: dict, start: date, until: date | None, max_weeks: int) -> list[Slot]:
        """Page week by week from `start` and collect free slots at one location."""
        name = location_name(loc)
        out: list[Slot] = []
        base = start
        for _ in range(max_weeks):
            raw = self.free_week(cat, loc.get("idLieuExamen"), base)
            slots, has_next = parse_week(raw, name)
            out.extend(slots)
            base = base + timedelta(days=7)
            if not has_next or (until is not None and base > until):
                break
        return out
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `.venv/bin/pytest tests/test_parsers.py -q`
Expected: 14 passed.

- [ ] **Step 5: Commit**

```bash
git add exam_poller.py tests/test_parsers.py
git commit -m "Add portal response parsers and week paging"
```

---

### Task 7: Poll cycle, CLI, main loop and README

**Files:**
- Modify: `exam_poller.py` (append), `README.md`
- Create: `tests/test_cycle.py`

**Interfaces:**
- Consumes: everything above.
- Produces: `run_cycle(cfg, portal, flt, state, notifier, alerts) -> int` (new slots emailed), `parse_args(argv) -> argparse.Namespace`, `main(argv=None) -> int`.

- [ ] **Step 1: Write the failing tests**

`tests/test_cycle.py`:
```python
import smtplib
from datetime import date

import pytest

import exam_poller
from exam_poller import Alerts, Config, Notifier, Portal, SlotFilter, SmtpConfig, State, run_cycle
from tests.fakes import FakeResponse, FakeSession

LOGIN = ("POST", "/login/loginConduitePrive/de")
PARAM = ("GET", "/param/load")
CATS = ("GET", "/personne/listCatPossible")
LIEUX = ("POST", "/lieu-examen/listeLieu")
WEEK = ("POST", "/lieu-examen/listeJourHeureLibre")
CAT = {"idGroupeExamen": 1, "categorie": "B", "idTypeExamen": 2, "idGenreExamen": 3, "rendezVous": 99}
ALBIS = {"idLieuExamen": 7, "nom": "Zürich-Albisgüetli"}
WINTI = {"idLieuExamen": 8, "nom": "Winterthur"}
OFF = {"captchaParam": {"conduitePublic": False}, "blocking": {"conduitePublic": False}}


class Sent:
    def __init__(self):
        self.items: list[tuple[str, str]] = []
        self.fail = False

    def __call__(self, subject, body):
        if self.fail:
            raise smtplib.SMTPException("down")
        self.items.append((subject, body))


def cfg(notify_before=None, locations=("Albisguetli",)) -> Config:
    return Config(candidate_id="1", birth_date="01.01.1990", notify_before=notify_before,
                  locations=list(locations), smtp=SmtpConfig("h", 587, "", "", "a@b", "a@b"),
                  poll_interval_minutes=60, max_weeks=12)


def build(tmp_path, routes, config=None):
    config = config or cfg()
    sess = FakeSession(routes)
    portal = Portal(config.candidate_id, config.birth_date, session=sess)
    notifier = Notifier(config.smtp)
    sent = Sent()
    notifier.send = sent
    return dict(cfg=config, portal=portal, flt=SlotFilter(config.notify_before, config.locations),
                state=State(tmp_path / "state.json"), notifier=notifier, alerts=Alerts(notifier)), sess, sent


def week(days, has_next=False):
    return FakeResponse(200, {"listeJourHeureLibreDispo": [{"jour": d, "heure": 8, "minute": 0} for d in days],
                              "nextWeek": has_next})


def test_cycle_emails_new_matching_slots_only_once(tmp_path):
    parts, sess, sent = build(tmp_path, {
        PARAM: [FakeResponse(200, OFF)],
        LOGIN: [FakeResponse(200, {})],
        CATS: [FakeResponse(200, [CAT])],
        LIEUX: [FakeResponse(200, [ALBIS, WINTI])],
        WEEK: [week(["2026-11-10"])],
    })
    assert run_cycle(**parts) == 1
    assert sent.items[0][0] == "Exam slots: 1 new, earliest 10.11.2026"
    # Winterthur was filtered out before any slot request was made for it.
    assert sum(1 for m, p, _ in sess.calls if (m, p) == WEEK) == 1
    assert run_cycle(**parts) == 0
    assert len(sent.items) == 1


def test_cycle_skips_and_alerts_when_captcha_on(tmp_path):
    parts, sess, sent = build(tmp_path, {
        PARAM: [FakeResponse(200, {"captchaParam": {"conduitePublic": True}, "blocking": {"conduitePublic": False}})],
    })
    assert run_cycle(**parts) == 0
    assert sent.items[0][0] == "Exam poller problem: captcha"
    assert LOGIN not in sess.paths()


def test_cycle_alerts_once_on_login_rejected(tmp_path):
    parts, sess, sent = build(tmp_path, {
        PARAM: [FakeResponse(200, OFF)],
        LOGIN: [FakeResponse(401, {})],
    })
    run_cycle(**parts)
    run_cycle(**parts)
    assert [s for s, _ in sent.items] == ["Exam poller problem: login"]


def test_smtp_failure_leaves_state_untouched_so_slots_realert(tmp_path):
    parts, sess, sent = build(tmp_path, {
        PARAM: [FakeResponse(200, OFF)],
        LOGIN: [FakeResponse(200, {})],
        CATS: [FakeResponse(200, [CAT])],
        LIEUX: [FakeResponse(200, [ALBIS])],
        WEEK: [week(["2026-11-10"])],
    })
    sent.fail = True
    with pytest.raises(smtplib.SMTPException):
        run_cycle(**parts)
    assert not (tmp_path / "state.json").exists()
    sent.fail = False
    assert run_cycle(**parts) == 1


def test_main_once_dry_run(tmp_path, monkeypatch, capsys):
    config_path = tmp_path / "config.json"
    config_path.write_text(
        '{"credentials": {"candidate_id": "1", "birth_date": "01.01.1990"},'
        ' "filter": {"locations": ["Albisguetli"]},'
        ' "smtp": {"host": "h", "port": 587, "from": "a@b", "to": "a@b"}}'
    )
    sess = FakeSession({
        PARAM: [FakeResponse(200, OFF)],
        LOGIN: [FakeResponse(200, {})],
        CATS: [FakeResponse(200, [CAT])],
        LIEUX: [FakeResponse(200, [ALBIS])],
        WEEK: [week(["2026-11-10"])],
    })
    monkeypatch.setattr(exam_poller.requests, "Session", lambda: sess)
    rc = exam_poller.main(["--config", str(config_path), "--state", str(tmp_path / "s.json"),
                           "--once", "--dry-run"])
    assert rc == 0
    assert "Exam slots: 1 new" in capsys.readouterr().out


def test_main_reports_config_error(tmp_path, capsys):
    rc = exam_poller.main(["--config", str(tmp_path / "missing.json"), "--once"])
    assert rc == 2
    assert "config error" in capsys.readouterr().err
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv/bin/pytest tests/test_cycle.py -q`
Expected: ImportError on `run_cycle`.

- [ ] **Step 3: Write the implementation**

Append to `exam_poller.py` (add `import argparse`, `import random`, `import sys`, `import time as _time` to the imports; `time` is already used for `datetime.time`, hence the alias):
```python
def run_cycle(cfg: Config, portal: Portal, flt: SlotFilter, state: State,
              notifier: Notifier, alerts: Alerts) -> int:
    """One poll. Returns the number of new slots emailed. Raises on network or SMTP failure."""
    captcha, blocked = portal.check_params()
    if captcha:
        alerts.raise_("captcha", "The portal has switched the login captcha on. "
                                 "The poller cannot log in until it is switched off again.")
        return 0
    alerts.clear("captcha")
    if blocked:
        alerts.raise_("blocked", "The portal reports the private driving-exam service as blocked.")
        return 0
    alerts.clear("blocked")

    if not portal.logged_in:
        try:
            portal.login()
        except LoginRejected as exc:
            alerts.raise_("login", f"Login was rejected with HTTP {exc.status}. "
                                   "Check candidate_id and birth_date in the config.")
            return 0
    alerts.clear("login")

    today = date.today()
    found: list[Slot] = []
    for cat in portal.categories():
        for loc in portal.locations(cat):
            name = location_name(loc)
            if not flt.location_matches(name):
                log.debug("skipping location %s", name)
                continue
            slots = portal.free_slots(cat, loc, start=today, until=flt.notify_before,
                                      max_weeks=cfg.max_weeks)
            log.info("%s: %d free slot(s) within horizon", name, len(slots))
            found.extend(slots)

    matches = flt.apply(found)
    new = state.new_slots(matches)
    if new:
        notifier.send_slots(new)
    state.save(matches)
    return len(new)


def parse_args(argv: list[str] | None) -> argparse.Namespace:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--config", default="config.json", help="path to config JSON (default: config.json)")
    ap.add_argument("--state", default="state.json", help="path to state JSON (default: state.json)")
    ap.add_argument("--once", action="store_true", help="run a single cycle and exit")
    ap.add_argument("--dry-run", action="store_true", help="print emails instead of sending them")
    ap.add_argument("--discover", action="store_true",
                    help="log in once and save every raw response under discovery/")
    ap.add_argument("-v", "--verbose", action="store_true", help="debug logging")
    return ap.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO,
                        format="%(asctime)s %(levelname)s %(message)s")
    try:
        cfg = Config.load(args.config)
    except ConfigError as exc:
        print(f"config error: {exc}", file=sys.stderr)
        return 2

    portal = Portal(cfg.candidate_id, cfg.birth_date)

    if args.discover:
        try:
            for path in portal.discover(Path("discovery")):
                print(path)
        except (requests.RequestException, PortalError) as exc:
            print(f"discovery failed: {exc}", file=sys.stderr)
            return 1
        return 0

    notifier = Notifier(cfg.smtp, dry_run=args.dry_run)
    alerts = Alerts(notifier)
    flt = SlotFilter(cfg.notify_before, cfg.locations)
    state = State(args.state)
    base_delay = cfg.poll_interval_minutes * 60
    failures = 0

    while True:
        try:
            new = run_cycle(cfg, portal, flt, state, notifier, alerts)
            failures = 0
            alerts.clear("errors")
            log.info("cycle done, %d new slot(s)", new)
        except (requests.RequestException, PortalError) as exc:
            failures += 1
            log.warning("cycle failed (%d in a row): %s", failures, exc)
            portal.logged_in = False
            if failures >= 5:
                alerts.raise_("errors", f"{failures} consecutive failures. Last error: {exc}")
        except (smtplib.SMTPException, OSError) as exc:
            log.error("email failed, will retry next cycle: %s", exc)

        if args.once:
            return 0
        delay = min(base_delay * (2 ** min(failures, 4)), 6 * 3600)
        delay += random.uniform(0, 0.1 * delay)
        log.info("next poll in %.0f min", delay / 60)
        _time.sleep(delay)


if __name__ == "__main__":
    sys.exit(main())
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `.venv/bin/pytest -q`
Expected: all tests pass (9 + 8 + 5 + 7 + 12 + 14 + 6 = 61).

- [ ] **Step 5: Write the README**

Replace `README.md` with:
```markdown
# Exam slot poller

Polls the Strassenverkehrsamt Zürich appointment portal for free practical
driving-exam slots and emails you when a new one matches your filter.

## Setup

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
cp config.example.json config.json   # then edit it
```

`config.json` is gitignored. Fields:

| Field | Meaning |
|---|---|
| `credentials.candidate_id` | the digits you type into the portal's login |
| `credentials.birth_date` | `dd.mm.yyyy` |
| `filter.notify_before` | only alert for slots on or before this date; empty = no cutoff |
| `filter.locations` | substrings of exam locations, umlaut-insensitive; empty = all |
| `smtp.*` | your mail server; `starttls: true` for port 587, `false` for SSL on 465 |
| `poll_interval_minutes` | default 60 |
| `max_weeks` | how far ahead to look when no cutoff is set, default 12 |

## First run

```bash
.venv/bin/python exam_poller.py --discover      # saves raw responses under discovery/
.venv/bin/python exam_poller.py --once --dry-run
```

## Run

```bash
.venv/bin/python exam_poller.py        # loops forever, Ctrl-C to stop
```

Keep it alive with `tmux`, `nohup`, or a launchd job calling `--once` on a schedule.

The script emails you once per new slot set, and once per problem (captcha
switched on, login rejected, repeated request failures) until that problem clears.

## Tests

```bash
.venv/bin/pytest -q
```
```

- [ ] **Step 6: Commit**

```bash
git add exam_poller.py tests/test_cycle.py README.md
git commit -m "Add poll cycle, CLI, main loop and README"
```

---

### Task 8: Verify parsers against a real discovery capture

This task needs the user's real credentials and is run by the user, not an agent.

**Files:**
- Modify: `exam_poller.py` only if the capture shows different field names.
- Create: `tests/fixtures/week.json`, `tests/fixtures/locations.json` (sanitised copies, no personal data).

- [ ] **Step 1: User runs discovery**

```bash
cp config.example.json config.json   # fill in real values
.venv/bin/python exam_poller.py --discover
```
Expected: five or more file paths printed under `discovery/`, and `1_login.json` has a non-null `personneDispo`.

- [ ] **Step 2: Compare the capture with the parsers**

Check, in this order, and fix the named function if it differs:
- `2_categories.json`: a list of objects containing `idGroupeExamen`, `categorie`, `idTypeExamen`, `idGenreExamen`, `rendezVous`. If not, adjust `type_body`.
- `3_locations_cat0.json`: each object has `idLieuExamen` and a human-readable name. If the name key is not in `_LOCATION_NAME_KEYS`, add it at the front of that tuple.
- `4_week_cat0_loc0.json`: has `listeJourHeureLibreDispo` with `jour`, `heure`, `minute`, plus `nextWeek`. If the date encoding is not one that `parse_portal_date` handles, add a parametrised case to `tests/test_parsers.py::test_parse_portal_date` with the real string first, then extend the function.

- [ ] **Step 3: Add sanitised fixtures and a test that loads them**

Copy the week and locations files into `tests/fixtures/`, delete any personal fields (names, candidate ids, addresses), and append to `tests/test_parsers.py`:
```python
import json
from pathlib import Path

FIXTURES = Path(__file__).parent / "fixtures"


def test_real_week_fixture_parses():
    raw = json.loads((FIXTURES / "week.json").read_text(encoding="utf-8"))
    slots, _ = parse_week(raw, "Albisgüetli")
    assert all(s.time.count(":") == 1 for s in slots)


def test_real_locations_fixture_has_names():
    locs = json.loads((FIXTURES / "locations.json").read_text(encoding="utf-8"))
    assert all(not location_name(l).startswith("lieu ") for l in locs)
```

- [ ] **Step 4: Run the whole suite and one real dry run**

```bash
.venv/bin/pytest -q
.venv/bin/python exam_poller.py --once --dry-run -v
```
Expected: all tests pass; the dry run logs one line per location with a slot count and prints an email only if matching slots exist.

- [ ] **Step 5: Commit**

```bash
git add exam_poller.py tests/test_parsers.py tests/fixtures/
git commit -m "Verify parsers against real portal responses"
```
