from pathlib import Path

from src.humble_browser import (
    extract_key_from_detail_html,
    find_action_id,
    group_into_orders,
    parse_flight_payload,
    parse_library_results,
    parse_login_env,
)

FIX = Path(__file__).parent / "fixtures"
K1 = "11111111-0000-0000-0000-000000000001"


def _keys():
    payload = parse_flight_payload((FIX / "humble_library_action.txt").read_text())
    return payload, parse_library_results(payload["results"])


def test_download_only_tiles_skipped_and_counts():
    payload, keys = _keys()
    assert payload["pagination"]["totalDocs"] == 5
    assert [k["human_name"] for k in keys] == [
        "Revealed Game", "Unrevealed Game", "Gifted Game", "GOG Game"]


def test_revealed_flag_is_presence_of_key():
    _, keys = _keys()
    by = {k["human_name"]: k for k in keys}
    assert by["Revealed Game"]["redeemed_key_val"] == "XXXXX-XXXXX-XXXXX"
    assert "redeemed_key_val" not in by["Unrevealed Game"]
    assert by["Gifted Game"]["is_gift"] is True
    assert by["Revealed Game"]["is_gift"] is False


def test_steam_has_app_id_key_none_and_gog_does_not():
    _, keys = _keys()
    by = {k["human_name"]: k for k in keys}
    assert "steam_app_id" in by["Revealed Game"] and by["Revealed Game"]["steam_app_id"] is None
    assert "steam_app_id" not in by["GOG Game"]
    assert by["GOG Game"]["key_type_human_name"] == "gog"
    assert by["Revealed Game"]["detail_url"].endswith("/home/library/content/" + K1)
    assert by["Revealed Game"]["key_index"] == 0


def test_group_into_orders_shape():
    _, keys = _keys()
    orders = group_into_orders(keys, {"98346460-9cbf-4b5c-8429-6bc1497a92f6": "Bundle"})
    assert len(orders) == 2
    first = orders[0]
    assert first["product"]["human_name"] == "Bundle"
    assert len(first["tpkd_dict"]["all_tpks"]) == 3


def test_extract_key_from_detail_html():
    html = (FIX / "humble_detail.html").read_text()
    assert extract_key_from_detail_html(html, K1) == "XXXXX-XXXXX-XXXXX"
    assert extract_key_from_detail_html(html, "nope") == ""


def test_find_action_id():
    js = 'x=(0,a.createServerReference)("60aa11",rn.callServer,void 0,rn.findSourceMapURL,"filterUserLibrary")'
    assert find_action_id(js, "filterUserLibrary") == "60aa11"
    assert find_action_id(js, "other") is None


def test_parse_login_env_keeps_special_chars():
    env = parse_login_env("USERNAME=a@b.c\nPASSWORD=p$w'd=\"x#!`\n")
    assert env == {"USERNAME": "a@b.c", "PASSWORD": "p$w'd=\"x#!`"}
