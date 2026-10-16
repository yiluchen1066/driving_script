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
import unicodedata
from dataclasses import dataclass
from datetime import date, datetime
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
