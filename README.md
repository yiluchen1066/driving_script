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

Keep it alive with `tmux` or `nohup`, for example:

```bash
nohup .venv/bin/python exam_poller.py >> poller.log 2>&1 &
```

The config is re-read every cycle, so credentials, the filter, SMTP settings
or the interval can be fixed in place without a restart. Logs go to stdout.

The script emails you once per new slot set, and once per problem (captcha
switched on, service blocked, login rejected, filter matching no location,
repeated request failures) until that problem clears. Problem memory lives in
the process, so `--once` under cron re-sends a standing problem on every run
and does not back off; prefer the loop for unattended use. `--once` exits 1
when the cycle failed.

## Tests

```bash
.venv/bin/pytest -q
```
