import json
from datetime import date

from exam_poller import Slot, State


def s(day: int, time: str = "08:00") -> Slot:
    return Slot(date=date(2026, 11, day), time=time, location="Albisgüetli")


def test_fresh_state_reports_everything_new(tmp_path):
    st = State(tmp_path / "state.json")
    assert st.new_slots([s(1), s(2)]) == [s(1), s(2)]


def test_saved_slots_are_not_new_next_time(tmp_path):
    st = State(tmp_path / "state.json")
    st.save([s(1), s(2)])
    assert st.new_slots([s(1), s(2), s(3)]) == [s(3)]


def test_slot_that_disappears_and_returns_is_new_again(tmp_path):
    st = State(tmp_path / "state.json")
    st.save([s(1)])
    st.save([])  # slot vanished this cycle
    assert st.new_slots([s(1)]) == [s(1)]


def test_corrupt_state_file_is_treated_as_empty(tmp_path):
    p = tmp_path / "state.json"
    p.write_text("{truncated")
    assert State(p).load() == set()
    p.write_text("[1, 2, 3]")
    assert State(p).load() == set()


def test_save_writes_sorted_keys_and_timestamp(tmp_path):
    p = tmp_path / "state.json"
    State(p).save([s(2), s(1)])
    data = json.loads(p.read_text())
    assert data["notified"] == [s(1).key, s(2).key]
    assert "updated" in data
