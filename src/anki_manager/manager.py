"""High-level AnkiManager — combines lifecycle + anki-rpc + domain checks."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from anki_rpc import Client

from .config import Config
from .errors import (
    InvalidNoteError,
    NoteExistsError,
    NoteNotFoundError,
)
from .guid import compute_guid, derive_front, derive_source
from .lifecycle import Lifecycle, Status


@dataclass(frozen=True)
class AddResult:
    note_id: int
    stable_guid: str


@dataclass(frozen=True)
class UpsertResult:
    note_id: int
    stable_guid: str
    created: bool  # True if a new note was added, False if an existing one was updated


class AnkiManager:
    """Owns the local Anki container's lifecycle and provides the
    domain-validated note/deck/sync surface that Layer 3 (AnkiTranslator)
    or any other fleet caller talks to.
    """

    def __init__(
        self,
        config: Config | None = None,
        *,
        client: Client | None = None,
        lifecycle: Lifecycle | None = None,
    ) -> None:
        self._config = config or Config()
        self._rpc = client or Client(
            host=self._config.host,
            port=self._config.port,
            api_key=self._config.api_key,
            timeout=self._config.rpc_timeout,
        )
        self._lifecycle = lifecycle or Lifecycle(
            unit_name=self._config.unit_name,
            client=self._rpc,
            ready_timeout=self._config.ready_timeout,
        )

    # ------------------------------------------------------------------ #
    # Lifecycle pass-throughs                                             #
    # ------------------------------------------------------------------ #

    def ensure_running(self) -> None:
        self._lifecycle.ensure_running()

    def stop(self) -> None:
        self._lifecycle.stop()

    def restart(self) -> None:
        self._lifecycle.restart()
        self._lifecycle.wait_ready()

    def status(self) -> Status:
        return self._lifecycle.status()

    # ------------------------------------------------------------------ #
    # Domain ops                                                          #
    # ------------------------------------------------------------------ #

    def list_models(self) -> dict[str, list[str]]:
        """Returns {model_name: [field_names]} for every model in the collection.

        Per plan: schema is queried **live** every time, never cached across
        sessions — a rename in Anki Desktop would silently break a cache.
        """
        models = self._rpc.model_names()
        return {name: self._rpc.model_field_names(name) for name in models}

    def add_deck(self, name: str) -> int:
        return self._rpc.add_deck(name)

    def add_note(
        self,
        deck: str,
        model: str,
        fields: dict[str, str],
        *,
        tags: list[str] | None = None,
        stable_guid: str | None = None,
    ) -> AddResult:
        """Add a note with live schema validation + stable GUID tagging.

        Fails fast if:
          - the provided fields don't match the model's current schema
            (InvalidNoteError)
          - a note with the same stable GUID already exists
            (NoteExistsError)

        If `stable_guid` is omitted, it is derived from the `source` and
        `front`/`text` fields using `guid.derive_*`.  Pass an explicit
        `stable_guid` when those heuristics don't apply.
        """
        available = self._rpc.model_field_names(model)
        if not available:
            raise InvalidNoteError(f"Model {model!r} not found in collection")
        self._validate_fields(model, fields, available)

        if stable_guid is None:
            stable_guid = compute_guid(
                source=derive_source(fields),
                front=derive_front(fields, available),
            )

        existing = self.find_by_guid(stable_guid)
        if existing is not None:
            raise NoteExistsError(
                f"Note with stable_guid {stable_guid!r} already exists (note_id={existing})"
            )

        all_tags = list(tags or []) + [stable_guid]
        note_id = self._rpc.add_note(deck=deck, model=model, fields=fields, tags=all_tags)
        return AddResult(note_id=note_id, stable_guid=stable_guid)

    def find_by_guid(self, stable_guid: str) -> int | None:
        """Returns the note_id for the given stable GUID, or None if not found."""
        # GUID is a tag of the form  anki-manager::<hash>.  Search by exact tag.
        matches = self._rpc.find_notes(f"tag:{stable_guid}")
        if not matches:
            return None
        if len(matches) > 1:
            # Should be impossible if all writes go through this manager, but
            # better to surface than to silently pick one.
            raise NoteExistsError(
                f"Multiple notes share stable_guid {stable_guid!r}: {matches}"
            )
        return matches[0]

    def update_note(self, stable_guid: str, fields: dict[str, str]) -> int:
        """Update fields on the note identified by `stable_guid`.

        Only fields are updated; the note's deck, model, and other tags
        are unchanged.  Raises NoteNotFoundError if no note has this GUID.
        """
        note_id = self.find_by_guid(stable_guid)
        if note_id is None:
            raise NoteNotFoundError(f"No note found with stable_guid {stable_guid!r}")
        self._rpc.update_note_fields(note_id, fields)
        return note_id

    def upsert_note(
        self,
        deck: str,
        model: str,
        fields: dict[str, str],
        *,
        tags: list[str] | None = None,
        stable_guid: str | None = None,
    ) -> UpsertResult:
        """Add the note if missing, update its fields if present.

        Field-update path: deck/model/non-GUID tags are NOT changed even
        if the call specifies different ones — only the field values are
        synced.  If you need to move a note between decks, use a direct
        `call("changeDeck", ...)` escape hatch.
        """
        available = self._rpc.model_field_names(model)
        if not available:
            raise InvalidNoteError(f"Model {model!r} not found in collection")
        self._validate_fields(model, fields, available)

        if stable_guid is None:
            stable_guid = compute_guid(
                source=derive_source(fields),
                front=derive_front(fields, available),
            )

        existing = self.find_by_guid(stable_guid)
        if existing is not None:
            self._rpc.update_note_fields(existing, fields)
            return UpsertResult(note_id=existing, stable_guid=stable_guid, created=False)

        all_tags = list(tags or []) + [stable_guid]
        note_id = self._rpc.add_note(deck=deck, model=model, fields=fields, tags=all_tags)
        return UpsertResult(note_id=note_id, stable_guid=stable_guid, created=True)

    def sync(self) -> None:
        self._rpc.sync()

    def _validate_fields(
        self,
        model: str,
        fields: dict[str, str],
        available: list[str],
    ) -> None:
        available_set = set(available)
        provided_set = set(fields.keys())

        extra = provided_set - available_set
        if extra:
            raise InvalidNoteError(
                f"Model {model!r} has no fields named: {sorted(extra)}. "
                f"Available fields: {available}"
            )

        missing = available_set - provided_set
        if missing:
            raise InvalidNoteError(
                f"Model {model!r} requires fields not provided: {sorted(missing)}"
            )

    def force_upload(self) -> None:
        self._rpc.force_upload()

    def force_download(self) -> None:
        self._rpc.force_download()

    # ------------------------------------------------------------------ #
    # Escape hatch                                                        #
    # ------------------------------------------------------------------ #

    def call(self, action: str, **params: Any) -> Any:
        """Generic AnkiConnect call passthrough — for anything not in the typed surface."""
        return self._rpc.call(action, **params)
