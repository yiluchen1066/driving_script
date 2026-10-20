from datetime import date

from exam_poller import Slot, SlotFilter, normalise


def slot(d: date, time: str = "08:00", location: str = "Zürich-Albisgüetli") -> Slot:
    return Slot(date=d, time=time, location=location)


def test_normalise_makes_umlaut_spellings_equal():
    assert normalise("Albisgüetli") == normalise("Albisguetli") == normalise("Albisgueetli")
    assert normalise("Zürich") == normalise("Zuerich") == normalise("ZURICH")
    assert normalise("Straße") == normalise("strasse")


def test_normalise_handles_combining_diaeresis():
    assert normalise("Albisgüetli") == normalise("Albisgüetli")


def test_normalise_keeps_distinct_names_distinct():
    assert normalise("Winterthur") != normalise("Albisgüetli")
    assert normalise("Bülach") != normalise("Zürich")


def test_location_substring_match_ignores_umlaut_and_case():
    f = SlotFilter(None, ["Albisguetli"])
    assert f.location_matches("Zürich-Albisgüetli")
    assert f.location_matches("ALBISGÜETLI")
    assert not f.location_matches("Winterthur")


def test_empty_location_list_matches_everything():
    assert SlotFilter(None, []).location_matches("Winterthur")


def test_cutoff_is_inclusive():
    f = SlotFilter(date(2026, 11, 30), [])
    assert f.slot_matches(slot(date(2026, 11, 30)))
    assert not f.slot_matches(slot(date(2026, 12, 1)))


def test_start_date_is_inclusive():
    f = SlotFilter(None, [], notify_after=date(2026, 11, 30))
    assert not f.slot_matches(slot(date(2026, 11, 29)))
    assert f.slot_matches(slot(date(2026, 11, 30)))
    assert f.slot_matches(slot(date(2026, 12, 1)))


def test_start_and_end_dates_are_both_inclusive():
    f = SlotFilter(date(2026, 11, 30), [], notify_after=date(2026, 11, 1))
    assert not f.slot_matches(slot(date(2026, 10, 31)))
    assert f.slot_matches(slot(date(2026, 11, 1)))
    assert f.slot_matches(slot(date(2026, 11, 30)))
    assert not f.slot_matches(slot(date(2026, 12, 1)))


def test_no_cutoff_passes_any_date():
    assert SlotFilter(None, []).slot_matches(slot(date(2030, 1, 1)))


def test_apply_filters_and_sorts_by_date_then_time():
    f = SlotFilter(date(2026, 11, 30), ["Albisguetli"])
    slots = [
        slot(date(2026, 11, 20), "10:30"),
        slot(date(2026, 11, 20), "08:00"),
        slot(date(2026, 11, 10), "14:00"),
        slot(date(2026, 12, 5), "08:00"),
        slot(date(2026, 11, 15), "08:00", location="Winterthur"),
    ]
    out = f.apply(slots)
    assert [(s.date, s.time) for s in out] == [
        (date(2026, 11, 10), "14:00"),
        (date(2026, 11, 20), "08:00"),
        (date(2026, 11, 20), "10:30"),
    ]


def test_key_is_stable_and_unique_per_slot():
    a = slot(date(2026, 11, 20), "08:00")
    b = slot(date(2026, 11, 20), "08:30")
    assert a.key == "Zürich-Albisgüetli|2026-11-20|08:00"
    assert a.key != b.key
