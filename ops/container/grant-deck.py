#!/usr/bin/env python3
"""grant-deck — privileged allowlist editor for kryshanti-anki.

Installed by host-setup.sh at /usr/local/libexec/kryshanti-anki/grant-deck.
Invoked via pkexec by anki-manager when an agent's section has the <new>
capability and needs to extend itself, or when running the
`anki-manager permissions ...` subcommands.

Polkit rule (also installed by host-setup.sh) allows members of
kryshanti-anki-users to run this specific binary without a password.

Validates everything server-side:
  - The pattern matches a strict character whitelist
  - The section is "universal" or a known agent section
  - For agent-section mutations: the invoker (via PKEXEC_UID) must
    resolve to that same agent through the allowlist's aliases
  - For "universal" or new-section mutations: the invoker must be root,
    because those changes affect every agent
"""

from __future__ import annotations

import argparse
import os
import pwd
import re
import sys
import tomllib
from pathlib import Path
from tempfile import NamedTemporaryFile

ALLOWLIST_PATH = Path("/var/lib/kryshanti-anki/allowlist.toml")
NEW_SENTINEL = "<new>"
UNIVERSAL = "universal"

# Only allow patterns made of:  ASCII letters/digits + : * - _ space
# Forbids quotes, newlines, path separators, and shell metacharacters.
PATTERN_RE = re.compile(r"^[A-Za-z0-9_:\*\- ]+$")
SECTION_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_\-]*$")


def die(msg: str, code: int = 1) -> None:
    print(f"grant-deck: {msg}", file=sys.stderr)
    sys.exit(code)


# ---------------------------------------------------------------- #
# Validation helpers                                                #
# ---------------------------------------------------------------- #


def validate_pattern(pattern: str) -> None:
    if not pattern:
        die("pattern must not be empty")
    if len(pattern) > 256:
        die("pattern too long (max 256 chars)")
    if not PATTERN_RE.match(pattern):
        die(f"pattern contains disallowed characters: {pattern!r}")


def validate_section_name(section: str) -> None:
    if section == UNIVERSAL:
        return
    if not SECTION_RE.match(section):
        die(f"section name must be alphanumeric/dash/underscore: {section!r}")


def invoker_username() -> str:
    """Resolve the original invoking user's name via PKEXEC_UID."""
    uid_str = os.environ.get("PKEXEC_UID")
    if uid_str is None:
        # Direct invocation as root (e.g. by sysadmin, not via pkexec).
        # Allowed; treated as elevated authority.
        return "root"
    try:
        uid = int(uid_str)
    except ValueError:
        die(f"PKEXEC_UID is not an integer: {uid_str!r}")
    try:
        return pwd.getpwuid(uid).pw_name
    except KeyError:
        die(f"PKEXEC_UID {uid} doesn't resolve to a username")


def load_allowlist() -> dict:
    if not ALLOWLIST_PATH.exists():
        die(f"allowlist not found at {ALLOWLIST_PATH}")
    try:
        return tomllib.loads(ALLOWLIST_PATH.read_text(encoding="utf-8"))
    except tomllib.TOMLDecodeError as exc:
        die(f"failed to parse {ALLOWLIST_PATH}: {exc}")


def resolve_invoker_agent(data: dict, username: str) -> str | None:
    """Return the agent section name claiming this username, or None."""
    matches: list[str] = []
    for key, section in data.items():
        if not isinstance(section, dict):
            continue
        aliases = section.get("aliases") or []
        if username in aliases:
            matches.append(key)
    if len(matches) > 1:
        die(f"user {username!r} matches multiple sections {matches}; allowlist is ambiguous")
    return matches[0] if matches else None


def authorize(section: str, data: dict, invoker: str) -> None:
    """Raise (via die) if invoker is not allowed to mutate `section`."""
    if invoker == "root":
        return  # direct root invocation = full authority
    if section == UNIVERSAL:
        die("only root may mutate the universal section")
    invoker_agent = resolve_invoker_agent(data, invoker)
    if invoker_agent is None:
        die(f"user {invoker!r} has no agent section; cannot mutate {section!r}")
    if invoker_agent != section:
        die(
            f"user {invoker!r} resolves to agent {invoker_agent!r}, "
            f"not authorized to mutate section {section!r}"
        )


# ---------------------------------------------------------------- #
# Mutators                                                          #
# ---------------------------------------------------------------- #


def add_pattern(data: dict, section: str, pattern: str) -> None:
    if section == UNIVERSAL:
        lst = data.setdefault(UNIVERSAL, [])
        if pattern not in lst:
            lst.append(pattern)
        return
    sec = data.setdefault(section, {})
    if not isinstance(sec, dict):
        die(f"section [{section}] in allowlist is not a table")
    allowed = sec.setdefault("allowed", [])
    if pattern not in allowed:
        allowed.append(pattern)


