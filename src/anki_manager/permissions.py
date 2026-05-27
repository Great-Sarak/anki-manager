"""Privileged allowlist mutations — pkexec invocations to the
`grant-deck` helper installed by host-setup.sh at
/usr/local/libexec/kryshanti-anki/grant-deck.

The helper does the actual file write as root after validating the
request.  This module is a thin shell that calls it.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

from .errors import PermissionsHelperError

DEFAULT_HELPER_PATH = Path("/usr/local/libexec/kryshanti-anki/grant-deck")


def _invoke(args: list[str], helper: Path = DEFAULT_HELPER_PATH) -> subprocess.CompletedProcess[str]:
    if not helper.exists():
        raise PermissionsHelperError(
            f"grant-deck helper not found at {helper}. Did host-setup.sh run successfully?"
        )
    result = subprocess.run(
        ["pkexec", str(helper), *args],
        capture_output=True, text=True, check=False,
    )
    if result.returncode != 0:
        raise PermissionsHelperError(
            f"grant-deck {' '.join(args)} failed (rc={result.returncode}): "
            f"{result.stderr.strip() or result.stdout.strip()}"
        )
    return result


def add_pattern(section: str, pattern: str, *, helper: Path = DEFAULT_HELPER_PATH) -> None:
    _invoke(["add", "--section", section, "--pattern", pattern], helper=helper)


def remove_pattern(section: str, pattern: str, *, helper: Path = DEFAULT_HELPER_PATH) -> None:
    _invoke(["remove", "--section", section, "--pattern", pattern], helper=helper)


def grant_new(section: str, *, helper: Path = DEFAULT_HELPER_PATH) -> None:
    _invoke(["grant-new", "--section", section], helper=helper)


def revoke_new(section: str, *, helper: Path = DEFAULT_HELPER_PATH) -> None:
    _invoke(["revoke-new", "--section", section], helper=helper)
