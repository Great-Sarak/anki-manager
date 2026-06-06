"""SeedLogin — one-shot AnkiWeb credential bootstrap, persisted to prefs21.db."""

from __future__ import annotations

import os
import traceback
from pathlib import Path

from aqt import mw
from aqt.gui_hooks import profile_did_open, main_window_did_init
from aqt.qt import QTimer

LOG = Path("/data/seedlogin.log")
_DONE = {"v": False}


def _log(msg: str) -> None:
    try:
        with LOG.open("a", encoding="utf-8") as fh:
            fh.write(msg.rstrip() + "\n")
    except Exception:
        pass


def _seed_login(trigger: str) -> None:
    if _DONE["v"]:
        return
    try:
        if mw is None or mw.pm is None or mw.col is None:
            _log(f"seedlogin[{trigger}]: not ready (mw/pm/col); skip")
            return
        existing = mw.pm.profile.get("syncKey")
        if existing:
            _log(f"seedlogin[{trigger}]: syncKey already set; no-op")
            _DONE["v"] = True
            return
        username = os.environ.get("ANKIWEB_USERNAME")
        password = os.environ.get("ANKIWEB_PASSWORD")
        if not username or not password:
            _log(f"seedlogin[{trigger}]: ANKIWEB_USERNAME/PASSWORD not set; no-op")
            _DONE["v"] = True
            return
        _log(f"seedlogin[{trigger}]: attempting login username={username!r}")
        auth = mw.col.sync_login(username=username, password=password, endpoint=None)
        _DONE["v"] = True
        hkey = getattr(auth, "hkey", None)
        endpoint = getattr(auth, "endpoint", None)
        if not hkey:
            _log(f"seedlogin[{trigger}]: login returned no hkey: {auth!r}")
            return
        mw.pm.set_sync_key(hkey)
        mw.pm.set_sync_username(username)
        if endpoint:
            mw.pm.set_current_sync_url(endpoint)
        try:
            mw.pm.save()
            _log(f"seedlogin[{trigger}]: pm.save() OK")
        except Exception:
            _log(f"seedlogin[{trigger}]: pm.save() failed:")
            _log(traceback.format_exc())
        _log(
            f"seedlogin[{trigger}]: SUCCESS hkey_len={len(hkey)} "
            f"endpoint={endpoint!r} username={username!r}"
        )
    except Exception:
        _log(f"seedlogin[{trigger}]: EXCEPTION:")
        _log(traceback.format_exc())


def _on_profile_did_open():
    _seed_login("profile_did_open")


def _on_main_window_did_init():
    QTimer.singleShot(3000, lambda: _seed_login("delayed_timer"))


profile_did_open.append(_on_profile_did_open)
main_window_did_init.append(_on_main_window_did_init)
_log("seedlogin: addon loaded")
