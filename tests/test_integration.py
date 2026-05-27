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
    note_id = mgr.add_note(DECK, MODEL_BASIC, fields=fields, tags=["anki-manager-test"])
    assert isinstance(note_id, int)
    assert note_id in mgr.call("findNotes", query=f'tag:anki-manager-test')


def test_add_note_validates_unknown_field(mgr: AnkiManager):
    from anki_manager import InvalidNoteError
    mgr.add_deck(DECK)
    with pytest.raises(InvalidNoteError, match="has no fields named"):
        mgr.add_note(DECK, MODEL_BASIC, fields={"NonExistent": "x"})


def test_sync_does_not_raise(mgr: AnkiManager):
    # AnkiWeb already has the spike data (forceUpload run earlier in the spike).
    # `sync` should succeed (NO_CHANGES or NORMAL_SYNC) after any test mutations.
    mgr.sync()