def remove_pattern(data: dict, section: str, pattern: str) -> None:
    if section == UNIVERSAL:
        lst = data.get(UNIVERSAL) or []
        if pattern in lst:
            lst.remove(pattern)
        return
    sec = data.get(section)
    if not isinstance(sec, dict):
        die(f"section [{section}] not found")
    allowed = sec.get("allowed") or []
    if pattern in allowed:
        allowed.remove(pattern)


def grant_new(data: dict, section: str) -> None:
    if section == UNIVERSAL:
        die("<new> capability is per-agent, not universal")
    add_pattern(data, section, NEW_SENTINEL)


def revoke_new(data: dict, section: str) -> None:
    remove_pattern(data, section, NEW_SENTINEL)


# ---------------------------------------------------------------- #
# TOML emitter (purpose-built; preserves our exact shape)          #
# ---------------------------------------------------------------- #


def _toml_string(s: str) -> str:
    # Our patterns + aliases are constrained by validate_pattern /
    # SECTION_RE so they cannot contain " or \ — safe to wrap in basic
    # double quotes without escaping.
    return f'"{s}"'


def emit(data: dict) -> str:
    lines: list[str] = []
    # Top-level `universal` first
    universal = data.get(UNIVERSAL, [])
    if universal:
        lines.append("universal = [")
        for entry in universal:
            lines.append(f"    {_toml_string(entry)},")
        lines.append("]")
    else:
        lines.append("universal = []")
    lines.append("")

    # Section tables, in sorted order for stability
    for key in sorted(k for k in data.keys() if k != UNIVERSAL):
        section = data[key]
        if not isinstance(section, dict):
            continue
        lines.append(f"[{key}]")
        allowed = section.get("allowed", [])
        lines.append("allowed = [")
        for entry in allowed:
            lines.append(f"    {_toml_string(entry)},")
        lines.append("]")
        aliases = section.get("aliases", [])
        lines.append("aliases = [")
        for entry in aliases:
            lines.append(f"    {_toml_string(entry)},")
        lines.append("]")
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"


def write_atomic(content: str) -> None:
    # Write to a sibling tempfile, fsync, rename — survives crashes
    # mid-write without corrupting the original.
    parent = ALLOWLIST_PATH.parent
    with NamedTemporaryFile(
        mode="w", encoding="utf-8", dir=parent, delete=False,
        prefix=".allowlist-", suffix=".tmp",
    ) as tmp:
        tmp.write(content)
        tmp.flush()
        os.fsync(tmp.fileno())
        tmp_path = Path(tmp.name)
    try:
        os.chmod(tmp_path, 0o644)
        os.chown(tmp_path, os.stat(ALLOWLIST_PATH).st_uid, os.stat(ALLOWLIST_PATH).st_gid)
        os.replace(tmp_path, ALLOWLIST_PATH)
    except Exception:
        tmp_path.unlink(missing_ok=True)
        raise


# ---------------------------------------------------------------- #
# Entry point                                                       #
# ---------------------------------------------------------------- #


def main() -> int:
    parser = argparse.ArgumentParser(prog="grant-deck")
    sub = parser.add_subparsers(dest="cmd", required=True)

    def add_section_arg(p: argparse.ArgumentParser) -> None:
        p.add_argument("--section", required=True,
                       help="Section name: an agent name like 'Myrzka' or 'universal'.")

    p_add = sub.add_parser("add", help="Append a pattern to a section")
    add_section_arg(p_add)
    p_add.add_argument("--pattern", required=True)

    p_remove = sub.add_parser("remove", help="Remove a pattern from a section")
    add_section_arg(p_remove)
    p_remove.add_argument("--pattern", required=True)

    p_grant = sub.add_parser("grant-new", help="Add the <new> capability to an agent")
    add_section_arg(p_grant)

    p_revoke = sub.add_parser("revoke-new", help="Remove the <new> capability from an agent")
    add_section_arg(p_revoke)

    args = parser.parse_args()

    validate_section_name(args.section)
    if args.cmd in ("add", "remove"):
        validate_pattern(args.pattern)
    if args.cmd in ("grant-new", "revoke-new") and args.section == UNIVERSAL:
        die("<new> capability is per-agent, not universal")

    invoker = invoker_username()
    data = load_allowlist()
    authorize(args.section, data, invoker)

    if args.cmd == "add":
        add_pattern(data, args.section, args.pattern)
    elif args.cmd == "remove":
        remove_pattern(data, args.section, args.pattern)
    elif args.cmd == "grant-new":
        grant_new(data, args.section)
    elif args.cmd == "revoke-new":
        revoke_new(data, args.section)

    write_atomic(emit(data))
    return 0


if __name__ == "__main__":
    sys.exit(main())
