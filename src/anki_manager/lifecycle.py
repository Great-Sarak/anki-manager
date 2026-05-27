"""systemctl wrapper + AnkiConnect readiness wait.

`Lifecycle` is intentionally minimal — it shells out to `systemctl` and
polls AnkiConnect via the supplied `anki_rpc.Client`.  All long-lived
state lives in systemd (the unit) and the AnkiConnect container; this
module just gives the rest of `anki-manager` a typed surface over those
two side-effecting boundaries.
"""

from __future__ import annotations

import subprocess
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from anki_rpc import AnkiConnectError, Client

from .errors import LifecycleError, NotReadyError

# Injectable subprocess runner for testing.  Signature matches subprocess.run.
SystemctlRunner = Callable[..., subprocess.CompletedProcess[str]]


def _default_runner(*args: str, **kwargs: Any) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["systemctl", *args],
        capture_output=True,
        text=True,
        check=False,
        **kwargs,
    )


@dataclass
class Status:
    active: bool       # `systemctl is-active` returns "active"
    ready: bool        # AnkiConnect on host:port answers `version` action
    sub_state: str     # e.g. "running", "dead", "failed"


class Lifecycle:
    def __init__(
        self,
        unit_name: str,
        client: Client,
        *,
        ready_timeout: float = 60.0,
        runner: SystemctlRunner | None = None,
    ) -> None:
        self._unit = unit_name
        self._client = client
        self._ready_timeout = ready_timeout
        self._run = runner or _default_runner

    # ------------------------------------------------------------------ #
    # systemctl ops                                                       #
    # ------------------------------------------------------------------ #

    def start(self) -> None:
        self._systemctl("start", self._unit)

    def stop(self) -> None:
        self._systemctl("stop", self._unit)

    def restart(self) -> None:
        self._systemctl("restart", self._unit)

    def is_active(self) -> bool:
        result = self._run("is-active", self._unit)
        return result.stdout.strip() == "active"

    def sub_state(self) -> str:
        result = self._run("show", self._unit, "--property=SubState", "--value")
        return result.stdout.strip()

    # ------------------------------------------------------------------ #
    # AnkiConnect readiness                                                #
    # ------------------------------------------------------------------ #

    def is_ready(self) -> bool:
        """True if AnkiConnect responds AND the collection is loaded.

        AnkiConnect's HTTP server starts answering before the profile is
        opened, so a non-erroring `deckNames` is the cheapest end-to-end
        probe (verified in the Phase 1 spike findings).
        """
        try:
            self._client.deck_names()
            return True
        except (AnkiConnectError, OSError):
            return False

    def wait_ready(self, timeout: float | None = None) -> None:
        deadline = time.monotonic() + (timeout if timeout is not None else self._ready_timeout)
        while time.monotonic() < deadline:
            if self.is_ready():
                return
            time.sleep(0.5)
        raise NotReadyError(
            f"AnkiConnect not ready within {self._ready_timeout}s "
            f"(unit={self._unit}, sub_state={self.sub_state()})"
        )

    # ------------------------------------------------------------------ #
    # High-level                                                           #
    # ------------------------------------------------------------------ #

    def ensure_running(self) -> None:
        """Idempotent: start the unit if not active, then wait for ready."""
        if not self.is_active():
            self.start()
        self.wait_ready()

    def status(self) -> Status:
        return Status(
            active=self.is_active(),
            ready=self.is_ready(),
            sub_state=self.sub_state(),
        )

    # ------------------------------------------------------------------ #
    # Internal                                                             #
    # ------------------------------------------------------------------ #

    def _systemctl(self, *args: str) -> None:
        result = self._run(*args)
        if result.returncode != 0:
            raise LifecycleError(
                f"systemctl {' '.join(args)} failed (rc={result.returncode}): "
                f"{result.stderr.strip() or result.stdout.strip()}"
            )
