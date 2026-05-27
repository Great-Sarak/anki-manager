from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path


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
    # Cross-process write lock — set lock_path=None to disable (tests).
    lock_path: Path | None = Path("/var/lib/kryshanti-anki/writer.lock")
    lock_timeout: float = 30.0
    # Trigger Anki to create a backup once per AnkiManager instance, before
    # the first add/update/upsert. Retention is governed by Anki's own
    # backup preferences (Tools → Preferences → Backups inside the
    # container), not by us. Disable for short-lived integration tests.
    auto_backup: bool = True
