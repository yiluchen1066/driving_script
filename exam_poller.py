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
from dataclasses import dataclass
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
        except (ValueError, OSError) as exc:  # bad JSON, truncated UTF-8, unreadable path
            log.warning("state file %s is unreadable (%s), starting fresh", self.path, exc)
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
            slots, has_next = parse_week(raw, name)
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
            if until is not None and base > until:
                break
            max_date = raw.get("maxDate")
            if max_date is not None and base > parse_portal_date(max_date):
                break
        return out

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


def parse_week(raw: dict, location: str) -> tuple[list[Slot], bool]:
    code = raw.get("retCode")
    if code is not None and str(code) != "0000":
        raise PortalError(f"portal returned retCode {code}: {raw.get('retMessage')}")
    slots: list[Slot] = []
    for entry in raw.get("listeJourHeureLibreDispo") or []:
        day = parse_portal_date(entry["jour"])
        hour = int(entry.get("heure") or 0)
        minute = int(entry.get("minute") or 0)
        slots.append(Slot(date=day, time=f"{hour:02d}:{minute:02d}", location=location))
    return slots, bool(raw.get("nextWeek"))


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
            slots = portal.free_slots(cat, loc, start=today, until=flt.notify_before,
                                      max_weeks=cfg.max_weeks)
            log.info("%s: %d free slot(s) found", name, len(slots))
            found.extend(slots)

    if flt.locations and matched_locations == 0:
        alerts.raise_("filter", f"No exam location matched filter.locations={cfg.locations}. "
                                f"The portal lists: {', '.join(seen_names) or 'nothing'}. "
                                "Fix the filter in the config.")
    else:
        alerts.clear("filter")

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
    flt = SlotFilter(cfg.notify_before, cfg.locations)
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
        delay = min(base_delay * (2 ** min(failures, 4)), 6 * 3600)
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
            flt = SlotFilter(cfg.notify_before, cfg.locations)
            notifier.smtp = cfg.smtp
            base_delay = cfg.poll_interval_minutes * 60


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        print("stopped", file=sys.stderr)
        sys.exit(130)
