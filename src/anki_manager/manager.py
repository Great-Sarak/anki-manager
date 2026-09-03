"""High-level AnkiManager — combines lifecycle + anki-rpc + domain checks."""

from __future__ import annotations

from contextlib import contextmanager, nullcontext
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from anki_rpc import Client

from . import permissions
from .allowlist import AgentEntry, Allowlist, DeckNotAllowedError
from .config import Config
from .errors import (
    InvalidNoteError,
    NoteExistsError,
    NoteNotFoundError,
)
from .guid import compute_guid, derive_front, derive_source
from .lifecycle import Lifecycle, Status
from .lock import file_lock


DRY_RUN_NOTE_ID = 0  # sentinel; real Anki note IDs are positive ints


@dataclass(frozen=True)
class AddResult:
    note_id: int           # DRY_RUN_NOTE_ID (0) if dry_run was True
    stable_guid: str
    dry_run: bool = False


@dataclass(frozen=True)
class UpsertResult:
    note_id: int           # existing id when created=False, new id when created=True,
                           # DRY_RUN_NOTE_ID when dry_run was True and would-create
    stable_guid: str
    created: bool          # True if a new note was added, False if an existing one was updated
    dry_run: bool = False


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
        allowlist: Allowlist | None = None,
        agent: AgentEntry | None = None,
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
            broker_socket=self._config.lifecycle_socket,
            broker_timeout=self._config.lifecycle_timeout,
        )
        # Load allowlist lazily so callers can stub it for testing.
        self._allowlist: Allowlist | None = allowlist
        # Agent identity resolution happens at first use, not __init__ —
        # avoids needing the file on import for code that only uses lifecycle.
        self._agent_cached: AgentEntry | None = agent
        self._agent_resolved: bool = agent is not None
        # One backup per AnkiManager instance, taken just before the first
        # mutating write (add/update/upsert).  Tracked here so we don't
        # spam the backup folder on every call.
        self._backup_done: bool = False

    # ------------------------------------------------------------------ #
    # Allowlist access                                                    #
    # ------------------------------------------------------------------ #

    def _get_allowlist(self) -> Allowlist:
        if self._allowlist is None:
            self._allowlist = Allowlist.load()
        return self._allowlist

    def _get_agent(self) -> AgentEntry | None:
        if not self._agent_resolved:
            self._agent_cached = self._get_allowlist().resolve_agent()
            self._agent_resolved = True
        return self._agent_cached

    def _require_allowed(self, deck: str) -> None:
        allowlist = self._get_allowlist()
        agent = self._get_agent()
        if not allowlist.matches(deck, agent):
            agent_name = agent.name if agent else "<no-agent>"
            raise DeckNotAllowedError(
                f"Deck {deck!r} is not in the effective allowlist for agent {agent_name!r}. "
                f"Run `anki-manager permissions add --pattern '{deck}'` to grant access."
            )

    def effective_allowlist(self) -> tuple[str, ...]:
        """Inspect the patterns this AnkiManager will let through."""
        return self._get_allowlist().effective_patterns(self._get_agent())

    # ------------------------------------------------------------------ #
    # Backup                                                              #
    # ------------------------------------------------------------------ #

    def create_backup(self) -> None:
        """Manually trigger a backup.  Sets the session backup flag so the
        next auto-backup hook is a no-op (we only need one per session).
        """
        self._rpc.create_backup()
        self._backup_done = True

    def _auto_backup_if_needed(self, dry_run: bool) -> None:
        """Take the per-session backup before the first real write.

        Skipped when:
          - config.auto_backup is False
          - already taken in this AnkiManager instance
          - the current call is dry_run=True (no real write would happen)
        """
        if dry_run or not self._config.auto_backup or self._backup_done:
            return
        self._rpc.create_backup()
        self._backup_done = True

    # ------------------------------------------------------------------ #
    # Write lock                                                          #
    # ------------------------------------------------------------------ #

    @contextmanager
    def _writer_lock(self):
        """Hold the cross-process writer lock for the GUID-check + RPC-write window.

        Closes the TOCTOU race where two agents both run find_by_guid,
        both see "no existing note", both call addNote with the same
        stable GUID → two notes with the same id.

        Disabled (no-op) when config.lock_path is None — used by unit tests.
        """
        lock_path = self._config.lock_path
        if lock_path is None:
            yield
            return
        with file_lock(lock_path, timeout=self._config.lock_timeout):
            yield

    # ------------------------------------------------------------------ #
    # Lifecycle pass-throughs                                             #
    # ------------------------------------------------------------------ #

    def ensure_running(self) -> None:
        """Start the kryshanti-anki systemd unit if not active, then block until
        AnkiConnect answers. Idempotent — no-op when the unit is already running.

        Raises LifecycleError if systemd refuses to start the unit, or NotReadyError
        if AnkiConnect doesn't answer within config.ready_timeout."""
        self._lifecycle.ensure_running()

    def stop(self) -> None:
        """Stop the kryshanti-anki systemd unit. Idempotent."""
        self._lifecycle.stop()

    def restart(self) -> None:
        """Restart the unit and block until AnkiConnect answers again. Useful after
        an Anki-side config change that needs a fresh process."""
        self._lifecycle.restart()
        self._lifecycle.wait_ready()

    def status(self) -> Status:
        """Return a Status snapshot — active, ready, sub_state. Cheap; no RPC writes."""
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
        """Create the deck, or no-op if it already exists.

        Enforces the allowlist.  If the deck name doesn't match any current
        pattern but the invoking agent has the `<new>` capability, this
        method invokes the privileged helper to append the deck name to
        the agent's section, reloads the allowlist, and retries — so a
        single call adds both the deck and its permission.
        """
        allowlist = self._get_allowlist()
        agent = self._get_agent()
        if not allowlist.matches(name, agent):
            if allowlist.has_new_capability(agent) and agent is not None:
                permissions.add_pattern(agent.name, name)
                # Reload the file (helper just wrote to it) but keep the
                # already-resolved agent identity — re-resolving via
                # _current_username() would defeat caller-provided
                # `agent=` in tests and is unnecessary anyway.
                self._allowlist = Allowlist.load()
                # Re-fetch the agent entry from the new allowlist (its
                # patterns will now include the new deck).
                refreshed = self._allowlist.agents.get(agent.name)
                if refreshed is not None:
                    self._agent_cached = refreshed
                self._require_allowed(name)
            else:
                self._require_allowed(name)  # raises with helpful message
        return self._rpc.add_deck(name)

    def add_note(
        self,
        deck: str,
        model: str,
        fields: dict[str, str],
        *,
        tags: list[str] | None = None,
        stable_guid: str | None = None,
        dry_run: bool = False,
    ) -> AddResult:
        """Add a note with live schema validation + stable GUID tagging.

        Fails fast if:
          - the deck isn't in the effective allowlist (DeckNotAllowedError)
          - the provided fields don't match the model's current schema
            (InvalidNoteError)
          - a note with the same stable GUID already exists
            (NoteExistsError)

        If `stable_guid` is omitted, it is derived from the `source` and
        `front`/`text` fields using `guid.derive_*`.  Pass an explicit
        `stable_guid` when those heuristics don't apply.

        When `dry_run=True`, every check above still runs (so callers can
        validate a batch before writing) but no RPC write happens.  The
        returned AddResult has `dry_run=True` and `note_id=DRY_RUN_NOTE_ID`.
        """
        self._require_allowed(deck)

        available = self._rpc.model_field_names(model)
        if not available:
            raise InvalidNoteError(f"Model {model!r} not found in collection")
        self._validate_fields(model, fields, available)

        if stable_guid is None:
            stable_guid = compute_guid(
                source=derive_source(fields),
                front=derive_front(fields, available),
            )

        self._auto_backup_if_needed(dry_run)

        # Hold the writer lock across the lookup + write to close the
        # TOCTOU race with concurrent agents adding the same GUID.
        # For dry_run we don't need the lock — no write happens — but
        # we still take it so the lookup result is consistent.
        with self._writer_lock():
            existing = self.find_by_guid(stable_guid)
            if existing is not None:
                raise NoteExistsError(
                    f"Note with stable_guid {stable_guid!r} already exists (note_id={existing})"
                )

            if dry_run:
                return AddResult(
                    note_id=DRY_RUN_NOTE_ID, stable_guid=stable_guid, dry_run=True,
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

    def update_note(
        self,
        stable_guid: str,
        fields: dict[str, str],
        *,
        dry_run: bool = False,
    ) -> int:
        """Update fields on the note identified by `stable_guid`.

        Only fields are updated; the note's deck, model, and other tags
        are unchanged.  Raises NoteNotFoundError if no note has this GUID.

        When `dry_run=True`, the GUID lookup still runs (and still raises
        NoteNotFoundError if absent) but no field update happens.  The
        returned int is still the would-update note_id.
        """
        self._auto_backup_if_needed(dry_run)

        with self._writer_lock():
            note_id = self.find_by_guid(stable_guid)
            if note_id is None:
                raise NoteNotFoundError(f"No note found with stable_guid {stable_guid!r}")
            if not dry_run:
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
        dry_run: bool = False,
    ) -> UpsertResult:
        """Add the note if missing, update its fields if present.

        Field-update path: deck/model/non-GUID tags are NOT changed even
        if the call specifies different ones — only the field values are
        synced.  If you need to move a note between decks, use a direct
        `call("changeDeck", ...)` escape hatch.

        Enforces the allowlist on the requested `deck` regardless of
        whether the existing note already lives there — keeps the
        permission check consistent with add_note.

        When `dry_run=True`, validation + lookup run, but neither
        `addNote` nor `updateNoteFields` are called.  `created` reflects
        what WOULD have happened.  `note_id` is the existing id for
        update path, or DRY_RUN_NOTE_ID for create path.
        """
        self._require_allowed(deck)

        available = self._rpc.model_field_names(model)
        if not available:
            raise InvalidNoteError(f"Model {model!r} not found in collection")
        self._validate_fields(model, fields, available)

        if stable_guid is None:
            stable_guid = compute_guid(
                source=derive_source(fields),
                front=derive_front(fields, available),
            )

        self._auto_backup_if_needed(dry_run)

        with self._writer_lock():
            existing = self.find_by_guid(stable_guid)
            if existing is not None:
                if not dry_run:
                    self._rpc.update_note_fields(existing, fields)
                return UpsertResult(
                    note_id=existing, stable_guid=stable_guid,
                    created=False, dry_run=dry_run,
                )

            if dry_run:
                return UpsertResult(
                    note_id=DRY_RUN_NOTE_ID, stable_guid=stable_guid,
                    created=True, dry_run=True,
                )

            all_tags = list(tags or []) + [stable_guid]
            note_id = self._rpc.add_note(deck=deck, model=model, fields=fields, tags=all_tags)
        return UpsertResult(note_id=note_id, stable_guid=stable_guid, created=True)

    def sync(self) -> None:
        """Trigger AnkiWeb sync. Does NOT take the writer lock — sync can run minutes
        and isn't write-critical from the agent's perspective; concurrent agents may
        sync independently."""
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
        """Force AnkiWeb to accept the local collection as authoritative (FULL_SYNC up).
        Destructive — overwrites whatever is on AnkiWeb. Use only when local is known
        good and remote is stale or corrupted."""
        self._rpc.force_upload()

    def force_download(self) -> None:
        """Force AnkiWeb to overwrite the local collection (FULL_SYNC down). Destructive —
        any local changes not yet synced will be lost. Use only when remote is known
        good and local is stale or corrupted."""
        self._rpc.force_download()

    # ------------------------------------------------------------------ #
    # Escape hatch                                                        #
    # ------------------------------------------------------------------ #

    def call(self, action: str, **params: Any) -> Any:
        """Generic AnkiConnect call passthrough — for anything not in the typed surface."""
        return self._rpc.call(action, **params)
