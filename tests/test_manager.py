from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from anki_manager import (
    AgentEntry,
    Allowlist,
    AnkiManager,
    Config,
    DeckNotAllowedError,
    InvalidNoteError,
    NoteExistsError,
    NoteNotFoundError,
)
from anki_manager.guid import compute_guid


_DEFAULT_AGENT = AgentEntry(
    name="Test",
    patterns=("D", "Deck", "Myrzka::*", "anki-rpc-test"),
    aliases=("tester",),
    has_new=False,
)
_DEFAULT_ALLOWLIST = Allowlist(universal=(), agents={"Test": _DEFAULT_AGENT})
# lock_path=None disables the cross-process flock for unit tests; we
# exercise the lock itself in tests/test_lock.py.
_NO_LOCK_CONFIG = Config(lock_path=None)


def _mgr(client, lifecycle=None, *, allowlist=_DEFAULT_ALLOWLIST, agent=_DEFAULT_AGENT, config=None):
    return AnkiManager(
        config=config or _NO_LOCK_CONFIG,
        client=client,
        lifecycle=lifecycle or MagicMock(),
        allowlist=allowlist,
        agent=agent,
    )


class TestAddNoteValidation:
    def test_passes_through_when_fields_match(self):
        client = MagicMock()
        client.model_field_names.return_value = ["Front", "Back"]
        client.find_notes.return_value = []
        client.add_note.return_value = 999
        result = _mgr(client).add_note(
            "Deck", "Basic",
            fields={"Front": "Q", "Back": "A"},
            tags=["t"],
        )
        assert result.note_id == 999
        assert result.stable_guid.startswith("anki-manager::")
        # Tag list should include the user's tag + the stable GUID
        call_tags = client.add_note.call_args.kwargs["tags"]
        assert "t" in call_tags
        assert result.stable_guid in call_tags

    def test_raises_when_unknown_model(self):
        client = MagicMock()
        client.model_field_names.return_value = []
        with pytest.raises(InvalidNoteError, match="not found"):
            _mgr(client).add_note("D", "Ghost", fields={"Front": "Q"})

    def test_raises_when_extra_field(self):
        client = MagicMock()
        client.model_field_names.return_value = ["Front", "Back"]
        with pytest.raises(InvalidNoteError, match="has no fields named"):
            _mgr(client).add_note(
                "D", "Basic",
                fields={"Front": "Q", "Back": "A", "Notes": "extra"},
            )

    def test_raises_when_missing_field(self):
        client = MagicMock()
        client.model_field_names.return_value = ["Front", "Back", "Source"]
        with pytest.raises(InvalidNoteError, match="requires fields not provided"):
            _mgr(client).add_note(
                "D", "Basic",
                fields={"Front": "Q", "Back": "A"},
            )

    def test_never_calls_add_note_on_validation_failure(self):
        client = MagicMock()
        client.model_field_names.return_value = ["Front", "Back"]
        with pytest.raises(InvalidNoteError):
            _mgr(client).add_note("D", "Basic", fields={"Front": "Q"})
        client.add_note.assert_not_called()


class TestAddNoteGuidHandling:
    def test_explicit_stable_guid_is_used(self):
        client = MagicMock()
        client.model_field_names.return_value = ["Front", "Back"]
        client.find_notes.return_value = []
        client.add_note.return_value = 42
        explicit = "anki-manager::deadbeefdeadbeef"
        result = _mgr(client).add_note(
            "D", "Basic",
            fields={"Front": "q", "Back": "a"},
            stable_guid=explicit,
        )
        assert result.stable_guid == explicit
        assert explicit in client.add_note.call_args.kwargs["tags"]

    def test_derived_guid_matches_compute_guid_of_source_and_front(self):
        client = MagicMock()
        client.model_field_names.return_value = ["Front", "Back", "Source"]
        client.find_notes.return_value = []
        client.add_note.return_value = 1
        result = _mgr(client).add_note(
            "D", "Basic",
            fields={"Front": "q", "Back": "a", "Source": "memory.md"},
        )
        assert result.stable_guid == compute_guid("memory.md", "q")

    def test_raises_when_guid_already_exists(self):
        client = MagicMock()
        client.model_field_names.return_value = ["Front", "Back"]
        client.find_notes.return_value = [123]
        with pytest.raises(NoteExistsError, match="123"):
            _mgr(client).add_note("D", "Basic", fields={"Front": "q", "Back": "a"})
        client.add_note.assert_not_called()


class TestFindByGuid:
    def test_returns_note_id(self):
        client = MagicMock()
        client.find_notes.return_value = [555]
        assert _mgr(client).find_by_guid("anki-manager::abc") == 555
        client.find_notes.assert_called_once_with("tag:anki-manager::abc")

    def test_returns_none_when_missing(self):
        client = MagicMock()
        client.find_notes.return_value = []
        assert _mgr(client).find_by_guid("anki-manager::xyz") is None

    def test_raises_when_multiple_match(self):
        client = MagicMock()
        client.find_notes.return_value = [1, 2]
        with pytest.raises(NoteExistsError, match="Multiple"):
            _mgr(client).find_by_guid("anki-manager::dupe")


