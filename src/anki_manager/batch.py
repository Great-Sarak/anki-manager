"""Batch add-notes: validate-then-write a list of note entries from JSONL or queue-style markdown.

All-or-nothing validation: every entry is dry-run through the same code paths the CLI uses
for one-at-a-time writes (allowlist, schema, GUID derivation). If any entry fails
validation, zero entries are written. Errors are reported per-index so the caller can fix
the input and retry.

Supports two input formats:

- **JSON Lines** — one note per line as `{"deck": ..., "model": ..., "fields": {...},
  "tags": [...], "stable_guid": null}`. Blank lines and lines starting with `#` are skipped.
- **Markdown** — anki-translator's queue format. `## Card N — <shape>` blocks separated by
  `---`, with `**FieldName:** value` lines. Deck, Model, Tags lines are extracted as meta;
  everything else (Front, Back, Text, Source, Position, custom fields) goes into the
  fields dict.
"""

from __future__ import annotations

import json
import re
import sys
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import IO, TYPE_CHECKING, Any

if TYPE_CHECKING:
    from .manager import AnkiManager

CARD_HEADER_RE = re.compile(r"^##\s+Card\s+\d+\s+—\s+(.+?)\s*$")
FIELD_LINE_RE = re.compile(r"^\*\*([^:]+):\*\*\s*(.*)$")


@dataclass(frozen=True)
class NoteEntry:
    """One note's worth of input. Maps 1:1 to AnkiManager.add_note / upsert_note kwargs."""
    deck: str
    model: str
    fields: dict[str, str]
    tags: list[str] = field(default_factory=list)
    stable_guid: str | None = None


@dataclass
class BatchResult:
    created: int = 0       # net new notes written
    updated: int = 0       # existing notes whose fields were updated (upsert mode only)
    skipped: int = 0       # entries skipped (e.g. comment lines — distinct from failures)
    failed: list[dict[str, Any]] = field(default_factory=list)
    dry_run: bool = False

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


class BatchParseError(Exception):
    """Raised when a batch input file is malformed enough that no entries can be parsed."""


# ---- parsers ----


