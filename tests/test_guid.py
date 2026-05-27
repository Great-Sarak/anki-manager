from __future__ import annotations

import pytest

from anki_manager.guid import (
    GUID_TAG_PREFIX,
    compute_guid,
    derive_front,
    derive_source,
)


class TestComputeGuid:
    def test_is_deterministic(self):
        assert compute_guid("src", "front") == compute_guid("src", "front")

    def test_differs_for_different_source(self):
        assert compute_guid("src-a", "front") != compute_guid("src-b", "front")

    def test_differs_for_different_front(self):
        assert compute_guid("src", "front-a") != compute_guid("src", "front-b")

    def test_uses_namespace_prefix(self):
        guid = compute_guid("s", "f")
        assert guid.startswith(GUID_TAG_PREFIX)

    def test_hash_is_16_hex_chars(self):
        guid = compute_guid("s", "f")
        suffix = guid[len(GUID_TAG_PREFIX):]
        assert len(suffix) == 16
        assert all(c in "0123456789abcdef" for c in suffix)

    def test_separator_prevents_concatenation_collision(self):
        # Without the NUL separator,  ("ab", "cdef")  and  ("abcd", "ef")
        # would hash to the same value.
        assert compute_guid("ab", "cdef") != compute_guid("abcd", "ef")


class TestDeriveSource:
    def test_finds_source_field(self):
        assert derive_source({"Front": "q", "Source": "x"}) == "x"

    def test_case_insensitive(self):
        assert derive_source({"source": "x"}) == "x"
        assert derive_source({"SOURCE": "x"}) == "x"

    def test_returns_empty_when_missing(self):
        assert derive_source({"Front": "q", "Back": "a"}) == ""


class TestDeriveFront:
    def test_prefers_front(self):
        assert derive_front({"Front": "q", "Text": "t"}, ["Front", "Text"]) == "q"

    def test_falls_back_to_text(self):
        assert derive_front({"Text": "t", "Extra": "e"}, ["Text", "Extra"]) == "t"

    def test_case_insensitive(self):
        assert derive_front({"front": "q", "Back": "a"}, ["front", "Back"]) == "q"

    def test_falls_back_to_first_model_field(self):
        # No Front or Text fields named — use first in model order.
        assert derive_front({"Foo": "v"}, ["Foo", "Bar"]) == "v"

    def test_raises_when_unresolvable(self):
        with pytest.raises(ValueError, match="Cannot derive"):
            derive_front({"Foo": "v"}, [])
