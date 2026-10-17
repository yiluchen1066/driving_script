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
    assert "Book at: https://portal.stva.zh.ch/ecari-dispoweb/ui/app/init/#/conduite/prive/login" in body


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
