"""Unit tests for SeedLogin's per-profile credential resolution.

Runnable in any Python 3.12 env with pytest installed — no Anki/aqt
imports happen at module scope (we strip them before exercising the
private helper). Run:

    cd ops/container && python3 -m pytest test_seedlogin.py -v
"""

from __future__ import annotations

import importlib.util
import sys
import types
from pathlib import Path

import pytest


def _load_module():
    # Stub aqt + aqt.gui_hooks + aqt.qt so the addon's module-scope imports
    # succeed without a real Anki install.
    aqt_stub = types.ModuleType("aqt")
    aqt_stub.mw = None
    gui_hooks_stub = types.ModuleType("aqt.gui_hooks")

    class _Hook:
        def append(self, _):
            pass

    gui_hooks_stub.profile_did_open = _Hook()
    gui_hooks_stub.main_window_did_init = _Hook()
    qt_stub = types.ModuleType("aqt.qt")
    qt_stub.QTimer = type("QTimer", (), {"singleShot": staticmethod(lambda *_a, **_k: None)})

    sys.modules["aqt"] = aqt_stub
    sys.modules["aqt.gui_hooks"] = gui_hooks_stub
    sys.modules["aqt.qt"] = qt_stub

    src = Path(__file__).parent / "seedlogin-src" / "__init__.py"
    spec = importlib.util.spec_from_file_location("seedlogin_test_load", src)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _load_resolver():
    return _load_module()._resolve_credentials


@pytest.fixture
def resolve(monkeypatch):
    # Clear all relevant env vars before each test
    for key in list(
        k for k in monkeypatch.delenv.__self__.__dict__ if False  # placeholder; loop below does it
    ):
        pass
    for key in [
        "ANKIWEB_USERNAME",
        "ANKIWEB_PASSWORD",
        "ANKIWEB_USERNAME_sorotassu",
        "ANKIWEB_PASSWORD_sorotassu",
        "ANKIWEB_USERNAME__anki_skill_testrun",
        "ANKIWEB_PASSWORD__anki_skill_testrun",
    ]:
        monkeypatch.delenv(key, raising=False)
    return _load_resolver()


def test_scoped_pair_preferred_over_legacy(resolve, monkeypatch):
    monkeypatch.setenv("ANKIWEB_USERNAME_sorotassu", "scoped@example.com")
    monkeypatch.setenv("ANKIWEB_PASSWORD_sorotassu", "scoped-pw")
    monkeypatch.setenv("ANKIWEB_USERNAME", "legacy@example.com")
    monkeypatch.setenv("ANKIWEB_PASSWORD", "legacy-pw")
    user, pwd, source = resolve("sorotassu", "test")
    assert user == "scoped@example.com"
    assert pwd == "scoped-pw"
    assert "scoped" in source


def test_legacy_fallback_when_no_scoped(resolve, monkeypatch):
    monkeypatch.setenv("ANKIWEB_USERNAME", "legacy@example.com")
    monkeypatch.setenv("ANKIWEB_PASSWORD", "legacy-pw")
    user, pwd, source = resolve("sorotassu", "test")
    assert user == "legacy@example.com"
    assert pwd == "legacy-pw"
    assert source == "legacy(unscoped)"


def test_underscore_prefix_profile_resolves(resolve, monkeypatch):
    monkeypatch.setenv("ANKIWEB_USERNAME__anki_skill_testrun", "test@example.com")
    monkeypatch.setenv("ANKIWEB_PASSWORD__anki_skill_testrun", "test-pw")
    user, pwd, source = resolve("_anki_skill_testrun", "test")
    assert user == "test@example.com"
    assert pwd == "test-pw"
    assert "scoped" in source


def test_partial_scoped_pair_falls_through_to_legacy(resolve, monkeypatch):
    # Only username set on scoped side → should not partially-use it
    monkeypatch.setenv("ANKIWEB_USERNAME_sorotassu", "scoped@example.com")
    monkeypatch.setenv("ANKIWEB_USERNAME", "legacy@example.com")
    monkeypatch.setenv("ANKIWEB_PASSWORD", "legacy-pw")
    user, pwd, source = resolve("sorotassu", "test")
    assert user == "legacy@example.com"
    assert source == "legacy(unscoped)"


def test_missing_returns_none(resolve):
    user, pwd, source = resolve("sorotassu", "test")
    assert user is None
    assert pwd is None
    assert source == "missing"


# --- _current_profile_name (regression for the silent <unknown> bug) -------
#
# aqt.profiles.ProfileManager.name is a plain str attribute in every Anki
# version (legacy ankiqt through 25.x); there is no name() method. The old
# `callable(getattr(pm, "name", None))` guard always evaluated False, so the
# profile name fell through to "<unknown>", the scoped credential lookup
# missed, and every login dropped to the legacy unscoped account.


class _FakePM:
    def __init__(self, name):
        self.name = name


def test_profile_name_reads_str_attribute():
    assert _load_module()._current_profile_name(_FakePM("sorotassu")) == "sorotassu"


def test_profile_name_underscore_prefix():
    pm = _FakePM("_anki_skill_testrun")
    assert _load_module()._current_profile_name(pm) == "_anki_skill_testrun"


def test_profile_name_is_not_treated_as_callable():
    # A str has no __call__; the resolver must never try to invoke it.
    name = _load_module()._current_profile_name(_FakePM("sorotassu"))
    assert name == "sorotassu"  # not "<unknown>", not a TypeError


def test_profile_name_absent_attribute_falls_back():
    class _NoName:
        pass

    assert _load_module()._current_profile_name(_NoName()) == "<unknown>"


def test_profile_name_none_falls_back():
    assert _load_module()._current_profile_name(_FakePM(None)) == "<unknown>"
