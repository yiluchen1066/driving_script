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

import argparse
import json
import logging
import random
import re
import smtplib
import sys
import time as _time
import unicodedata
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta, timezone
from email.message import EmailMessage
from pathlib import Path
from typing import Any, Iterable
from zoneinfo import ZoneInfo

import requests

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


TIME_FMT = "%H:%M"


def _check_time(text: str, name: str) -> str:
    try:
        return datetime.strptime(text, TIME_FMT).strftime(TIME_FMT)
    except ValueError:
        raise ConfigError(f"{name} must be HH:MM, got {text!r}") from None


@dataclass
class DateWindow:
    """One entry of filter.dates: a day, optionally limited to a time range (inclusive)."""
    day: date
    start: str | None = None   # "HH:MM"
    end: str | None = None     # "HH:MM"

    @classmethod
    def from_dict(cls, raw: dict, name: str) -> "DateWindow":
        if not isinstance(raw, dict):
            raise ConfigError(f"{name} must be an object like {{\"date\": \"dd.mm.yyyy\", \"from\": \"HH:MM\", \"to\": \"HH:MM\"}}")
        day_text = str(raw.get("date") or "").strip()
        if not day_text:
            raise ConfigError(f"{name}.date is required")
        _check_date(day_text, f"{name}.date")
        start = str(raw.get("from") or "").strip() or None
        end = str(raw.get("to") or "").strip() or None
        if start:
            start = _check_time(start, f"{name}.from")
        if end:
            end = _check_time(end, f"{name}.to")
        if start and end and start > end:
            raise ConfigError(f"{name}: 'from' {start} is after 'to' {end}")
        return cls(day=parse_date(day_text), start=start, end=end)

    def contains(self, day: date, time_text: str) -> bool:
        if day != self.day:
            return False
        if self.start is not None and time_text < self.start:
            return False
        if self.end is not None and time_text > self.end:
            return False
        return True


