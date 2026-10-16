# Exam slot poller — design

Date: 2026-10-16
Status: approved in chat (brainstorming), ready for planning

## Goal

A Python script that logs into the Strassenverkehrsamt Zürich appointment portal
(`https://portal.stva.zh.ch/ecari-dispoweb/`), checks for free practical
driving-exam slots, and emails the user when a matching slot appears.
Credentials and SMTP settings are read from a gitignored JSON file and never
appear in code.

## Findings from the feasibility spike (2026-10-16)

- The portal is an Angular SPA over a JSON backend. Sessions are plain cookies
  (`JSESSIONID` plus F5 `TS*` cookies). No CSRF token, no JavaScript challenge.
- An F5 web application firewall rejects requests with a bare user agent and
  accepts requests carrying a full Chrome user agent and a `Referer`.
- The login form has a Google reCAPTCHA wired in, but `GET /param/load`
  currently reports `captchaParam.conduitePublic = false`. The canton can flip
  this at any time; the script must detect it and alert.
- A dummy login returned HTTP 401 with a JSON body, confirming endpoint and
  payload shape.

## Portal API (relative to `/ecari-dispoweb`)

| Step | Call | Notes |
|---|---|---|
| 0 | `GET /param/load` | Public. `captchaParam.conduitePublic`, `blocking.conduitePublic`. |
| 1 | `POST /login/loginConduitePrive/de` | Body `{"noRegistre": "<digits>", "dateNaissance": "dd.mm.yyyy", "captcha": ""}`. 401 = bad credentials, 403 = blocked. Response has `personneDispo`, `examTypes`. |
| 2 | `GET /personne/listCatPossible` | Candidate's exam categories: `idGroupeExamen`, `categorie`, `idTypeExamen`, `idGenreExamen`, `rendezVous`. |
| 3 | `POST /lieu-examen/listeLieu` | Body `{"typeCategorieLieuDispo": {idGroupeExamen, idCategorie, idTypeExamen, idGenreExamen}, "rendezVous": ...}`. Returns exam locations (`idLieuExamen`, name). |
| 4 | `POST /lieu-examen/listeJourHeureLibre` | Body adds `dateBase` and `idLieuExamen`. Returns free days and times. Polling target. |
| — | `GET /login/restoreUserSession/{appName}` | Exists; optional for session restore. |

Exact response shapes for steps 2–4 are unknown until an authenticated run.
A `--discover` mode captures them (see below).

Required request headers: a full Chrome `User-Agent`,
`Referer: https://portal.stva.zh.ch/ecari-dispoweb/ui/app/init/`,
`Origin: https://portal.stva.zh.ch`, `Content-Type: application/json`,
`Accept: application/json`.

## Files

| Path | Purpose | Git |
|---|---|---|
| `exam_poller.py` | The script. Classes `Portal`, `SlotFilter`, `Notifier`, `State`, plus `main()`. | committed |
| `config.example.json` | Template with placeholder values. | committed |
| `config.json` | Real credentials, SMTP, filter, interval. | gitignored |
| `state.json` | Slot keys already emailed. | gitignored |
| `discovery/` | Raw JSON captured by `--discover`. Contains personal data. | gitignored |
| `requirements.txt` | `requests`. | committed |
| `.gitignore` | `config.json`, `state.json`, `discovery/`, `.venv/`, `__pycache__/`. | committed |
| `tests/test_poller.py` | Unit tests. | committed |
| `tests/fixtures/` | Sanitised copies of discovered responses. | committed |
| `README.md` | Setup and run instructions. | committed |

## Config shape

```json
{
  "credentials": {"candidate_id": "123456789", "birth_date": "12.09.1985"},
  "filter": {"notify_before": "", "locations": ["Albisguetli"]},
  "smtp": {"host": "smtp.example.ch", "port": 587, "user": "", "password": "",
           "from": "me@example.ch", "to": "me@example.ch", "starttls": true},
  "poll_interval_minutes": 60
}
```

