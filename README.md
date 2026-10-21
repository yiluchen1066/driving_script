# Exam slot poller

Polls the Strassenverkehrsamt Zürich appointment portal for free practical
driving-exam slots and emails you when a new slot appears at the exam
location you care about (default: Albisgütli). Checks once an hour, remembers
what it already told you, and emails you once per problem if something stops
working (captcha switched on, login rejected, mail failure).

Works on macOS, Linux and Windows. Needs Python 3.11 or newer.

## 1. Install

macOS / Linux (Terminal):

```bash
git clone https://github.com/yiluchen1066/driving_script.git
cd driving_script
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
cp config.example.json config.json
```

Windows (PowerShell, with Python from python.org installed):

```powershell
git clone https://github.com/yiluchen1066/driving_script.git
cd driving_script
py -3 -m venv .venv
.venv\Scripts\pip install -r requirements.txt
copy config.example.json config.json
```

Every command below is run from inside the `driving_script` folder. On
macOS/Linux use `.venv/bin/python`, on Windows use `.venv\Scripts\python`.

## 2. Configure `config.json`

`config.json` holds your credentials and is ignored by git. Never put real
values into `config.example.json`, which is committed.

| Field | Value |
|---|---|
| `credentials.candidate_id` | the number you type into the portal login, digits only |
| `credentials.birth_date` | `dd.mm.yyyy` |
| `filter.locations` | exam locations to watch, e.g. `["Albisguetli"]`; spelling with or without umlaut both work; `[]` = all locations |
| `filter.notify_after` | only alert for slots on or after this date, `dd.mm.yyyy`; `""` = no limit |
| `filter.notify_before` | only alert for slots on or before this date, `dd.mm.yyyy`; `""` = no limit |
| `filter.dates` | optional list of specific days, each `{"date": "dd.mm.yyyy", "from": "HH:MM", "to": "HH:MM"}`; `from`/`to` are optional and inclusive, omit both for the whole day; when the list is non-empty only slots on these days and times are reported or booked; see example below |
| `smtp.host` / `smtp.port` / `smtp.starttls` | for Gmail: `smtp.gmail.com`, `587`, `true` |
| `smtp.user` / `smtp.from` | the Gmail address that sends |
| `smtp.password` | a Gmail App Password (see below), not your normal password |
| `smtp.to` | where the alerts go; can be any address |
| `poll_interval_minutes` | how often to check, default `60` |
| `max_weeks` | maximum search horizon in weeks from today or `notify_after`, default `12` |
| `auto_book` | `true` books the earliest matching slot automatically, see below; default `false` |

Gmail App Password: open https://myaccount.google.com/security and turn on
2-Step Verification, then open https://myaccount.google.com/apppasswords,
create one named for example "exam poller", and paste the 16-letter code into
`smtp.password`. Spaces in the code can stay or go.

The config is re-read before every poll, so you can edit it while the
poller runs. No restart needed.

Example of specific days and time ranges, combined with the location filter:

```json
"filter": {
  "locations": ["Albisguetli"],
  "notify_after": "",
  "notify_before": "",
  "dates": [
    {"date": "20.11.2026", "to": "15:00"},
    {"date": "27.11.2026", "from": "13:00", "to": "18:00"},
    {"date": "04.12.2026"},
    {"date": "11.12.2026", "to": "12:00"}
  ]
}
```

With `dates` set the search only covers the span between the first and last
listed day, so fewer requests are made per poll.

## 3. Test the email

Sends one email in exactly the format of a real alert, tagged `[TEST]`:

```bash
.venv/bin/python exam_poller.py --test-email          # macOS / Linux
.venv\Scripts\python exam_poller.py --test-email      # Windows
```

Prints `test email sent to ...` on success, otherwise the SMTP error.
"535 Username and Password not accepted" from Gmail means the password is
not an App Password.

## 4. Test the portal access

Logs in, looks up the slots, prints what it would email, sends nothing:

```bash
.venv/bin/python exam_poller.py --once --dry-run -v
```

You should see `logged in`, one line per watched location with a slot count,
and `cycle done`. A `login rejected with HTTP 401` means `candidate_id` or
`birth_date` is wrong.

## 5. Start and stop

The poller is a long-running process. The machine must stay on and awake:
while it sleeps no polling happens. On Windows set Settings > System > Power >
Sleep to Never when plugged in (screen off is fine); on a Mac enable "Prevent
automatic sleeping on power adapter when the display is off" under Battery >
Options. The terminal or window it was started from can be closed when started
as shown here. A failed poll is retried after two minutes, then with
increasing gaps up to six hours.

macOS / Linux:

```bash
nohup .venv/bin/python exam_poller.py >> poller.log 2>&1 &
tail -f poller.log              # watch
pkill -f exam_poller.py         # stop
```

Windows (PowerShell):

```powershell
$p = Start-Process -FilePath .\.venv\Scripts\python.exe -ArgumentList "exam_poller.py" `
       -WorkingDirectory (Get-Location) -WindowStyle Hidden -PassThru `
       -RedirectStandardOutput poller.log -RedirectStandardError poller.err.log
$p.Id | Out-File poller.pid
Get-Content poller.log -Wait             # watch
Stop-Process -Id (Get-Content poller.pid) # stop
```

If `poller.pid` is lost, stop it via Task Manager (a `python.exe` whose
command line contains `exam_poller.py`) or with:

```powershell
Get-CimInstance Win32_Process | Where-Object { $_.CommandLine -like "*exam_poller.py*" } | ForEach-Object { Stop-Process -Id $_.ProcessId }
```

Is it running? Look for a fresh `next poll in ... min` line at the end of
`poller.log`.

## 6. What you will receive

- A slot email whenever new matching slots appear, subject
  `Exam slots: N new, earliest dd.mm.yyyy`, listing date, time and location.
  Slots already reported are not repeated; a slot that disappears and comes
  back is reported again.
- A problem email, once per problem until it clears: captcha switched on by
  the portal, portal service blocked, login rejected, no location matching
  your filter, five failed polls in a row. The poller keeps running and backs
  off up to six hours between attempts while a problem persists.

Booking itself is not automated: open the portal link in the email and book.

## Automatic booking

Off by default. With `"auto_book": true` in `config.json` the poller books the
earliest slot that matches your filter, in the same cycle it finds it, with no
further confirmation. Read this before switching it on:

- **Hard rule:** a slot within 10 days of today is never booked. Such slots are
  still emailed, just not booked.
- The booking is real and immediate. The log shows `BOOKING <slot> in 10
  seconds` first; stop the poller in that window to abort.
- On success you get an email with subject `BOOKED: ...`, the booking is
  written to `state.json`, and the poller stops. It will not book a second time
  while that record exists, even if restarted with the flag still on.
- On failure you get one email with the portal's message and the poller keeps
  polling.
- `--dry-run` never books; it prints `Would book: ...` instead.
- After booking, log in to the portal yourself to check the appointment and
  print the convocation, which the portal requires within a few days.
- Cancelling or moving a booked appointment is not automated; do that in the
  portal.

The flag is read every cycle, so it can be switched on or off while the poller
runs.

## Other flags

| Flag | Purpose |
|---|---|
| `--once` | one poll and exit; exit code 1 if the poll failed |
| `--dry-run` | print emails instead of sending |
| `--discover` | save the raw portal responses under `discovery/` for debugging |
| `--config PATH`, `--state PATH` | use other file locations |
| `-v` | debug logging |

Files created while running: `state.json` (slots already reported),
`poller.log`. Both are ignored by git.

## Development

```bash
.venv/bin/pytest -q
```
