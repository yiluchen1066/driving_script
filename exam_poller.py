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
import re
import smtplib
import unicodedata
from dataclasses import dataclass
from datetime import date, datetime
from email.message import EmailMessage
from pathlib import Path
from typing import Iterable

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


_VOWEL_E = re.compile(r"([aou])e+")


def normalise(text: str) -> str:
    """Fold a place name so that common spellings compare equal:
    'Albisgüetli', 'Albisguetli' and 'Albisgueetli' all become 'albisgutli',
    'Zürich' and 'Zuerich' both become 'zurich'. Case-insensitive."""
    decomposed = unicodedata.normalize("NFD", text).casefold()
    stripped = "".join(ch for ch in decomposed if not unicodedata.combining(ch))
    return _VOWEL_E.sub(r"\1", stripped)


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
