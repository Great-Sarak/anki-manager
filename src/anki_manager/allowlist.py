"""Deck allowlist — parses /var/lib/kryshanti-anki/shared/allowlist.toml, resolves
agent identity, and decides whether a deck name is writable by an agent.

The file shape:

    universal = ["Daily::Reading"]

    [Myrzka]
    allowed = ["Myrzka::*", "Myrzka", "<new>"]
    aliases = ["sorotassu"]

    [Tava]
    allowed = ["Tava::*"]
    aliases = ["tava-user"]

Agent resolution: the invoking user's Linux name is matched against each
section's `aliases` list.  A match resolves the user to that agent
section; multiple matches are ambiguous and rejected.  No match means
the user has no agent identity and only `universal` patterns apply.

The alias mechanism is purely **agent-side** — it maps a Linux user to
an agent identity (which determines the deck patterns).  It has nothing
to do with which Anki profile is loaded in the container.  Profile is
a separate axis: it determines *whose collection* AnkiConnect serves
(see `KRYSHANTI_ANKI_DEFAULT_PROFILE`), while the allowlist determines
*what an agent is permitted to write to* inside that collection.

Concretely: if Linux user `sorotassu` is listed as an alias under
`[Myrzka]`, that grants the Myrzka rules to processes running as the
`sorotassu` UID — regardless of whether the container is currently
loading a profile called `sorotassu`, `_anki_skill_testrun`, or
anything else.  A *different* Linux user (e.g. `khezzura-user`) calling
into AnkiConnect against the `sorotassu` profile resolves to the
`[Khezzura]` agent section (or no section at all if Khezzura's
aliases don't list them), not Myrzka's.

The literal sentinel `<new>` is not a pattern — it's a capability flag
indicating the agent may extend its own allowed list (via the
`grant-deck` helper) when creating a new deck.  Without `<new>`, the
agent can only operate on decks matching its current patterns.

Patterns use Python's `fnmatch` semantics — `*` matches any sequence of
characters (including `::`), `?` matches a single character.  So
`Myrzka::*` covers every sub-deck under Myrzka at any depth, but does
not match the bare `Myrzka` parent (separate pattern needed).
"""

from __future__ import annotations

import fnmatch
import os
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

from .errors import AnkiManagerError

NEW_SENTINEL = "<new>"
DEFAULT_ALLOWLIST_PATH = Path("/var/lib/kryshanti-anki/shared/allowlist.toml")


class AllowlistError(AnkiManagerError):
    """Raised when the allowlist file is missing, malformed, or ambiguous."""


class DeckNotAllowedError(AnkiManagerError):
    """Raised when an operation targets a deck the invoking agent isn't allowed to write to."""


@dataclass(frozen=True)
class AgentEntry:
    name: str
    patterns: tuple[str, ...]
    aliases: tuple[str, ...]
    has_new: bool


@dataclass(frozen=True)
class Allowlist:
    universal: tuple[str, ...]
    agents: dict[str, AgentEntry] = field(default_factory=dict)
    path: Path | None = None

    @classmethod
    def load(cls, path: Path | None = None) -> "Allowlist":
        path = path or DEFAULT_ALLOWLIST_PATH
        if not path.exists():
            raise AllowlistError(
                f"Allowlist not found at {path}. "
                f"Run host-setup.sh to install a starter, or create the file manually."
            )

        try:
            data = tomllib.loads(path.read_text(encoding="utf-8"))
        except tomllib.TOMLDecodeError as exc:
            raise AllowlistError(f"Failed to parse {path}: {exc}") from exc

        universal_raw = data.pop("universal", [])
        if not isinstance(universal_raw, list):
            raise AllowlistError(f"'universal' must be a list in {path}")
        universal = tuple(str(p) for p in universal_raw)

        agents: dict[str, AgentEntry] = {}
        for key, section in data.items():
            if not isinstance(section, dict):
                raise AllowlistError(
                    f"Section [{key}] in {path} must be a table, not {type(section).__name__}"
                )
            allowed_raw = section.get("allowed", [])
            aliases_raw = section.get("aliases", [])
            if not isinstance(allowed_raw, list) or not isinstance(aliases_raw, list):
                raise AllowlistError(
                    f"Section [{key}] in {path}: 'allowed' and 'aliases' must be lists"
                )
            patterns: list[str] = []
            has_new = False
            for entry in allowed_raw:
                if entry == NEW_SENTINEL:
                    has_new = True
                else:
                    patterns.append(str(entry))
            agents[key] = AgentEntry(
                name=key,
                patterns=tuple(patterns),
                aliases=tuple(str(a) for a in aliases_raw),
                has_new=has_new,
            )

        return cls(universal=universal, agents=agents, path=path)

    # ------------------------------------------------------------------ #
    # Agent resolution                                                    #
    # ------------------------------------------------------------------ #

    def resolve_agent(self, linux_user: str | None = None) -> AgentEntry | None:
        """Return the AgentEntry whose aliases contain `linux_user`.

        Defaults to the calling process's effective username.  Returns
        None if no agent claims this user.  Raises AllowlistError on
        ambiguity (multiple agents claim the same alias).
        """
        if linux_user is None:
            linux_user = _current_username()

        matches = [a for a in self.agents.values() if linux_user in a.aliases]
        if len(matches) > 1:
            names = sorted(a.name for a in matches)
            raise AllowlistError(
                f"User {linux_user!r} matches multiple agent sections: {names}. "
                f"Remove the duplicate alias."
            )
        return matches[0] if matches else None

    # ------------------------------------------------------------------ #
    # Matching                                                            #
    # ------------------------------------------------------------------ #

    def effective_patterns(self, agent: AgentEntry | None) -> tuple[str, ...]:
        if agent is None:
            return self.universal
        return self.universal + agent.patterns

    def matches(self, deck: str, agent: AgentEntry | None) -> bool:
        for pattern in self.effective_patterns(agent):
            if fnmatch.fnmatchcase(deck, pattern):
                return True
        return False

    def has_new_capability(self, agent: AgentEntry | None) -> bool:
        return agent is not None and agent.has_new


def _current_username() -> str:
    # Prefer login name (resolves "real" user even when running under sudo
    # without -E); fall back to effective uid lookup.
    try:
        return os.getlogin()
    except OSError:
        import pwd
        return pwd.getpwuid(os.geteuid()).pw_name
