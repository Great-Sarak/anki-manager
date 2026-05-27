"""Tests for the batch add-notes module.

Uses a fake AnkiManager with stubbed write methods so tests don't require a live container.
"""

from __future__ import annotations

import io
import json
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from anki_manager import batch
from anki_manager.batch import (
    BatchParseError,
    NoteEntry,
    add_notes,
    parse_jsonl,
    parse_markdown,
)


def _fake_mgr_upsert(would_create: bool = True) -> MagicMock:
    """Fake mgr whose upsert_note always succeeds and returns a configurable result."""
    mgr = MagicMock()
    counter = {"i": 0}
    def upsert(deck, model, fields, *, tags=None, stable_guid=None, dry_run=False):
        counter["i"] += 1
        result = MagicMock()
        result.note_id = 1000 + counter["i"]
        result.stable_guid = stable_guid or f"anki-manager::{counter['i']:04x}"
        result.created = would_create
        result.dry_run = dry_run
        return result
    mgr.upsert_note.side_effect = upsert
    return mgr


def _fake_mgr_add() -> MagicMock:
    mgr = MagicMock()
    counter = {"i": 0}
    def add(deck, model, fields, *, tags=None, stable_guid=None, dry_run=False):
        counter["i"] += 1
        result = MagicMock()
        result.note_id = 2000 + counter["i"]
        result.stable_guid = stable_guid or f"anki-manager::{counter['i']:04x}"
        result.dry_run = dry_run
        return result
    mgr.add_note.side_effect = add
    return mgr


# ---- parse_jsonl ----


def test_parse_jsonl_happy() -> None:
    body = (
        '{"deck": "D1", "model": "M1", "fields": {"Front": "Q1", "Back": "A1"}}\n'
        '{"deck": "D2", "model": "M2", "fields": {"Front": "Q2", "Back": "A2"}, "tags": ["t"]}\n'
    )
    entries = parse_jsonl(io.StringIO(body))
    assert len(entries) == 2
    assert entries[0].deck == "D1"
    assert entries[0].fields == {"Front": "Q1", "Back": "A1"}
    assert entries[1].tags == ["t"]


def test_parse_jsonl_skips_blank_and_comment_lines() -> None:
    body = (
        '# a comment\n'
        '\n'
        '{"deck": "D", "model": "M", "fields": {"Front": "Q"}}\n'
        '\n'
        '# trailing comment\n'
    )
    entries = parse_jsonl(io.StringIO(body))
    assert len(entries) == 1


def test_parse_jsonl_invalid_line_raises() -> None:
    body = '{"deck": "D", "model": "M", "fields": {"Front": "Q"}}\nnot json\n'
    with pytest.raises(BatchParseError, match="line 2.*invalid JSON"):
        parse_jsonl(io.StringIO(body))


def test_parse_jsonl_missing_required_key_raises() -> None:
    body = '{"deck": "D", "fields": {"Front": "Q"}}\n'
    with pytest.raises(BatchParseError, match="missing required key 'model'"):
        parse_jsonl(io.StringIO(body))


def test_parse_jsonl_non_dict_fields_raises() -> None:
    body = '{"deck": "D", "model": "M", "fields": "not-an-object"}\n'
    with pytest.raises(BatchParseError, match="'fields' must be a JSON object"):
        parse_jsonl(io.StringIO(body))


def test_parse_jsonl_coerces_field_values_to_str() -> None:
    body = '{"deck": "D", "model": "M", "fields": {"Front": "Q", "Page": 42}}\n'
    entries = parse_jsonl(io.StringIO(body))
    assert entries[0].fields["Page"] == "42"


# ---- parse_markdown ----


MARKDOWN_SAMPLE = (
    "## Card 1 — term-def\n"
    "**Front:** mitochondria\n"
    "**Back:** powerhouse of the cell\n"
    "**Source:** https://example.com\n"
    "**Position:** #organelles\n"
    "**Deck:** Reading\n"
    "**Model:** AT Basic\n"
    "**Tags:** biology, biology::organelles\n"
    "---\n"
    "## Card 2 — cloze\n"
    "**Text:** The {{c1::nucleus}} houses DNA.\n"
    "**Source:** https://example.com\n"
    "**Position:** \n"
    "**Deck:** Reading\n"
    "**Model:** AT Cloze\n"
    "**Tags:** biology\n"
    "---\n"
)


