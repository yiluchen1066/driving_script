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
