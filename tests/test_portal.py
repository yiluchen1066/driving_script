import json
from datetime import date

import pytest

from exam_poller import HEADERS, LoginRejected, Portal, PortalError, portal_datetime, type_body
from tests.fakes import FakeResponse, FakeSession

LOGIN = ("POST", "/login/loginConduitePrive/de")
PARAM = ("GET", "/param/load")
CATS = ("GET", "/personne/listCatPossible")
LIEUX = ("POST", "/lieu-examen/listeLieu")
WEEK = ("POST", "/lieu-examen/listeJourHeureLibre")

CAT = {"idGroupeExamen": 1, "categorie": "B", "idTypeExamen": 2, "idGenreExamen": 3, "rendezVous": 99}
LOC = {"idLieuExamen": 7, "nom": "Zürich-Albisgüetli"}
PARAMS_OFF = {"captchaParam": {"conduitePublic": False}, "blocking": {"conduitePublic": False}}


def make(routes) -> tuple[Portal, FakeSession]:
    sess = FakeSession(routes)
    return Portal("123456789", "12.09.1985", session=sess), sess


def test_headers_look_like_a_browser():
    p, sess = make({})
    assert "Chrome" in sess.headers["User-Agent"]
    assert sess.headers["Referer"].startswith("https://portal.stva.zh.ch/ecari-dispoweb/ui/app/init/")
    assert sess.headers["Origin"] == "https://portal.stva.zh.ch"
    assert sess.headers["Accept"] == "application/json"
    assert HEADERS["Content-Type"] == "application/json"


def test_check_params_reads_flags():
    p, _ = make({PARAM: [FakeResponse(200, {"captchaParam": {"conduitePublic": True},
                                           "blocking": {"conduitePublic": False}})]})
    assert p.check_params() == (True, False)


def test_login_posts_expected_body_and_sets_logged_in():
    p, sess = make({LOGIN: [FakeResponse(200, {"personneDispo": {"nom": "X"}, "examTypes": []})]})
    data = p.login()
    assert p.logged_in is True
    assert data["personneDispo"] == {"nom": "X"}
    method, path, body = sess.calls[0]
    assert (method, path) == LOGIN
    assert body == {"noRegistre": "123456789", "dateNaissance": "12.09.1985", "captcha": ""}


@pytest.mark.parametrize("status", [401, 403])
def test_login_rejected(status):
    p, _ = make({LOGIN: [FakeResponse(status, {"personneDispo": None})]})
    with pytest.raises(LoginRejected) as info:
        p.login()
    assert info.value.status == status
    assert p.logged_in is False


def test_non_json_response_is_portal_error():
    p, _ = make({PARAM: [FakeResponse(200, None, text="<html>Request Rejected")]})
    with pytest.raises(PortalError, match="non-JSON"):
        p.check_params()


def test_expired_session_relogins_once_and_retries():
    p, sess = make({
        LOGIN: [FakeResponse(200, {"personneDispo": {}})],
        CATS: [FakeResponse(401, {}), FakeResponse(200, [CAT])],
    })
    p.login()
    assert p.categories() == [CAT]
    assert sess.paths() == [LOGIN, CATS, LOGIN, CATS]


def test_second_401_surfaces_as_error_not_loop():
    p, sess = make({
        LOGIN: [FakeResponse(200, {"personneDispo": {}})],
        CATS: [FakeResponse(401, {})],
    })
    p.login()
    with pytest.raises(PortalError, match="401"):
        p.categories()
    assert sess.paths().count(LOGIN) == 2


def test_type_body_maps_category_fields():
    assert type_body(CAT) == {
        "typeCategorieLieuDispo": {"idGroupeExamen": 1, "idCategorie": "B", "idTypeExamen": 2, "idGenreExamen": 3},
        "rendezVous": 99,
    }
    assert type_body(CAT, idLieuExamen=7)["typeCategorieLieuDispo"]["idLieuExamen"] == 7


def test_portal_datetime_mimics_browser_utc_json():
    # Noon Zurich on 2026-10-20 (CEST, UTC+2) is 10:00 UTC.
    assert portal_datetime(date(2026, 10, 20)) == "2026-10-20T10:00:00.000Z"
    # Winter: CET, UTC+1.
    assert portal_datetime(date(2026, 12, 20)) == "2026-12-20T11:00:00.000Z"


def test_locations_and_free_week_send_expected_bodies():
    p, sess = make({
        LOGIN: [FakeResponse(200, {})],
        LIEUX: [FakeResponse(200, [LOC])],
        WEEK: [FakeResponse(200, {"listeJourHeureLibreDispo": [], "nextWeek": False})],
    })
    p.login()
    assert p.locations(CAT) == [LOC]
    assert sess.calls[-1][2] == type_body(CAT)
    p.free_week(CAT, 7, date(2026, 10, 20))
    body = sess.calls[-1][2]
    assert body["typeCategorieLieuDispo"]["idLieuExamen"] == 7
    assert body["typeCategorieLieuDispo"]["dateBase"] == "2026-10-20T10:00:00.000Z"
    assert body["rendezVous"] == 99


def test_discover_writes_each_response(tmp_path):
    p, _ = make({
        PARAM: [FakeResponse(200, PARAMS_OFF)],
        LOGIN: [FakeResponse(200, {"personneDispo": {"nom": "X"}})],
        CATS: [FakeResponse(200, [CAT])],
        LIEUX: [FakeResponse(200, [LOC])],
        WEEK: [FakeResponse(200, {"listeJourHeureLibreDispo": [], "nextWeek": False})],
    })
    files = p.discover(tmp_path)
    names = [f.name for f in files]
    assert names == ["0_param.json", "1_login.json", "2_categories.json",
                     "3_locations_cat0.json", "4_week_cat0_loc0.json"]
    assert json.loads((tmp_path / "2_categories.json").read_text()) == [CAT]
