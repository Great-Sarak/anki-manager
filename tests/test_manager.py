from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from anki_manager import AnkiManager, InvalidNoteError


def _mgr(client, lifecycle=None):
    return AnkiManager(client=client, lifecycle=lifecycle or MagicMock())


class TestAddNoteValidation:
    def test_passes_through_when_fields_match(self):
        client = MagicMock()
        client.model_field_names.return_value = ["Front", "Back"]
        client.add_note.return_value = 999
        result = _mgr(client).add_note(
            "Deck", "Basic",
            fields={"Front": "Q", "Back": "A"},
            tags=["t"],
        )
        assert result == 999
        client.add_note.assert_called_once_with(
            deck="Deck", model="Basic",
            fields={"Front": "Q", "Back": "A"},
            tags=["t"],
        )

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
