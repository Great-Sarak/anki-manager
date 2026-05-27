from __future__ import annotations

import subprocess
from collections.abc import Callable
from typing import Any
from unittest.mock import MagicMock

import pytest


def make_runner(scripts: dict[tuple[str, ...], subprocess.CompletedProcess[str]]) -> Callable[..., subprocess.CompletedProcess[str]]:
    """Build a fake systemctl runner from a {(args,): CompletedProcess} mapping.

    Unmapped calls raise — tests must enumerate every call they expect.
    """
    calls: list[tuple[str, ...]] = []

    def runner(*args: str, **kwargs: Any) -> subprocess.CompletedProcess[str]:
        calls.append(args)
        if args not in scripts:
            raise AssertionError(f"unexpected systemctl call: {args!r}")
        return scripts[args]

    runner.calls = calls  # type: ignore[attr-defined]
    return runner


def cp(stdout: str = "", stderr: str = "", returncode: int = 0) -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess(
        args=[], returncode=returncode, stdout=stdout, stderr=stderr,
    )


@pytest.fixture
def fake_client():
    return MagicMock()