- `candidate_id` is pure digits (the portal's `noRegistre`).
- `birth_date` is `dd.mm.yyyy`, passed to the portal unchanged.
- `notify_before` empty means no date limit. Otherwise `dd.mm.yyyy`, inclusive.
- `locations` empty means any location. Otherwise each entry is matched as a
  case-insensitive substring of the portal's location name, after normalising
  `ä/ö/ü` to `ae/oe/ue` on both sides, so `Albisguetli` matches `Albisgüetli`.
- `poll_interval_minutes` default 60. A small random jitter (up to 10 %) is
  added each cycle.

Config loading validates presence and format of every field and exits with a
clear message on error.

## Components

### Portal

Wraps a `requests.Session` with the headers above.

- `check_params()` → returns `(captcha_on, blocked)` from `GET /param/load`.
- `login()` → POST credentials. Raises `LoginRejected(status)` on 401/403.
- `categories()`, `locations(category)`, `free_slots(category, location)` →
  parsed results. Parsers are written from discovery output.
- Any 401 on steps 2–4 triggers exactly one re-login and retry.
- `discover(out_dir)` → runs steps 0–4 once and writes each raw response to
  `discovery/<step>.json` for parser development. Prints the file list.

### SlotFilter

Pure function of config and a slot list. A slot is
`Slot(location: str, date: date, time: str, raw: dict)`. Key for dedupe is
`(location, date.isoformat(), time)`.

### State

Reads and writes `state.json` holding the list of keys from the previous
cycle's matches. `new = matches - previous`; after notifying, the file is
overwritten with the current matches. A slot that disappears and reappears
triggers a fresh email.

### Notifier

`smtplib` with STARTTLS by default (SSL on port 465 if `starttls` is false).
Two kinds of email:

- Slot email: one per cycle with new matches, subject
  `Exam slots: N new, earliest dd.mm.yyyy`, plain-text body listing
  location, date, time, sorted by date.
- Operational email: sent once per condition and again only after the
  condition clears and recurs. Conditions: captcha switched on, service
  blocked, login rejected (401/403), five consecutive request failures.

`--dry-run` prints emails to stdout instead of sending.

### main loop

```
load config
loop:
    captcha/blocked check → operational email if needed, skip cycle
    ensure logged in
    matches = filter(all free slots over filtered locations)
    new = state.diff(matches)
    if new: send slot email
    state.save(matches)
    sleep(interval + jitter)
```

Network and HTTP 5xx errors: log, back off exponentially from the configured
interval up to 6 hours, reset on success. Logging to stdout with timestamps.

CLI flags: `--config PATH` (default `config.json`), `--once`, `--dry-run`,
`--discover`.

## Error handling summary

| Situation | Behaviour |
|---|---|
| Config missing or malformed | exit 2 with message |
| Captcha on or service blocked | operational email once, keep polling |
| Login 401/403 | operational email once, keep polling (credentials may be fixed in place) |
| 401 mid-cycle | one re-login, retry once |
| Network error / 5xx | log, exponential backoff |
| SMTP failure | log, keep running, retry next cycle (state not saved so slots re-alert) |

## Testing

- Unit tests with fixture JSON: config validation, umlaut-normalised location
  match, date cutoff, dedupe across cycles, email body formatting.
- Portal parsers tested against sanitised fixtures copied from discovery.
- Manual: `--discover`, then `--once --dry-run`.

## Implementation order

1. Skeleton: config loading, `Portal` with headers, `check_params`, `login`,
   `discover`; `State`, `SlotFilter`, `Notifier`; tests for everything that
   does not depend on unknown response shapes.
2. User runs `--discover` with real credentials.
3. Parsers and fixtures from discovery output; wire the full cycle; README.

## Out of scope

Automatic booking (`/rendez-vous/choixRdv`, `/rendez-vous/priseRdv`), other
cantons, other exam types.
