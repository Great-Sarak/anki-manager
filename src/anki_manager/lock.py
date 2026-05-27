"""Cross-process advisory lock via fcntl.flock.

Used by AnkiManager to serialise the find_by_guid → add_note / update
window across concurrent agents writing to the same Anki collection.

Without it, two agents could both run find_by_guid → see no existing
note → both call add_note with the same stable GUID → produce two notes
with the same identifier (a TOCTOU bug).  The lock collapses that
window so the second writer either sees the first's note (and switches
to update via upsert) or raises NoteExistsError on a plain add.
"""

from __future__ import annotations

import errno
import fcntl
import os
import time
from contextlib import contextmanager
from pathlib import Path

from .errors import AnkiManagerError


class LockTimeoutError(AnkiManagerError):
    """Raised when the writer lock cannot be acquired within the timeout."""


@contextmanager
def file_lock(path: Path, *, timeout: float = 30.0, poll_interval: float = 0.05):
    """Exclusive flock with timeout.

    Creates the lock file if it doesn't exist (mode 0664).  Returns
    when LOCK_EX is held; raises LockTimeoutError if the lock can't be
    acquired before `timeout` elapses.  The lock is released on
    context-manager exit, even on exception.
    """
    try:
        fd = os.open(path, os.O_RDWR | os.O_CREAT, 0o664)
    except (FileNotFoundError, PermissionError) as exc:
        raise AnkiManagerError(
            f"writer lock at {path} is not accessible ({exc}). "
            f"Run host-setup.sh to create it with the correct group ownership, "
            f"or pass lock_path=None to disable locking (single-process only)."
        ) from exc
    try:
        deadline = time.monotonic() + timeout
        while True:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except OSError as exc:
                if exc.errno not in (errno.EAGAIN, errno.EACCES):
                    raise
                if time.monotonic() >= deadline:
                    raise LockTimeoutError(
                        f"failed to acquire {path} within {timeout}s"
                    ) from None
                time.sleep(poll_interval)
        try:
            yield
        finally:
            fcntl.flock(fd, fcntl.LOCK_UN)
    finally:
        os.close(fd)
