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
