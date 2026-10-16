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


# --- behaviour pinned by the real portal responses captured with --discover ---

import json
from pathlib import Path

from exam_poller import PortalError, SlotFilter, portal_datetime, type_body

FIXTURES = Path(__file__).parent / "fixtures"


def ms(d: date) -> int:
    """Epoch milliseconds for midnight Zurich on `d`, as the portal encodes dates."""
    from datetime import datetime
    from exam_poller import ZURICH
    return int(datetime.combine(d, datetime.min.time(), tzinfo=ZURICH).timestamp() * 1000)


def test_location_name_uses_libelleLieu_and_skips_id_fields():
    real = {"defaut": True, "idLieuExamen": "AL", "libelleLieu": "Albisgütli", "maxDate": 1799881200401}
    assert location_name(real) == "Albisgütli"
    assert location_name({"idLieuExamen": "AL", "something": "Fallback"}) == "Fallback"


def test_parse_week_raises_on_error_retcode():
    with pytest.raises(PortalError, match="9999.*boom"):
        parse_week({"retCode": "9999", "retMessage": "boom", "listeJourHeureLibreDispo": []}, "X")


def test_real_week_fixture_parses():
    raw = json.loads((FIXTURES / "week.json").read_text(encoding="utf-8"))
    slots, has_next = parse_week(raw, "Bassersdorf")
    assert slots == [Slot(date(2026, 10, 30), "15:00", "Bassersdorf")]
    assert has_next is True


def test_real_locations_fixture_names_and_filter():
    locs = json.loads((FIXTURES / "locations.json").read_text(encoding="utf-8"))
    names = [location_name(l) for l in locs]
    assert names == ["Albisgütli", "Bülach", "Bassersdorf", "Hinwil", "Regensdorf", "Winterthur"]
    f = SlotFilter(None, ["Albisguetli"])
    assert [n for n in names if f.location_matches(n)] == ["Albisgütli"]


def test_real_category_fixture_builds_request_body():
    cat = json.loads((FIXTURES / "categories.json").read_text(encoding="utf-8"))[0]
    body = type_body(cat)
    assert body["typeCategorieLieuDispo"] == {
        "idGroupeExamen": None, "idCategorie": "B", "idTypeExamen": "PRATIQUEAUTO", "idGenreExamen": "01PAL",
    }
    assert body["rendezVous"] is None


def served_week(days: list[date], week_day: date, has_next: bool = True, max_date: date | None = None) -> FakeResponse:
    payload = {
        "retCode": "0000", "retMessage": None,
        "listeJourHeureLibreDispo": [{"jour": ms(d), "heure": 8, "minute": 0} for d in days],
        "nextWeek": has_next, "prevWeek": False, "weekDay": ms(week_day),
    }
    if max_date is not None:
        payload["maxDate"] = ms(max_date)
    return FakeResponse(200, payload)


def test_free_slots_advances_from_the_week_the_server_actually_served():
    # The portal clamps the requested week to a minimum lead time and reports the
    # served week in `weekDay`; the next request must be seven days after that.
    sess = FakeSession({WEEK: [
        served_week([date(2026, 10, 30)], week_day=date(2026, 10, 30)),
        served_week([], week_day=date(2026, 11, 6)),
    ]})
    p = Portal("1", "01.01.1990", session=sess)
    p.logged_in = True
    slots = p.free_slots(CAT, LOC, start=date(2026, 10, 19), until=None, max_weeks=2)
    assert [s.date for s in slots] == [date(2026, 10, 30)]
    bases = [c[2]["typeCategorieLieuDispo"]["dateBase"] for c in sess.calls]
    assert bases == [portal_datetime(date(2026, 10, 19)), portal_datetime(date(2026, 11, 6))]


def test_free_slots_stops_past_the_servers_max_date():
    sess = FakeSession({WEEK: [served_week([], week_day=date(2026, 10, 30), max_date=date(2026, 11, 1))]})
    p = Portal("1", "01.01.1990", session=sess)
    p.logged_in = True
    p.free_slots(CAT, LOC, start=date(2026, 10, 19), until=None, max_weeks=12)
    assert len(sess.calls) == 1


def test_free_slots_dedupes_a_week_served_twice():
    sess = FakeSession({WEEK: [
        served_week([date(2026, 10, 30)], week_day=date(2026, 10, 30)),
        served_week([date(2026, 10, 30)], week_day=date(2026, 10, 30)),
    ]})
    p = Portal("1", "01.01.1990", session=sess)
    p.logged_in = True
    slots = p.free_slots(CAT, LOC, start=date(2026, 10, 19), until=None, max_weeks=2)
    assert len(slots) == 1
