"""Unit tests for ops/container/grant-deck.py.

These tests import the helper as a module, redirect ALLOWLIST_PATH to a
tempfile, and exercise the validation + mutation logic in-process.

Run:
    cd ops/container && python3 -m pytest test_grant_deck.py -v
"""

from __future__ import annotations

import importlib.util
import os
from pathlib import Path

import pytest


def _load_helper():
    spec = importlib.util.spec_from_file_location(
        "grant_deck", Path(__file__).parent / "grant-deck.py",
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture
def helper(tmp_path, monkeypatch):
    mod = _load_helper()
    allowlist = tmp_path / "allowlist.toml"
    allowlist.write_text('universal = []\n\n[Myrzka]\nallowed = ["Myrzka::*"]\naliases = ["sorotassu"]\n')
    monkeypatch.setattr(mod, "ALLOWLIST_PATH", allowlist)
    yield mod, allowlist


class TestValidatePattern:
    def test_accepts_normal_pattern(self, helper):
        mod, _ = helper
        mod.validate_pattern("Myrzka::NewDeck")
        mod.validate_pattern("Myrzka::*")
        mod.validate_pattern("a-b_c")

    def test_rejects_empty(self, helper):
        mod, _ = helper
        with pytest.raises(SystemExit):
            mod.validate_pattern("")

    def test_rejects_shell_metacharacters(self, helper):
        mod, _ = helper
        for bad in [
            "x;rm",
            'x"y',
            "x'y",
            "x$y",
            "x`y",
            "x\ny",
            "x/y",      # no path separators allowed
            "x\x00y",
        ]:
            with pytest.raises(SystemExit):
                mod.validate_pattern(bad)

    def test_rejects_oversized(self, helper):
        mod, _ = helper
        with pytest.raises(SystemExit):
            mod.validate_pattern("a" * 257)


class TestAuthorize:
    def test_root_can_do_anything(self, helper):
        mod, _ = helper
        data = mod.load_allowlist()
        mod.authorize("universal", data, "root")
        mod.authorize("Myrzka", data, "root")
        mod.authorize("Tava", data, "root")

    def test_non_root_cannot_touch_universal(self, helper):
        mod, _ = helper
        data = mod.load_allowlist()
        with pytest.raises(SystemExit):
            mod.authorize("universal", data, "sorotassu")

    def test_invoker_must_match_section(self, helper):
        mod, _ = helper
        data = mod.load_allowlist()
        # sorotassu is aliased to Myrzka — OK
        mod.authorize("Myrzka", data, "sorotassu")
        # sorotassu is NOT aliased to Tava — fail
        with pytest.raises(SystemExit):
            mod.authorize("Tava", data, "sorotassu")

    def test_unknown_user_rejected(self, helper):
        mod, _ = helper
        data = mod.load_allowlist()
        with pytest.raises(SystemExit):
            mod.authorize("Myrzka", data, "stranger")


class TestMutations:
    def test_add_pattern_appends(self, helper):
        mod, _ = helper
        data = mod.load_allowlist()
        mod.add_pattern(data, "Myrzka", "Myrzka::NewDeck")
        assert "Myrzka::NewDeck" in data["Myrzka"]["allowed"]

    def test_add_pattern_idempotent(self, helper):
        mod, _ = helper
        data = mod.load_allowlist()
        mod.add_pattern(data, "Myrzka", "Myrzka::Same")
        mod.add_pattern(data, "Myrzka", "Myrzka::Same")
        assert data["Myrzka"]["allowed"].count("Myrzka::Same") == 1

    def test_remove_pattern(self, helper):
        mod, _ = helper
        data = mod.load_allowlist()
        mod.remove_pattern(data, "Myrzka", "Myrzka::*")
        assert "Myrzka::*" not in data["Myrzka"]["allowed"]

    def test_grant_and_revoke_new(self, helper):
        mod, _ = helper
        data = mod.load_allowlist()
        mod.grant_new(data, "Myrzka")
        assert "<new>" in data["Myrzka"]["allowed"]
        mod.revoke_new(data, "Myrzka")
        assert "<new>" not in data["Myrzka"]["allowed"]

    def test_grant_new_universal_rejected(self, helper):
        mod, _ = helper
        data = mod.load_allowlist()
        with pytest.raises(SystemExit):
            mod.grant_new(data, "universal")


class TestEmit:
    def test_round_trip_preserves_structure(self, helper, tmp_path):
        mod, _ = helper
        data = mod.load_allowlist()
        out = mod.emit(data)
        # Write the emitted output back and re-parse — should be loadable
        new_path = tmp_path / "rewritten.toml"
        new_path.write_text(out)
        import tomllib
        parsed = tomllib.loads(out)
        assert parsed["Myrzka"]["allowed"] == ["Myrzka::*"]
        assert parsed["Myrzka"]["aliases"] == ["sorotassu"]


class TestAtomicWrite:
    def test_writes_file_and_replaces_atomically(self, helper):
        mod, allowlist = helper
        original = allowlist.read_text()
        data = mod.load_allowlist()
        mod.add_pattern(data, "Myrzka", "Myrzka::NewOne")
        mod.write_atomic(mod.emit(data))
        assert "Myrzka::NewOne" in allowlist.read_text()
        # No stray tempfile left behind
        leftovers = [
            f for f in allowlist.parent.iterdir()
            if f.name.startswith(".allowlist-")
        ]
        assert leftovers == []
