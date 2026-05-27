"""Integration tests against the live kryshanti-anki systemd unit.

Requires:
  - host-setup.sh has been run (creates the unit + polkit rule)
  - The invoking user is in the kryshanti-anki-users group
  - A current login session reflects that group membership (logout/in if not)

Run with:
    ANKI_MANAGER_INTEGRATION=1 pytest tests/test_integration.py -v
"""

from __future__ import annotations

import os

import pytest

from anki_manager import AnkiManager

pytestmark = pytest.mark.skipif(
    not os.getenv("ANKI_MANAGER_INTEGRATION"),
    reason="set ANKI_MANAGER_INTEGRATION=1 to run against the live unit",
)


DECK = "Myrzka::anki-manager-integration-test"
MODEL_BASIC = "Myrzka Basic"


@pytest.fixture(scope="module")
def mgr() -> AnkiManager:
    m = AnkiManager()
    m.ensure_running()
    return m


@pytest.fixture(autouse=True)
def cleanup(mgr: AnkiManager):
    yield
    note_ids = mgr.call("findNotes", query=f'deck:"{DECK}"')
    if note_ids:
        mgr.call("deleteNotes", notes=note_ids)


def test_status_active_and_ready(mgr: AnkiManager):
    s = mgr.status()
    assert s.active is True
    assert s.ready is True
    assert s.sub_state == "running"


def test_list_models(mgr: AnkiManager):
    models = mgr.list_models()
    assert MODEL_BASIC in models
    assert "Front" in models[MODEL_BASIC]
    assert "Back" in models[MODEL_BASIC]


def test_add_deck_and_note_round_trip(mgr: AnkiManager):
    mgr.add_deck(DECK)
    fields = {f: "" for f in mgr.list_models()[MODEL_BASIC]}
    fields["Front"] = "integration-test-q"
    fields["Back"] = "integration-test-a"
    result = mgr.add_note(DECK, MODEL_BASIC, fields=fields, tags=["anki-manager-test"])
    assert isinstance(result.note_id, int)
    assert result.note_id in mgr.call("findNotes", query='tag:anki-manager-test')


def test_add_note_validates_unknown_field(mgr: AnkiManager):
    from anki_manager import InvalidNoteError
    mgr.add_deck(DECK)
    with pytest.raises(InvalidNoteError, match="has no fields named"):
        mgr.add_note(DECK, MODEL_BASIC, fields={"NonExistent": "x"})


def test_sync_does_not_raise(mgr: AnkiManager):
    # AnkiWeb already has the spike data (forceUpload run earlier in the spike).
    # `sync` should succeed (NO_CHANGES or NORMAL_SYNC) after any test mutations.
    mgr.sync()


def test_guid_round_trip(mgr: AnkiManager):
    """add -> find_by_guid -> update -> verify -> upsert (idempotent)."""
    from anki_manager import NoteExistsError

    mgr.add_deck(DECK)
    fields = {f: "" for f in mgr.list_models()[MODEL_BASIC]}
    fields["Front"] = "guid-round-trip-q"
    fields["Back"] = "guid-round-trip-a-original"
    fields["Source"] = "phase4-integration"

    # 1. Add a note; GUID is derived from Source + Front.
    add_result = mgr.add_note(DECK, MODEL_BASIC, fields=fields, tags=["anki-manager-test"])
    assert add_result.stable_guid.startswith("anki-manager::")

    # 2. find_by_guid returns the same note_id
    assert mgr.find_by_guid(add_result.stable_guid) == add_result.note_id

    # 3. Adding the same content again raises NoteExistsError
    with pytest.raises(NoteExistsError, match=str(add_result.note_id)):
        mgr.add_note(DECK, MODEL_BASIC, fields=fields, tags=["anki-manager-test"])

    # 4. update_note changes fields, GUID unchanged
    mgr.update_note(add_result.stable_guid, {"Back": "guid-round-trip-a-updated"})
    info = mgr.call("notesInfo", notes=[add_result.note_id])
    assert info[0]["fields"]["Back"]["value"] == "guid-round-trip-a-updated"

    # 5. upsert with same content updates without raising
    fields["Back"] = "guid-round-trip-a-upserted"
    upsert_result = mgr.upsert_note(DECK, MODEL_BASIC, fields=fields, tags=["anki-manager-test"])
    assert upsert_result.created is False
    assert upsert_result.note_id == add_result.note_id
    assert upsert_result.stable_guid == add_result.stable_guid


def test_upsert_creates_new_when_absent(mgr: AnkiManager):
    """upsert on previously-unseen content should create a fresh note."""
    mgr.add_deck(DECK)
    fields = {f: "" for f in mgr.list_models()[MODEL_BASIC]}
    fields["Front"] = "upsert-fresh-q"
    fields["Back"] = "upsert-fresh-a"
    fields["Source"] = "phase4-integration-upsert"

    result = mgr.upsert_note(DECK, MODEL_BASIC, fields=fields)
    assert result.created is True
    assert mgr.find_by_guid(result.stable_guid) == result.note_id


def test_update_note_raises_for_missing_guid(mgr: AnkiManager):
    from anki_manager import NoteNotFoundError
    with pytest.raises(NoteNotFoundError):
        mgr.update_note("anki-manager::ffffffffffffffff", {"Front": "x"})


def test_allowlist_blocks_disallowed_deck(mgr: AnkiManager):
    """Requires the starter allowlist installed by host-setup.sh."""
    from anki_manager import DeckNotAllowedError
    fields = {f: "" for f in mgr.list_models()[MODEL_BASIC]}
    fields["Front"] = "should-fail"
    fields["Back"] = "should-fail"
    fields["Source"] = "phase4-integration"
    with pytest.raises(DeckNotAllowedError, match="OffLimits"):
        mgr.add_note("OffLimits", MODEL_BASIC, fields=fields)


def test_allowlist_show(mgr: AnkiManager):
    """Effective allowlist should include the agent's patterns."""
    patterns = mgr.effective_allowlist()
    # The starter ships with Myrzka section claiming the invoking user,
    # so we expect Myrzka::* in the effective set.
    assert any("Myrzka" in p for p in patterns), f"got: {patterns}"