def test_parse_markdown_happy() -> None:
    entries = parse_markdown(io.StringIO(MARKDOWN_SAMPLE))
    assert len(entries) == 2
    assert entries[0].deck == "Reading"
    assert entries[0].model == "AT Basic"
    assert entries[0].fields["Front"] == "mitochondria"
    assert entries[0].fields["Source"] == "https://example.com"
    assert entries[0].tags == ["biology", "biology::organelles"]
    assert entries[1].model == "AT Cloze"
    assert "{{c1::nucleus}}" in entries[1].fields["Text"]
    assert entries[1].tags == ["biology"]


def test_parse_markdown_skips_top_level_header() -> None:
    body = "# Queue (empty)\n\nNo card candidates from this ingestion.\n"
    entries = parse_markdown(io.StringIO(body))
    assert entries == []


def test_parse_markdown_missing_card_header_raises() -> None:
    body = "**Front:** x\n**Deck:** D\n**Model:** M\n**Tags:** \n---\n"
    with pytest.raises(BatchParseError, match="missing.*header"):
        parse_markdown(io.StringIO(body))


def test_parse_markdown_missing_deck_raises() -> None:
    body = "## Card 1 — term-def\n**Front:** Q\n**Model:** M\n**Tags:** \n---\n"
    with pytest.raises(BatchParseError, match="missing.*Deck"):
        parse_markdown(io.StringIO(body))


def test_parse_markdown_missing_model_raises() -> None:
    body = "## Card 1 — term-def\n**Front:** Q\n**Deck:** D\n**Tags:** \n---\n"
    with pytest.raises(BatchParseError, match="missing.*Model"):
        parse_markdown(io.StringIO(body))


def test_parse_markdown_no_content_fields_raises() -> None:
    body = "## Card 1 — term-def\n**Deck:** D\n**Model:** M\n**Tags:** \n---\n"
    with pytest.raises(BatchParseError, match="no content fields"):
        parse_markdown(io.StringIO(body))


# ---- add_notes driver ----


def _entry(i: int = 0, **overrides) -> NoteEntry:
    defaults = {
        "deck": "Reading",
        "model": "AT Basic",
        "fields": {"Front": f"Q{i}", "Back": f"A{i}"},
        "tags": [],
        "stable_guid": None,
    }
    defaults.update(overrides)
    return NoteEntry(**defaults)


def test_add_notes_upsert_all_succeed() -> None:
    mgr = _fake_mgr_upsert(would_create=True)
    entries = [_entry(1), _entry(2), _entry(3)]
    result = add_notes(mgr, entries, mode="upsert")
    assert result.created == 3
    assert result.updated == 0
    assert result.failed == []
    # validate dry-run pass + real pass = 6 calls total
    assert mgr.upsert_note.call_count == 6


def test_add_notes_upsert_some_existing() -> None:
    """First entry creates, second updates."""
    mgr = MagicMock()
    state = {"call": 0}
    def upsert(deck, model, fields, *, tags=None, stable_guid=None, dry_run=False):
        state["call"] += 1
        r = MagicMock()
        r.note_id = 1000 + state["call"]
        r.stable_guid = "anki-manager::xxx"
        # First two calls are dry-run validation (both report would-create).
        # Calls 3,4 are real writes — alternating created/updated based on real state.
        if state["call"] <= 2:
            r.created = True
        else:
            r.created = state["call"] == 3
        r.dry_run = dry_run
        return r
    mgr.upsert_note.side_effect = upsert
    entries = [_entry(1), _entry(2)]
    result = add_notes(mgr, entries, mode="upsert")
    assert result.created == 1
    assert result.updated == 1
    assert result.failed == []