@dataclass
class Config:
    candidate_id: str
    birth_date: str
    notify_before: date | None
    locations: list[str]
    smtp: SmtpConfig
    poll_interval_minutes: int = 60
    max_weeks: int = 12
    auto_book: bool = False
    notify_after: date | None = None
    dates: list[DateWindow] = field(default_factory=list)

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
        start_text = str(filt.get("notify_after") or "").strip()
        notify_after = None
        if start_text:
            _check_date(start_text, "filter.notify_after")
            notify_after = parse_date(start_text)
        locations = filt.get("locations") or []
        if not isinstance(locations, list) or not all(isinstance(x, str) for x in locations):
            raise ConfigError("filter.locations must be a list of strings")
        dates_raw = filt.get("dates") or []
        if not isinstance(dates_raw, list):
            raise ConfigError("filter.dates must be a list")
        dates = [DateWindow.from_dict(d, f"filter.dates[{i}]") for i, d in enumerate(dates_raw)]

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
        auto_book = raw.get("auto_book", False)
        if not isinstance(auto_book, bool):
            raise ConfigError("auto_book must be true or false")

        return cls(
            candidate_id=candidate_id,
            birth_date=birth_date,
            notify_before=notify_before,
            locations=locations,
            smtp=smtp,
            poll_interval_minutes=interval,
            max_weeks=max_weeks,
            auto_book=auto_book,
            notify_after=notify_after,
            dates=dates,
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
    # Portal objects needed to book this slot (category, location, raw entry).
    # Excluded from equality, ordering and hashing.
    booking: dict | None = field(default=None, compare=False, repr=False)

    @property
    def key(self) -> str:
        return f"{self.location}|{self.date.isoformat()}|{self.time}"


class SlotFilter:
    def __init__(self, notify_before: date | None, locations: list[str],
                 notify_after: date | None = None, dates: Iterable[DateWindow] = ()):
        self.notify_before = notify_before
        self.notify_after = notify_after
        self.dates = list(dates)
        self.locations = [normalise(name) for name in locations if name.strip()]

    def location_matches(self, name: str) -> bool:
        if not self.locations:
            return True
        folded = normalise(name)
        return any(wanted in folded for wanted in self.locations)

    def slot_matches(self, slot: Slot) -> bool:
        if self.notify_after is not None and slot.date < self.notify_after:
            return False
        if self.notify_before is not None and slot.date > self.notify_before:
            return False
        if self.dates and not any(w.contains(slot.date, slot.time) for w in self.dates):
            return False
        return self.location_matches(slot.location)

    def search_bounds(self, today: date) -> tuple[date, date | None]:
        """First day to request and last day worth requesting, from all date limits."""
        start = today
        if self.notify_after is not None:
            start = max(start, self.notify_after)
        until = self.notify_before
        if self.dates:
            start = max(start, min(w.day for w in self.dates))
            last = max(w.day for w in self.dates)
            until = last if until is None else min(until, last)
        return start, until

    def apply(self, slots: Iterable[Slot]) -> list[Slot]:
        return sorted(s for s in slots if self.slot_matches(s))


class State:
    """Keys of the slots emailed in the previous cycle, kept in a small JSON file."""

    def __init__(self, path: Path | str):
        self.path = Path(path)

    def _read(self) -> dict:
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return {}
        except (ValueError, OSError) as exc:  # bad JSON, truncated UTF-8, unreadable path
            log.warning("state file %s is unreadable (%s), starting fresh", self.path, exc)
            return {}
        if not isinstance(data, dict):
            log.warning("state file %s has unexpected shape, starting fresh", self.path)
            return {}
        return data

    def _write(self, data: dict) -> None:
        self.path.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")

    def load(self) -> set[str]:
        notified = self._read().get("notified")
        if not isinstance(notified, list):
            return set()
        return {str(k) for k in notified}

    def save(self, slots: Iterable[Slot]) -> None:
        data = self._read()
        data["notified"] = sorted(s.key for s in slots)
        data["updated"] = datetime.now().isoformat(timespec="seconds")
        self._write(data)

    def booked(self) -> dict | None:
        """The booking this poller made, if any. Once set, auto_book never books again."""
        booked = self._read().get("booked")
        return booked if isinstance(booked, dict) else None

    def record_booking(self, slot: Slot, response: dict) -> None:
        data = self._read()
        portal = response.get("nouveauRendezVous") if isinstance(response, dict) else None
        data["booked"] = {
            "location": slot.location,
            "date": slot.date.isoformat(),
            "time": slot.time,
            "booked_at": datetime.now().isoformat(timespec="seconds"),
            "portal": {k: v for k, v in (portal or {}).items() if isinstance(v, (str, int, float, bool)) or v is None},
        }
        self._write(data)

    def new_slots(self, slots: list[Slot]) -> list[Slot]:
        seen = self.load()
        return [s for s in slots if s.key not in seen]


BASE_URL = "https://portal.stva.zh.ch/ecari-dispoweb"
PORTAL_UI_URL = f"{BASE_URL}/ui/app/init/"
BOOKING_URL = f"{PORTAL_UI_URL}#/conduite/prive/login"


def format_slot_email(slots: Iterable[Slot]) -> tuple[str, str]:
    ordered = sorted(slots)
    earliest = ordered[0].date.strftime(DATE_FMT)
    subject = f"Exam slots: {len(ordered)} new, earliest {earliest}"
    lines = [f"{s.date.strftime('%a %d.%m.%Y')}  {s.time}  {s.location}" for s in ordered]
    body = "New free exam slots:\n\n" + "\n".join(lines) + f"\n\nBook at: {BOOKING_URL}\n"
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

    def free_slots(self, cat: dict, loc: dict, start: date, until: date | None, max_weeks: int) -> list[Slot]:
        """Page week by week from `start` and collect free slots at one location."""
        name = location_name(loc)
        out: list[Slot] = []
        seen: set[str] = set()
        base = start
        for _ in range(max_weeks):
            raw = self.free_week(cat, loc.get("idLieuExamen"), base)
            slots, has_next = parse_week(raw, name, cat, loc)
            for slot in slots:
                if slot.key not in seen:
                    seen.add(slot.key)
                    out.append(slot)
            # The portal clamps the requested week to its minimum lead time and
            # reports the week it actually served in weekDay; step on from there.
            served = raw.get("weekDay")
            week_start = parse_portal_date(served) if served is not None else base
            base = max(week_start, base) + timedelta(days=7)
            if not has_next:
                break
            # The portal serves whole Monday-to-Sunday weeks, so keep going as long
            # as the week of the next request starts on or before the limits.
            next_monday = base - timedelta(days=base.weekday())
            if until is not None and next_monday > until:
                break
            max_date = raw.get("maxDate")
            if max_date is not None and next_monday > parse_portal_date(max_date):
                break
        return out

    def existing_appointments(self) -> int | None:
        """How many appointments the portal already holds for this candidate."""
        try:
            data = self.get("/rendez-vous/listeRdvPrive")
        except PortalError:
            return None
        lst = data.get("listeRdvPrive") if isinstance(data, dict) else None
        return len(lst) if isinstance(lst, list) else None

    def nothing_offered_reason(self) -> str:
        n = self.existing_appointments()
        text = "The portal offers no exam category or location for your record, so there is nothing to search. "
        if n:
            text += (f"It shows {n} existing appointment(s). While an appointment exists the portal does not offer "
                     "a new booking; cancel it in the portal if you want the poller to search and book again.")
        elif n == 0:
            text += "It shows no existing appointment either; check your record in the portal."
        else:
            text += "Check your record in the portal."
        return text

    # -- booking -------------------------------------------------------------

    def choose_slot(self, cat: dict, loc: dict, entry: dict) -> dict:
        """Non-committing pre-check the UI runs when a slot is clicked. Returns
        the portal's notice (amount, message, days allowed for moving)."""
        same_place = cat.get("idLieuExamen") == loc.get("idLieuExamen")
        body = {
            "idrendezvous": cat.get("rendezVous"),
            "typeCategorieLieu": {
                "idLieuExamen": loc.get("idLieuExamen"),
                "dateBase": browser_json_date(entry.get("jour")),
                "idCategorie": cat.get("categorie"),
                "idGenreExamen": cat.get("idGenreExamen") if same_place else None,
                "idTypeExamen": cat.get("idTypeExamen"),
            },
        }
        data = self.post("/rendez-vous/choixRdv", body)
        return data if isinstance(data, dict) else {}

    def book_slot(self, cat: dict, loc: dict, entry: dict) -> dict:
        """Commit a NEW booking (the UI's Validate button). Raises PortalError
        unless the portal answers with a nouveauRendezVous object."""
        slot = dict(entry)
        slot["jour"] = browser_json_date(entry.get("jour"))
        body = {
            "rendezVous": cat.get("rendezVous"),
            "slot": slot,
            "typeCategorieLieu": {
                "idLieuExamen": loc.get("idLieuExamen"),
                "idCategorie": cat.get("categorie"),
                "idGenreExamen": cat.get("idGenreExamen"),
                "idTypeExamen": cat.get("idTypeExamen"),
            },
            "action": "confirm",
        }
        data = self.post("/rendez-vous/priseRdv", body)
        if not isinstance(data, dict) or not data.get("nouveauRendezVous"):
            code = data.get("retCode") if isinstance(data, dict) else None
            msg = data.get("retMessage") if isinstance(data, dict) else repr(data)[:200]
            raise PortalError(f"booking not confirmed by the portal (retCode={code}, message={msg})")
        return data

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


_LOCATION_NAME_KEYS = ("libelleLieu", "nom", "libelle", "designation", "nomLieu", "lieu", "name", "label")


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
        if isinstance(value, str) and value.strip() and not key.lower().startswith("id"):
            return value.strip()
    return f"lieu {loc.get('idLieuExamen')}"


def parse_week(raw: dict, location: str, cat: dict | None = None,
               loc: dict | None = None) -> tuple[list[Slot], bool]:
    code = raw.get("retCode")
    if code is not None and str(code) != "0000":
        raise PortalError(f"portal returned retCode {code}: {raw.get('retMessage')}")
    slots: list[Slot] = []
    for entry in raw.get("listeJourHeureLibreDispo") or []:
        day = parse_portal_date(entry["jour"])
        hour = int(entry.get("heure") or 0)
        minute = int(entry.get("minute") or 0)
        booking = {"cat": cat, "loc": loc, "entry": entry} if cat is not None and loc is not None else None
        slots.append(Slot(date=day, time=f"{hour:02d}:{minute:02d}", location=location, booking=booking))
    return slots, bool(raw.get("nextWeek"))


def browser_json_date(value: Any) -> Any:
    """Re-encode a portal date the way the browser sends it back: ISO UTC with Z."""
    if isinstance(value, (int, float)) or (isinstance(value, str) and value.strip().isdigit()):
        return datetime.fromtimestamp(int(value) / 1000, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000Z")
    return value


BOOK_MIN_DAYS_AHEAD = 10   # hard rule: never book a slot within 10 days of today
BOOK_COUNTDOWN_SECONDS = 10


class StopPolling(Exception):
    """Raised after a successful booking: there is nothing left to watch."""


def format_booked_email(slot: Slot, response: dict) -> tuple[str, str]:
    when = f"{slot.date.strftime('%a %d.%m.%Y')}  {slot.time}  {slot.location}"
    portal = response.get("nouveauRendezVous") or {}
    ref = portal.get("rendezVous") or portal.get("id") or ""
    subject = f"BOOKED: exam {slot.date.strftime(DATE_FMT)} {slot.time} {slot.location}"
    body = (
        "The poller booked this exam slot for you:\n\n"
        f"  {when}\n\n"
        + (f"Portal appointment reference: {ref}\n\n" if ref else "")
        + "Log in to the portal, check the appointment and print the convocation yourself; "
        "the portal requires that within a few days:\n"
        f"{BOOKING_URL}\n\n"
        "The poller has stopped. It will not book again while state.json records this booking.\n"
    )
    return subject, body


def attempt_booking(cfg: Config, portal: Portal, state: State, notifier: Notifier,
                    alerts: Alerts, matches: list[Slot]) -> None:
    """Book the earliest matching slot that is more than BOOK_MIN_DAYS_AHEAD days away."""
    if state.booked() is not None:
        log.info("auto_book is on but a booking is already recorded in %s; not booking again", state.path)
        return
    today = datetime.now(ZURICH).date()
    eligible = [s for s in matches if s.booking and (s.date - today).days > BOOK_MIN_DAYS_AHEAD]
    too_close = len(matches) - len(eligible)
    if too_close:
        log.info("%d matching slot(s) not bookable: within %d days of today", too_close, BOOK_MIN_DAYS_AHEAD)
    if not eligible:
        return
    target = eligible[0]
    label = f"{target.date.strftime('%a %d.%m.%Y')} {target.time} {target.location}"
    if notifier.dry_run:
        print(f"--- DRY RUN booking ---\nWould book: {label}", flush=True)
        log.info("dry run: would book %s", label)
        return
    log.warning("BOOKING %s in %d seconds (Ctrl-C to abort)", label, BOOK_COUNTDOWN_SECONDS)
    _time.sleep(BOOK_COUNTDOWN_SECONDS)
    b = target.booking
    try:
        notice = portal.choose_slot(b["cat"], b["loc"], b["entry"])
        log.info("portal pre-check: amount=%s message=%s", notice.get("montant"), notice.get("message"))
        result = portal.book_slot(b["cat"], b["loc"], b["entry"])
    except PortalError as exc:
        alerts.raise_("booking", f"Booking {label} failed: {exc}. "
                                 "The poller keeps running and will try again next cycle if a slot is free.")
        return
    state.record_booking(target, result)
    alerts.clear("booking")
    log.warning("BOOKED %s", label)
    subject, body = format_booked_email(target, result)
    try:
        notifier.send(subject, body)
    except (smtplib.SMTPException, OSError) as exc:
        log.error("booked %s but the confirmation email failed: %s", label, exc)
    raise StopPolling(f"booked {label}")


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
            raise
    alerts.clear("login")

    today = datetime.now(ZURICH).date()
    start, until = flt.search_bounds(today)
    if until is not None and until < start:
        log.info("no dates left to search: window ends %s, search would start %s", until, start)
        state.save([])
        return 0
    found: list[Slot] = []
    seen_names: list[str] = []
    matched_locations = 0
    for cat in portal.categories():
        for loc in portal.locations(cat):
            name = location_name(loc)
            if name not in seen_names:
                seen_names.append(name)
            if not flt.location_matches(name):
                log.debug("skipping location %s", name)
                continue
            matched_locations += 1
            slots = portal.free_slots(cat, loc, start=start, until=until,
                                      max_weeks=cfg.max_weeks)
            log.info("%s: %d free slot(s) found", name, len(slots))
            found.extend(slots)

    if not seen_names:
        alerts.clear("filter")
        alerts.raise_("nothing-offered", portal.nothing_offered_reason())
        state.save([])
        return 0
    alerts.clear("nothing-offered")
    if flt.locations and matched_locations == 0:
        alerts.raise_("filter", f"No exam location matched filter.locations={cfg.locations}. "
                                f"The portal lists: {', '.join(seen_names)}. "
                                "Fix the filter in the config.")
    else:
        alerts.clear("filter")

    matches = flt.apply(found)
    new = state.new_slots(matches)
    if new:
        notifier.send_slots(new)
    state.save(matches)
    if cfg.auto_book and matches:
        attempt_booking(cfg, portal, state, notifier, alerts, matches)
    return len(new)


def parse_args(argv: list[str] | None) -> argparse.Namespace:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--config", default="config.json", help="path to config JSON (default: config.json)")
    ap.add_argument("--state", default="state.json", help="path to state JSON (default: state.json)")
    ap.add_argument("--once", action="store_true", help="run a single cycle and exit")
    ap.add_argument("--dry-run", action="store_true", help="print emails instead of sending them")
    ap.add_argument("--discover", action="store_true",
                    help="log in once and save every raw response under discovery/")
    ap.add_argument("--test-email", action="store_true",
                    help="send a sample vacancy email with the configured SMTP settings and exit")
    ap.add_argument("-v", "--verbose", action="store_true", help="debug logging")
    return ap.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    try:  # Windows consoles and redirected files default to a legacy code page
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError, OSError):
        pass
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
    log.handlers[:] = [handler]
    log.propagate = False
    log.setLevel(logging.DEBUG if args.verbose else logging.INFO)
    try:
        cfg = Config.load(args.config)
    except ConfigError as exc:
        print(f"config error: {exc}", file=sys.stderr)
        return 2

    if args.test_email:
        today = datetime.now(ZURICH).date()
        where = "Albisgütli"
        if cfg.locations and normalise(cfg.locations[0]) != normalise(where):
            where = cfg.locations[0]
        sample = [
            Slot(today + timedelta(days=14), "08:00", where),
            Slot(today + timedelta(days=18), "13:30", where),
            Slot(today + timedelta(days=27), "10:00", where),
        ]
        subject, body = format_slot_email(sample)
        body = ("This is a test. A real notification looks exactly like this, without the "
                "[TEST] tag, and lists only slots that are actually free.\n\n" + body)
        try:
            Notifier(cfg.smtp).send("[TEST] " + subject, body)
        except (smtplib.SMTPException, OSError) as exc:
            print(f"test email FAILED: {exc!r}", file=sys.stderr)
            if "gmail" in cfg.smtp.host and "535" not in repr(exc):
                print("hint: Gmail needs a 16-letter App Password, not your normal password", file=sys.stderr)
            return 1
        print(f"test email sent to {cfg.smtp.to} via {cfg.smtp.host}:{cfg.smtp.port}")
        return 0

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
    flt = SlotFilter(cfg.notify_before, cfg.locations, cfg.notify_after, cfg.dates)
    state = State(args.state)
    base_delay = cfg.poll_interval_minutes * 60
    failures = 0

    while True:
        cycle_ok = False
        try:
            new = run_cycle(cfg, portal, flt, state, notifier, alerts)
            cycle_ok = True
            failures = 0
            alerts.clear("errors")
            log.info("cycle done, %d new slot(s)", new)
        except StopPolling as exc:
            log.info("stopping: %s", exc)
            return 0
        except LoginRejected as exc:
            failures += 1  # back off so bad credentials are not retried at full rate
            log.warning("login rejected (%d in a row): %s", failures, exc)
        except (requests.RequestException, PortalError) as exc:
            failures += 1
            log.warning("cycle failed (%d in a row): %s", failures, exc)
            portal.logged_in = False
            if failures >= 5:
                alerts.raise_("errors", f"{failures} consecutive failures. Last error: {exc}")
        except (smtplib.SMTPException, OSError) as exc:
            log.error("email failed, will retry next cycle: %s", exc)
        except Exception as exc:  # unexpected shape from the portal must not kill the loop
            failures += 1
            log.exception("unexpected error in cycle (%d in a row)", failures)
            portal.logged_in = False
            if failures >= 5:
                alerts.raise_("errors", f"{failures} consecutive failures. Last error: {exc!r}")

        if args.once:
            return 0 if cycle_ok else 1
        if failures == 1:
            delay = 120  # quick retry: the network is often just not back yet after sleep
        else:
            delay = min(base_delay * (2 ** min(failures - 1, 4)), 6 * 3600)
        delay += random.uniform(0, 0.1 * delay)
        log.info("next poll in %.0f min", delay / 60)
        _time.sleep(delay)

        # Re-read the config so credentials, filter, SMTP settings or the interval
        # can be fixed in place without restarting the poller.
        try:
            fresh = Config.load(args.config)
        except ConfigError as exc:
            log.warning("config reload failed, keeping previous values: %s", exc)
            continue
        if fresh != cfg:
            log.info("config changed, applying new values")
            cfg = fresh
            portal.candidate_id, portal.birth_date = cfg.candidate_id, cfg.birth_date
            portal.logged_in = False
            flt = SlotFilter(cfg.notify_before, cfg.locations, cfg.notify_after, cfg.dates)
            notifier.smtp = cfg.smtp
            base_delay = cfg.poll_interval_minutes * 60


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        print("stopped", file=sys.stderr)
        sys.exit(130)
