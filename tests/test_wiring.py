"""Wiring tests using a fake HumbleBrowser (no network, no real browser).

Runs under pytest or directly: ``python tests/test_wiring.py``.
"""

import contextlib
import csv
import os
import sys
import tempfile
import types
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src import export as export_mod  # noqa: E402
from src import __main__ as main_mod  # noqa: E402
from src.export import EXPORT_KEY_HEADERS  # noqa: E402
from src.humble_api import redeem_humble_key  # noqa: E402


class HumbleLoginRequired(RuntimeError):
    pass


def _orders():
    return [
        {
            "gamekey": "ORDER1",
            "product": {"human_name": "Bundle One"},
            "tpkd_dict": {
                "all_tpks": [
                    {"human_name": "Revealed Game", "key_type_human_name": "Steam",
                     "steam_app_id": 111, "redeemed_key_val": "RRRRR-RRRRR-RRRRR",
                     "is_gift": False, "is_expired": False, "key_index": 0},
                    {"human_name": "Hidden Game", "key_type_human_name": "Steam",
                     "steam_app_id": 222, "is_gift": False, "is_expired": False,
                     "key_index": 1},
                    {"human_name": "GOG Game", "key_type_human_name": "GOG",
                     "redeemed_key_val": "GOGKEY", "is_gift": False,
                     "is_expired": False, "key_index": 2},
                ]
            },
        }
    ]


class FakeHumbleBrowser:
    login_required = False
    last = None

    def __init__(self, *a, **kw):
        self.closed = False
        self.reveals = []
        self.interactive = None
        FakeHumbleBrowser.last = self

    def ensure_logged_in(self, *, interactive):
        self.interactive = interactive
        if FakeHumbleBrowser.login_required:
            raise HumbleLoginRequired("expired")

    def fetch_order_details(self):
        return _orders(), []

    def reveal_key(self, tpk):
        self.reveals.append(tpk["human_name"])
        return "AAAAA-BBBBB-CCCCC"

    def close(self):
        self.closed = True


@contextlib.contextmanager
def _env(login_required=False):
    """Fake src.humble_browser module, temp cwd, restored stderr."""
    fake = types.ModuleType("src.humble_browser")
    fake.HumbleBrowser = FakeHumbleBrowser
    fake.HumbleLoginRequired = HumbleLoginRequired
    FakeHumbleBrowser.login_required = login_required
    old_mod = sys.modules.get("src.humble_browser")
    sys.modules["src.humble_browser"] = fake
    old_cwd, old_err = os.getcwd(), sys.stderr
    with tempfile.TemporaryDirectory() as d:
        os.chdir(d)
        try:
            yield Path(d)
        finally:
            sys.stderr.close() if sys.stderr is not old_err else None
            sys.stderr = old_err
            os.chdir(old_cwd)
            if old_mod is None:
                sys.modules.pop("src.humble_browser", None)
            else:
                sys.modules["src.humble_browser"] = old_mod


def test_redeem_humble_key_delegates():
    b = FakeHumbleBrowser()
    assert redeem_humble_key(b, {"human_name": "x"}) == "AAAAA-BBBBB-CCCCC"


def test_export_csv_columns_and_reveal():
    answers = {
        "Export only Steam keys?": True,
        "Export revealed keys?": True,
        "Export unrevealed keys?": True,
    }

    def fake_yn(question, default=True):
        if question.startswith("Sign into Steam"):
            return False
        return answers.get(question, True)  # reveal + confirm -> True

    orig_yn, orig_cls = export_mod.prompt_yes_no, export_mod.cls
    export_mod.prompt_yes_no, export_mod.cls = fake_yn, lambda: None
    try:
        with _env() as d:
            b = FakeHumbleBrowser()
            export_mod.export_mode(b, _orders())
            files = list(d.glob("humble_export_*.csv"))
            assert len(files) == 1
            with open(files[0], encoding="utf-8-sig", newline="") as f:
                rows = list(csv.reader(f))
    finally:
        export_mod.prompt_yes_no, export_mod.cls = orig_yn, orig_cls
    assert rows[0] == EXPORT_KEY_HEADERS
    assert len(rows) == 3  # steam only: revealed + hidden
    assert rows[1][:2] == ["Revealed Game", "RRRRR-RRRRR-RRRRR"]
    assert rows[2][:2] == ["Hidden Game", "AAAAA-BBBBB-CCCCC"]
    assert b.reveals == ["Hidden Game"]


def test_export_header_matches_real_export_file():
    p = Path(__file__).resolve().parent.parent / "humble_export_20261024-154453.csv"
    if p.exists():
        first = p.read_text(encoding="utf-8-sig").splitlines()[0]
        assert first == ",".join(EXPORT_KEY_HEADERS)


def test_main_filters_already_redeemed_and_closes():
    captured = {}

    def fake_redeem(session, keys, **kw):
        captured["session"], captured["keys"], captured["kw"] = session, keys, kw

    orig = main_mod.redeem_steam_keys
    main_mod.redeem_steam_keys = fake_redeem
    try:
        with _env() as d:
            # gamekey col, name, key -- Revealed Game already redeemed
            (d / "redeemed.csv").write_text("ORDER1,Revealed Game,RRRRR-RRRRR-RRRRR\n")
            main_mod.main(["--auto"])
            b = FakeHumbleBrowser.last
    finally:
        main_mod.redeem_steam_keys = orig
    assert [k["human_name"] for k in captured["keys"]] == ["Hidden Game"]
    assert captured["session"] is b
    assert b.interactive is False and b.closed


def test_main_login_required_exits_cleanly():
    with _env(login_required=True):
        try:
            main_mod.main(["--auto"])
        except SystemExit as e:
            assert e.code == 1
        else:
            raise AssertionError("expected SystemExit")
        assert FakeHumbleBrowser.last.closed


def test_missing_gamekey_and_zero_app_id_tolerated():
    from src.redeemer import KeyFileManager
    from src.ownership import match_ownership

    with _env() as d:
        with KeyFileManager() as kfm:
            kfm.write_key(0, {"human_name": "No Gamekey", "steam_app_id": 0,
                              "redeemed_key_val": ""})
    assert match_ownership({1: "Other"}, {"steam_app_id": None}) == (0, None)


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_"):
            fn()
            print("ok", name)
