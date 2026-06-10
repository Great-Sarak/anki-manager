"""SeedLogin — one-shot AnkiWeb credential bootstrap, persisted to prefs21.db.

Reads credentials from ``ANKIWEB_USERNAME_<profile>`` and
``ANKIWEB_PASSWORD_<profile>`` first (where ``<profile>`` is the loaded
profile's name, e.g. ``ANKIWEB_USERNAME__anki_skill_testrun`` for the
``_anki_skill_testrun`` profile). Falls back to the unscoped legacy pair
``ANKIWEB_USERNAME`` / ``ANKIWEB_PASSWORD`` for one release cycle.
"""

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


def _resolve_credentials(profile_name: str, trigger: str) -> tuple[str | None, str | None, str]:
    """Look up (username, password, source_label) for the active profile.

    Profile-scoped vars win (``ANKIWEB_USERNAME_<profile>``); unscoped legacy
    vars are the fallback. ``source_label`` describes which path was used,
    for logging.
    """
    scoped_user_key = f"ANKIWEB_USERNAME_{profile_name}"
    scoped_pass_key = f"ANKIWEB_PASSWORD_{profile_name}"
    user = os.environ.get(scoped_user_key)
    pwd = os.environ.get(scoped_pass_key)
    if user and pwd:
        return user, pwd, f"scoped({scoped_user_key})"
    legacy_user = os.environ.get("ANKIWEB_USERNAME")
    legacy_pwd = os.environ.get("ANKIWEB_PASSWORD")
    if legacy_user and legacy_pwd:
        _log(
            f"seedlogin[{trigger}]: using legacy unscoped ANKIWEB_USERNAME/"
            f"PASSWORD for profile={profile_name!r}; migrate to "
            f"{scoped_user_key}/{scoped_pass_key} before v0.2."
        )
        return legacy_user, legacy_pwd, "legacy(unscoped)"
    return None, None, "missing"


def _current_profile_name(pm) -> str:
    """Return the active profile's name, or ``"<unknown>"`` if unavailable.

    ``aqt.profiles.ProfileManager.name`` is a plain ``str`` instance attribute
    in every Anki version (legacy ankiqt through 25.x) — it is assigned in
    ``__init__``/``load``/``rename`` and read directly; there has never been a
    ``name()`` method. The earlier ``callable(getattr(pm, "name", None))``
    guard therefore *always* evaluated False, so the name silently fell
    through to ``"<unknown>"`` — which broke the scoped credential lookup in
    ``_resolve_credentials`` and dropped every login to the legacy unscoped
    fallback account. Read the attribute directly; the ``getattr`` default
    guards only the theoretical case of the attribute being absent entirely.
    """
    return getattr(pm, "name", None) or "<unknown>"


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
        profile_name = _current_profile_name(mw.pm)
        username, password, source = _resolve_credentials(profile_name, trigger)
        if not username or not password:
            _log(
                f"seedlogin[{trigger}]: no credentials for profile "
                f"{profile_name!r} (checked scoped + legacy); no-op"
            )
            _DONE["v"] = True
            return
        _log(
            f"seedlogin[{trigger}]: attempting login profile={profile_name!r} "
            f"username={username!r} source={source}"
        )
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
