"""High-level AnkiManager — combines lifecycle + anki-rpc + domain checks."""

from __future__ import annotations

from typing import Any

from anki_rpc import Client

from .config import Config
from .errors import InvalidNoteError
from .lifecycle import Lifecycle, Status


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
    ) -> int:
        """Add a note with live schema validation.

        Fails fast if the provided fields don't match the model's current
        schema — protects against silent corruption from drifted field
        names.
        """
        available = self._rpc.model_field_names(model)
        if not available:
            raise InvalidNoteError(f"Model {model!r} not found in collection")

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

        return self._rpc.add_note(deck=deck, model=model, fields=fields, tags=tags)

    def sync(self) -> None:
        self._rpc.sync()

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
