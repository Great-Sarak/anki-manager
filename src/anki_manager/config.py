from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True)
class Config:
    unit_name: str = "kryshanti-anki.service"
    host: str = "127.0.0.1"
    port: int = 8765
    api_key: str | None = None
    # How long to wait for AnkiConnect to start answering after `systemctl start`.
    # The container needs to boot Xvfb + Anki + load the profile before AnkiConnect
    # starts responding to non-version actions; 60s is a comfortable upper bound.
    ready_timeout: float = 60.0
    rpc_timeout: float = 60.0