def parse_jsonl(stream: IO[str]) -> list[NoteEntry]:
    entries: list[NoteEntry] = []
    for line_num, raw in enumerate(stream, start=1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        try:
            data = json.loads(line)
        except json.JSONDecodeError as e:
            raise BatchParseError(f"line {line_num}: invalid JSON: {e}") from e
        try:
            entries.append(_entry_from_dict(data))
        except (KeyError, TypeError) as e:
            raise BatchParseError(f"line {line_num}: {e}") from e
    return entries


def parse_markdown(stream: IO[str]) -> list[NoteEntry]:
    body = stream.read()
    raw_blocks = body.split("\n---\n")
    entries: list[NoteEntry] = []
    for i, raw in enumerate(raw_blocks):
        raw = raw.strip()
        if not raw or raw.startswith("# "):  # skip top-level headers / empty-queue sentinel
            continue
        try:
            entries.append(_parse_markdown_block(raw))
        except BatchParseError as e:
            raise BatchParseError(f"block {i + 1}: {e}") from e
    return entries


def _parse_markdown_block(raw: str) -> NoteEntry:
    lines = raw.splitlines()
    if not lines or not CARD_HEADER_RE.match(lines[0]):
        raise BatchParseError("missing '## Card N — <shape>' header")

    fields: dict[str, str] = {}
    deck: str | None = None
    model: str | None = None
    tags: list[str] = []

    for line in lines[1:]:
        m = FIELD_LINE_RE.match(line.rstrip())
        if not m:
            continue
        name, value = m.group(1).strip(), m.group(2).strip()
        if name == "Deck":
            deck = value
        elif name == "Model":
            model = value
        elif name == "Tags":
            tags = [t.strip() for t in value.split(",") if t.strip()]
        else:
            fields[name] = value

    if deck is None:
        raise BatchParseError("missing **Deck:** line")
    if model is None:
        raise BatchParseError("missing **Model:** line")
    if not fields:
        raise BatchParseError("no content fields")
    return NoteEntry(deck=deck, model=model, fields=fields, tags=tags)


def _entry_from_dict(data: Any) -> NoteEntry:
    if not isinstance(data, dict):
        raise TypeError(f"entry must be a JSON object, got {type(data).__name__}")
    for required in ("deck", "model", "fields"):
        if required not in data:
            raise KeyError(f"missing required key {required!r}")
    if not isinstance(data["fields"], dict):
        raise TypeError("'fields' must be a JSON object (key→value mapping)")
    return NoteEntry(
        deck=str(data["deck"]),
        model=str(data["model"]),
        fields={k: str(v) for k, v in data["fields"].items()},
        tags=list(data.get("tags") or []),
        stable_guid=data.get("stable_guid"),
    )


# ---- driver ----


def add_notes(
    mgr: "AnkiManager",
    entries: list[NoteEntry],
    *,
    mode: str = "upsert",
    dry_run: bool = False,
) -> BatchResult:
    """Validate every entry, then (if all validate) write every entry.

    mode: 'upsert' (default — idempotent) or 'add-only' (fail on stable_guid collision).

    Validation phase: every entry is run through the matching write method with dry_run=True.
    If any entry raises, the whole batch fails with per-index error details and zero writes
    happen. This is the "all-or-nothing" guarantee the issue body asked for.
    """
    if mode not in ("upsert", "add-only"):
        raise ValueError(f"unknown mode {mode!r}; expected 'upsert' or 'add-only'")

    result = BatchResult(dry_run=dry_run)

    # Phase 1: validate every entry under dry_run. Collect failures.
    for i, entry in enumerate(entries):
        try:
            _call_write(mgr, entry, mode=mode, dry_run=True)
        except Exception as e:  # noqa: BLE001 — re-raise after collecting them all
            result.failed.append({"index": i, "error": f"{type(e).__name__}: {e}"})

    if result.failed:
        return result

    # Phase 2: write for real (unless dry_run was set on the batch).
    if dry_run:
        # Synthesize counts from a second validation pass — upsert needs to distinguish
        # would-create from would-update.
        for entry in entries:
            outcome = _call_write(mgr, entry, mode=mode, dry_run=True)
            if mode == "add-only":
                result.created += 1
            else:
                if outcome.created:
                    result.created += 1
                else:
                    result.updated += 1
        return result

    for i, entry in enumerate(entries):
        try:
            outcome = _call_write(mgr, entry, mode=mode, dry_run=False)
        except Exception as e:  # noqa: BLE001
            result.failed.append({"index": i, "error": f"{type(e).__name__}: {e}"})
            continue
        if mode == "add-only":
            result.created += 1
        else:
            if outcome.created:
                result.created += 1
            else:
                result.updated += 1
    return result


def _call_write(mgr: "AnkiManager", entry: NoteEntry, *, mode: str, dry_run: bool) -> Any:
    """Dispatch to the right typed write on AnkiManager based on mode."""
    kwargs = dict(
        deck=entry.deck,
        model=entry.model,
        fields=entry.fields,
        tags=entry.tags or None,
        stable_guid=entry.stable_guid,
        dry_run=dry_run,
    )
    if mode == "add-only":
        return mgr.add_note(**kwargs)
    return mgr.upsert_note(**kwargs)


def load_entries(*, from_file: Path | str | None, from_markdown: Path | str | None,
                 from_stdin: bool) -> list[NoteEntry]:
    """Top-level dispatch helper for the CLI — exactly one source must be set."""
    sources_set = sum(bool(x) for x in (from_file, from_markdown, from_stdin))
    if sources_set != 1:
        raise BatchParseError(
            "exactly one of --from-file, --from-markdown, or --from-stdin must be specified"
        )
    if from_stdin:
        return parse_jsonl(sys.stdin)
    if from_file:
        p = Path(from_file)
        with p.open("r", encoding="utf-8") as fh:
            return parse_jsonl(fh)
    assert from_markdown is not None
    p = Path(from_markdown)
    with p.open("r", encoding="utf-8") as fh:
        return parse_markdown(fh)