def test_add_notes_all_or_nothing_validation() -> None:
    """If any entry fails dry-run validation, ZERO writes happen."""
    mgr = MagicMock()
    state = {"call": 0}
    def upsert(deck, model, fields, *, tags=None, stable_guid=None, dry_run=False):
        state["call"] += 1
        if state["call"] == 2:
            raise ValueError("simulated validation failure")
        r = MagicMock()
        r.note_id = 0 if dry_run else 1000 + state["call"]
        r.stable_guid = "anki-manager::xxx"
        r.created = True
        r.dry_run = dry_run
        return r
    mgr.upsert_note.side_effect = upsert
    entries = [_entry(1), _entry(2), _entry(3)]
    result = add_notes(mgr, entries, mode="upsert")
    assert result.created == 0
    assert result.updated == 0
    assert len(result.failed) == 1
    assert result.failed[0]["index"] == 1  # zero-indexed
    assert "ValueError" in result.failed[0]["error"]
    # Only the validation pass ran; no real writes
    assert state["call"] == 3  # all three dry-runs, even though entry 2 failed


def test_add_notes_dry_run_does_not_write() -> None:
    mgr = _fake_mgr_upsert(would_create=True)
    entries = [_entry(1), _entry(2)]
    result = add_notes(mgr, entries, mode="upsert", dry_run=True)
    assert result.dry_run is True
    assert result.created == 2  # would-create counts reflected
    # Every mgr call was dry_run=True
    for call in mgr.upsert_note.call_args_list:
        assert call.kwargs["dry_run"] is True


def test_add_notes_add_only_uses_add_note() -> None:
    mgr = _fake_mgr_add()
    entries = [_entry(1), _entry(2)]
    result = add_notes(mgr, entries, mode="add-only")
    assert result.created == 2
    assert mgr.add_note.call_count == 4  # validate + write
    mgr.upsert_note.assert_not_called()


def test_add_notes_add_only_collision_fails_batch() -> None:
    """In add-only mode, a NoteExistsError from any entry fails the batch."""
    mgr = MagicMock()
    state = {"call": 0}
    def add(deck, model, fields, *, tags=None, stable_guid=None, dry_run=False):
        state["call"] += 1
        if state["call"] == 1:
            raise RuntimeError("note already exists")
        r = MagicMock()
        r.note_id = 0 if dry_run else 1000 + state["call"]
        r.stable_guid = "anki-manager::xxx"
        r.dry_run = dry_run
        return r
    mgr.add_note.side_effect = add
    result = add_notes(mgr, [_entry(1), _entry(2)], mode="add-only")
    assert result.created == 0
    assert len(result.failed) == 1


def test_add_notes_unknown_mode_raises() -> None:
    with pytest.raises(ValueError, match="unknown mode"):
        add_notes(_fake_mgr_upsert(), [_entry(1)], mode="bogus")


def test_add_notes_empty_input() -> None:
    """Zero entries → empty success result, no calls."""
    mgr = _fake_mgr_upsert()
    result = add_notes(mgr, [], mode="upsert")
    assert result.created == 0
    assert result.updated == 0
    assert result.failed == []
    mgr.upsert_note.assert_not_called()


# ---- load_entries dispatch ----


def test_load_entries_requires_exactly_one_source() -> None:
    with pytest.raises(BatchParseError, match="exactly one"):
        batch.load_entries(from_file=None, from_markdown=None, from_stdin=False)
    with pytest.raises(BatchParseError, match="exactly one"):
        batch.load_entries(from_file="a", from_markdown="b", from_stdin=False)


def test_load_entries_from_jsonl_file(tmp_path: Path) -> None:
    p = tmp_path / "notes.jsonl"
    p.write_text('{"deck": "D", "model": "M", "fields": {"Front": "Q"}}\n')
    entries = batch.load_entries(from_file=p, from_markdown=None, from_stdin=False)
    assert len(entries) == 1


def test_load_entries_from_markdown_file(tmp_path: Path) -> None:
    p = tmp_path / "queue.md"
    p.write_text(MARKDOWN_SAMPLE)
    entries = batch.load_entries(from_file=None, from_markdown=p, from_stdin=False)
    assert len(entries) == 2
