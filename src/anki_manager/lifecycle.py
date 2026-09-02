"""Host systemd and narrow-broker lifecycle backends plus readiness wait."""

from __future__ import annotations

import json
import socket
import subprocess
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

from anki_rpc import AnkiConnectError, Client

from .errors import LifecycleError, NotReadyError

SystemctlRunner = Callable[..., subprocess.CompletedProcess[str]]
MAX_BROKER_RESPONSE_BYTES = 16384


def _default_runner(*args: str, **kwargs: Any) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["systemctl", *args],
        capture_output=True,
        text=True,
        check=False,
        **kwargs,
    )


@dataclass(frozen=True)
class Status:
    active: bool
    ready: bool
    sub_state: str


@dataclass(frozen=True)
class UnitState:
    active: bool
    sub_state: str


class LifecycleBackend(Protocol):
    def start(self) -> None: ...

    def stop(self) -> None: ...

    def restart(self) -> None: ...

    def is_active(self) -> bool: ...

    def sub_state(self) -> str: ...

    def snapshot(self) -> UnitState: ...


class SystemctlBackend:
    def __init__(self, unit: str, runner: SystemctlRunner = _default_runner) -> None:
        self._unit = unit
        self._run = runner

    def start(self) -> None:
        self._action("start")

    def stop(self) -> None:
        self._action("stop")

    def restart(self) -> None:
        self._action("restart")

    def is_active(self) -> bool:
        result = self._run("is-active", self._unit)
        return result.stdout.strip() == "active"

    def sub_state(self) -> str:
        args = ("show", self._unit, "--property=SubState", "--value")
        result = self._run(*args)
        if result.returncode != 0:
            self._raise_failure(result, *args)
        return result.stdout.strip()

    def snapshot(self) -> UnitState:
        return UnitState(active=self.is_active(), sub_state=self.sub_state())

    def _action(self, action: str) -> None:
        result = self._run(action, self._unit)
        if result.returncode != 0:
            self._raise_failure(result, action, self._unit)

    @staticmethod
    def _raise_failure(result: subprocess.CompletedProcess[str], *args: str) -> None:
        raise LifecycleError(
            f"systemctl {' '.join(args)} failed (rc={result.returncode}): "
            f"{result.stderr.strip() or result.stdout.strip()}"
        )


class BrokerBackend:
    def __init__(self, unit: str, socket_path: Path, timeout: float = 30.0) -> None:
        self._unit = unit
        self._socket_path = socket_path
        self._timeout = timeout

    def start(self) -> None:
        self._request("start")

    def stop(self) -> None:
        self._request("stop")

    def restart(self) -> None:
        self._request("restart")

    def is_active(self) -> bool:
        return self.snapshot().active

    def sub_state(self) -> str:
        return self.snapshot().sub_state

    def snapshot(self) -> UnitState:
        result = self._request("status")
        active = result.get("active")
        sub_state = result.get("sub_state")
        if not isinstance(active, bool) or not isinstance(sub_state, str):
            raise LifecycleError("lifecycle broker returned an invalid status result")
        return UnitState(active=active, sub_state=sub_state)

    def _request(self, action: str) -> dict[str, Any]:
        payload = json.dumps(
            {"version": 1, "unit": self._unit, "action": action},
            separators=(",", ":"),
        ).encode("utf-8") + b"\n"
        try:
            with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
                client.settimeout(self._timeout)
                client.connect(str(self._socket_path))
                client.sendall(payload)
                response = self._read_response(client)
        except (OSError, TimeoutError) as exc:
            raise LifecycleError(
                f"lifecycle broker unavailable at {self._socket_path}: {exc}"
            ) from exc

        try:
            decoded = json.loads(response)
        except (json.JSONDecodeError, UnicodeDecodeError) as exc:
            raise LifecycleError("lifecycle broker returned invalid JSON") from exc
        if not isinstance(decoded, dict) or not isinstance(decoded.get("ok"), bool):
            raise LifecycleError("lifecycle broker returned an invalid response")
        if not decoded["ok"]:
            error = decoded.get("error")
            message = error.get("message") if isinstance(error, dict) else None
            raise LifecycleError(
                f"lifecycle broker rejected {action}: {message or 'unknown error'}"
            )
        result = decoded.get("result")
        if not isinstance(result, dict):
            raise LifecycleError("lifecycle broker returned an invalid result")
        return result

    @staticmethod
    def _read_response(client: socket.socket) -> bytes:
        chunks: list[bytes] = []
        size = 0
        while True:
            chunk = client.recv(min(4096, MAX_BROKER_RESPONSE_BYTES + 1 - size))
            if not chunk:
                break
            chunks.append(chunk)
            size += len(chunk)
            if size > MAX_BROKER_RESPONSE_BYTES:
                raise LifecycleError("lifecycle broker response exceeds limit")
            if b"\n" in chunk:
                break
        response = b"".join(chunks)
        if not response.endswith(b"\n"):
            raise LifecycleError("lifecycle broker response is incomplete")
        return response


class Lifecycle:
    def __init__(
        self,
        unit_name: str,
        client: Client,
        *,
        ready_timeout: float = 60.0,
        runner: SystemctlRunner | None = None,
        broker_socket: Path | None = None,
        broker_timeout: float = 30.0,
        backend: LifecycleBackend | None = None,
    ) -> None:
        self._unit = unit_name
        self._client = client
        self._ready_timeout = ready_timeout
        if backend is not None:
            self._backend = backend
        elif runner is not None:
            self._backend = SystemctlBackend(unit_name, runner)
        elif broker_socket is not None:
            self._backend = BrokerBackend(unit_name, broker_socket, broker_timeout)
        else:
            self._backend = SystemctlBackend(unit_name)

    def start(self) -> None:
        self._backend.start()

    def stop(self) -> None:
        self._backend.stop()

    def restart(self) -> None:
        self._backend.restart()

    def is_active(self) -> bool:
        return self._backend.is_active()

    def sub_state(self) -> str:
        return self._backend.sub_state()

    def is_ready(self) -> bool:
        """True if AnkiConnect responds and the collection is loaded."""
        try:
            self._client.deck_names()
            return True
        except (AnkiConnectError, OSError):
            return False

    def wait_ready(self, timeout: float | None = None) -> None:
        effective_timeout = timeout if timeout is not None else self._ready_timeout
        deadline = time.monotonic() + effective_timeout
        while time.monotonic() < deadline:
            if self.is_ready():
                return
            time.sleep(0.5)
        raise NotReadyError(
            f"AnkiConnect not ready within {effective_timeout}s "
            f"(unit={self._unit}, sub_state={self.sub_state()})"
        )

    def ensure_running(self) -> None:
        if not self.is_active():
            self.start()
        self.wait_ready()

    def status(self) -> Status:
        state = self._backend.snapshot()
        return Status(
            active=state.active,
            ready=self.is_ready(),
            sub_state=state.sub_state,
        )