class TestUpdateNote:
    def test_updates_fields_of_existing_note(self):
        client = MagicMock()
        client.find_notes.return_value = [999]
        note_id = _mgr(client).update_note("anki-manager::abc", {"Front": "new"})
        assert note_id == 999
        client.update_note_fields.assert_called_once_with(999, {"Front": "new"})

    def test_raises_when_note_missing(self):
        client = MagicMock()
        client.find_notes.return_value = []
        with pytest.raises(NoteNotFoundError):
            _mgr(client).update_note("anki-manager::missing", {"Front": "x"})


class TestUpsertNote:
    def test_creates_when_absent(self):
        client = MagicMock()
        client.model_field_names.return_value = ["Front", "Back"]
        client.find_notes.return_value = []
        client.add_note.return_value = 111
        result = _mgr(client).upsert_note(
            "D", "Basic", fields={"Front": "q", "Back": "a"},
        )
        assert result.created is True
        assert result.note_id == 111
        client.update_note_fields.assert_not_called()

    def test_updates_when_present(self):
        client = MagicMock()
        client.model_field_names.return_value = ["Front", "Back"]
        client.find_notes.return_value = [222]
        result = _mgr(client).upsert_note(
            "D", "Basic", fields={"Front": "q", "Back": "a"},
        )
        assert result.created is False
        assert result.note_id == 222
        client.update_note_fields.assert_called_once_with(222, {"Front": "q", "Back": "a"})
        client.add_note.assert_not_called()


class TestListModels:
    def test_lists_all_models_with_their_fields(self):
        client = MagicMock()
        client.model_names.return_value = ["Basic", "Cloze"]
        client.model_field_names.side_effect = lambda name: {
            "Basic": ["Front", "Back"],
            "Cloze": ["Text", "Extra"],
        }[name]
        result = _mgr(client).list_models()
        assert result == {
            "Basic": ["Front", "Back"],
            "Cloze": ["Text", "Extra"],
        }


class TestPassthroughs:
    def test_add_deck(self):
        client = MagicMock()
        client.add_deck.return_value = 12345
        assert _mgr(client).add_deck("D") == 12345

    def test_sync(self):
        client = MagicMock()
        _mgr(client).sync()
        client.sync.assert_called_once()

    def test_force_upload(self):
        client = MagicMock()
        _mgr(client).force_upload()
        client.force_upload.assert_called_once()

    def test_call_escape_hatch(self):
        client = MagicMock()
        client.call.return_value = {"x": 1}
        result = _mgr(client).call("guiDeckReview", name="D")
        assert result == {"x": 1}
        client.call.assert_called_once_with("guiDeckReview", name="D")


class TestLifecyclePassthroughs:
    def test_ensure_running(self):
        lc = MagicMock()
        _mgr(MagicMock(), lifecycle=lc).ensure_running()
        lc.ensure_running.assert_called_once()

    def test_status_returns_underlying_status(self):
        lc = MagicMock()
        lc.status.return_value = "STATUS"
        assert _mgr(MagicMock(), lifecycle=lc).status() == "STATUS"

    def test_restart_waits_ready(self):
        lc = MagicMock()
        _mgr(MagicMock(), lifecycle=lc).restart()
        lc.restart.assert_called_once()
        lc.wait_ready.assert_called_once()


