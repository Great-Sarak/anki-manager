from __future__ import annotations

import multiprocessing
import os
import time
from pathlib import Path

import pytest

from anki_manager import LockTimeoutError, file_lock


def _hold_lock(path: str, hold_seconds: float, ready_event, done_event) -> None:
    """Worker: acquire the lock, signal ready, hold, release."""
    with file_lock(Path(path), timeout=5.0):
        ready_event.set()
        time.sleep(hold_seconds)
    done_event.set()


class TestFileLock:
    def test_acquires_when_uncontended(self, tmp_path):
        lock = tmp_path / "test.lock"
        with file_lock(lock, timeout=1.0):
            pass
        # Re-acquire after release should also work
        with file_lock(lock, timeout=1.0):
            pass

    def test_creates_lock_file_if_missing(self, tmp_path):
        lock = tmp_path / "fresh.lock"
        assert not lock.exists()
        with file_lock(lock):
            assert lock.exists()

    def test_times_out_when_contended(self, tmp_path):
        lock = tmp_path / "contended.lock"
        ctx = multiprocessing.get_context("fork")
        ready = ctx.Event()
        done = ctx.Event()
        proc = ctx.Process(
            target=_hold_lock, args=(str(lock), 2.0, ready, done),
        )
        proc.start()
        try:
            assert ready.wait(timeout=3), "worker never acquired lock"
            # Worker is holding the lock for ~2s; try to acquire with 0.3s timeout
            t0 = time.monotonic()
            with pytest.raises(LockTimeoutError, match="contended.lock"):
                with file_lock(lock, timeout=0.3, poll_interval=0.02):
                    pass
            elapsed = time.monotonic() - t0
            # Should have given up after ~0.3s (allow generous slack)
            assert 0.25 <= elapsed < 1.0, f"timeout took {elapsed}s"
        finally:
            assert done.wait(timeout=5)
            proc.join(timeout=2)

    def test_acquires_after_holder_releases(self, tmp_path):
        lock = tmp_path / "release.lock"
        ctx = multiprocessing.get_context("fork")
        ready = ctx.Event()
        done = ctx.Event()
        proc = ctx.Process(
            target=_hold_lock, args=(str(lock), 0.2, ready, done),
        )
        proc.start()
        try:
            assert ready.wait(timeout=3)
            # Wait with a generous timeout — should acquire shortly after worker releases
            with file_lock(lock, timeout=2.0, poll_interval=0.02):
                pass
        finally:
            proc.join(timeout=5)

    def test_releases_on_exception(self, tmp_path):
        lock = tmp_path / "exception.lock"

        class Boom(Exception):
            pass

        with pytest.raises(Boom):
            with file_lock(lock, timeout=1.0):
                raise Boom()

        # Lock should be released — re-acquire promptly
        with file_lock(lock, timeout=0.5):
            pass


def _try_add(path: str, result_queue) -> None:
    """Worker: under the writer lock, add a fake note unless one already exists."""
    from anki_manager import file_lock as _file_lock
    seen_existing = False
    with _file_lock(Path(path), timeout=5.0):
        # Simulate the find_by_guid → add_note window
        marker = Path(path).with_suffix(".marker")
        if marker.exists():
            seen_existing = True
        else:
            time.sleep(0.1)  # widen the would-be TOCTOU race
            marker.write_text("written")
    result_queue.put(seen_existing)


class TestWriterLockSerialization:
    def test_serialises_concurrent_writers(self, tmp_path):
        """Two workers race; lock ensures the second sees the first's marker."""
        lock = tmp_path / "writer.lock"
        marker = lock.with_suffix(".marker")
        assert not marker.exists()

        ctx = multiprocessing.get_context("fork")
        q = ctx.Queue()
        procs = [ctx.Process(target=_try_add, args=(str(lock), q)) for _ in range(2)]
        for p in procs:
            p.start()
        for p in procs:
            p.join(timeout=10)

        results = [q.get() for _ in range(2)]
        # Exactly one worker should have written, the other should have seen the marker
        assert sorted(results) == [False, True], f"got {results}"
        assert marker.exists()