class TestDryRun:
    def test_add_note_dry_run_skips_rpc(self):
        client = MagicMock()
        client.model_field_names.return_value = ["Front", "Back"]
        client.find_notes.return_value = []
        result = _mgr(client).add_note(
            "D", "Basic",
            fields={"Front": "q", "Back": "a"},
            dry_run=True,
        )
        assert result.dry_run is True
        assert result.note_id == 0
        assert result.stable_guid.startswith("anki-manager::")
        client.add_note.assert_not_called()

    def test_add_note_dry_run_still_validates_schema(self):
        client = MagicMock()
        client.model_field_names.return_value = ["Front", "Back"]
        with pytest.raises(InvalidNoteError):
            _mgr(client).add_note(
                "D", "Basic",
                fields={"Front": "q"},  # missing Back
                dry_run=True,
            )

    def test_add_note_dry_run_still_blocks_disallowed_deck(self):
        client = MagicMock()
        with pytest.raises(DeckNotAllowedError):
            _mgr(client).add_note(
                "OffLimits", "Basic",
                fields={"Front": "q", "Back": "a"},
                dry_run=True,
            )

    def test_add_note_dry_run_still_raises_on_collision(self):
        client = MagicMock()
        client.model_field_names.return_value = ["Front", "Back"]
        client.find_notes.return_value = [777]
        with pytest.raises(NoteExistsError):
            _mgr(client).add_note(
                "D", "Basic",
                fields={"Front": "q", "Back": "a"},
                dry_run=True,
            )

    def test_update_note_dry_run_does_lookup_no_write(self):
        client = MagicMock()
        client.find_notes.return_value = [555]
        note_id = _mgr(client).update_note(
            "anki-manager::abc", {"Front": "new"}, dry_run=True,
        )
        assert note_id == 555
        client.update_note_fields.assert_not_called()

    def test_update_note_dry_run_raises_when_missing(self):
        client = MagicMock()
        client.find_notes.return_value = []
        with pytest.raises(NoteNotFoundError):
            _mgr(client).update_note(
                "anki-manager::missing", {"Front": "x"}, dry_run=True,
            )

    def test_upsert_dry_run_create_path(self):
        client = MagicMock()
        client.model_field_names.return_value = ["Front", "Back"]
        client.find_notes.return_value = []
        result = _mgr(client).upsert_note(
            "D", "Basic", fields={"Front": "q", "Back": "a"}, dry_run=True,
        )
        assert result.dry_run is True
        assert result.created is True
        assert result.note_id == 0
        client.add_note.assert_not_called()
        client.update_note_fields.assert_not_called()

    def test_upsert_dry_run_update_path(self):
        client = MagicMock()
        client.model_field_names.return_value = ["Front", "Back"]
        client.find_notes.return_value = [222]
        result = _mgr(client).upsert_note(
            "D", "Basic", fields={"Front": "q", "Back": "a"}, dry_run=True,
        )
        assert result.dry_run is True
        assert result.created is False
        assert result.note_id == 222
        client.update_note_fields.assert_not_called()


class TestAllowlistEnforcement:
    def test_add_note_blocked_for_disallowed_deck(self):
        client = MagicMock()
        client.model_field_names.return_value = ["Front", "Back"]
        with pytest.raises(DeckNotAllowedError, match="OffLimits"):
            _mgr(client).add_note(
                "OffLimits", "Basic", fields={"Front": "q", "Back": "a"},
            )
        client.add_note.assert_not_called()

    def test_add_note_allowed_when_deck_matches_pattern(self):
        client = MagicMock()
        client.model_field_names.return_value = ["Front", "Back"]
        client.find_notes.return_value = []
        client.add_note.return_value = 1
        _mgr(client).add_note(
            "Myrzka::Daily", "Basic", fields={"Front": "q", "Back": "a"},
        )
        client.add_note.assert_called_once()

    def test_upsert_blocked_for_disallowed_deck(self):
        client = MagicMock()
        client.model_field_names.return_value = ["Front", "Back"]
        with pytest.raises(DeckNotAllowedError):
            _mgr(client).upsert_note(
                "OffLimits", "Basic", fields={"Front": "q", "Back": "a"},
            )

    def test_add_deck_blocked_without_new_capability(self):
        client = MagicMock()
        with pytest.raises(DeckNotAllowedError):
            _mgr(client).add_deck("OffLimits::NewDeck")
        client.add_deck.assert_not_called()

    def test_add_deck_allowed_when_matches_pattern(self):
        client = MagicMock()
        client.add_deck.return_value = 12345
        result = _mgr(client).add_deck("Myrzka::FreshDeck")
        assert result == 12345

    def test_add_deck_auto_extends_with_new_capability(self, monkeypatch):
        client = MagicMock()
        client.add_deck.return_value = 999

        from anki_manager import permissions
        calls: list[tuple[str, str]] = []
        def fake_add_pattern(section, pattern):
            calls.append((section, pattern))
        monkeypatch.setattr(permissions, "add_pattern", fake_add_pattern)

        # Build a mutable allowlist the manager can "reload" to a wider one
        agent_with_new = AgentEntry(
            name="Test",
            patterns=("Myrzka::*",),
            aliases=("tester",),
            has_new=True,
        )
        allowlist_before = Allowlist(universal=(), agents={"Test": agent_with_new})
        # After the helper "writes", reload returns an allowlist that includes
        # the new pattern.  Patch Allowlist.load to simulate that.
        agent_after = AgentEntry(
            name="Test",
            patterns=("Myrzka::*", "Brand::NewDeck"),
            aliases=("tester",),
            has_new=True,
        )
        allowlist_after = Allowlist(universal=(), agents={"Test": agent_after})
        monkeypatch.setattr(Allowlist, "load", classmethod(lambda cls, path=None: allowlist_after))

        mgr = AnkiManager(
            config=_NO_LOCK_CONFIG,
            client=client, lifecycle=MagicMock(),
            allowlist=allowlist_before, agent=agent_with_new,
        )
        mgr.add_deck("Brand::NewDeck")

        # Helper invoked exactly once with the new pattern
        assert calls == [("Test", "Brand::NewDeck")]
        client.add_deck.assert_called_once_with("Brand::NewDeck")
