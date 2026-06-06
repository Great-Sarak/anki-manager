from __future__ import annotations

from pathlib import Path

import pytest

from anki_manager.allowlist import (
    AgentEntry,
    Allowlist,
    AllowlistError,
    DEFAULT_ALLOWLIST_PATH,
    NEW_SENTINEL,
)


def _write(tmp_path: Path, content: str) -> Path:
    p = tmp_path / "allowlist.toml"
    p.write_text(content)
    return p


class TestLoad:
    def test_simple_file(self, tmp_path):
        p = _write(tmp_path, """
universal = ["Daily::Reading"]

[Myrzka]
allowed = ["Myrzka::*", "Myrzka", "<new>"]
aliases = ["sorotassu"]
""")
        al = Allowlist.load(p)
        assert al.universal == ("Daily::Reading",)
        assert set(al.agents.keys()) == {"Myrzka"}
        agent = al.agents["Myrzka"]
        assert agent.patterns == ("Myrzka::*", "Myrzka")
        assert agent.has_new is True
        assert agent.aliases == ("sorotassu",)

    def test_empty_universal(self, tmp_path):
        p = _write(tmp_path, """
universal = []

[Tava]
allowed = ["Tava::*"]
aliases = []
""")
        al = Allowlist.load(p)
        assert al.universal == ()
        assert al.agents["Tava"].patterns == ("Tava::*",)
        assert al.agents["Tava"].has_new is False

    def test_missing_file_raises(self, tmp_path):
        with pytest.raises(AllowlistError, match="not found"):
            Allowlist.load(tmp_path / "absent.toml")

    def test_malformed_toml_raises(self, tmp_path):
        p = _write(tmp_path, "this is not toml [")
        with pytest.raises(AllowlistError, match="parse"):
            Allowlist.load(p)

    def test_non_list_universal_raises(self, tmp_path):
        p = _write(tmp_path, 'universal = "not a list"\n')
        with pytest.raises(AllowlistError, match="universal"):
            Allowlist.load(p)


class TestResolveAgent:
    def _al(self) -> Allowlist:
        return Allowlist(
            universal=(),
            agents={
                "Myrzka": AgentEntry("Myrzka", ("Myrzka::*",), ("sorotassu",), False),
                "Tava": AgentEntry("Tava", ("Tava::*",), ("tava-user",), False),
            },
        )

    def test_resolves_by_alias(self):
        assert self._al().resolve_agent("sorotassu").name == "Myrzka"

    def test_unknown_user_returns_none(self):
        assert self._al().resolve_agent("nobody") is None

    def test_ambiguous_alias_raises(self):
        al = Allowlist(
            universal=(),
            agents={
                "A": AgentEntry("A", (), ("shared",), False),
                "B": AgentEntry("B", (), ("shared",), False),
            },
        )
        with pytest.raises(AllowlistError, match="multiple"):
            al.resolve_agent("shared")


class TestMatches:
    @pytest.fixture
    def al(self) -> Allowlist:
        return Allowlist(
            universal=("Daily::Reading",),
            agents={
                "Myrzka": AgentEntry(
                    name="Myrzka",
                    patterns=("Myrzka::*", "Myrzka"),
                    aliases=("sorotassu",),
                    has_new=False,
                ),
            },
        )

    def test_universal_matches_for_any_agent(self, al):
        agent = al.agents["Myrzka"]
        assert al.matches("Daily::Reading", agent) is True
        assert al.matches("Daily::Reading", None) is True

    def test_agent_patterns_match(self, al):
        agent = al.agents["Myrzka"]
        assert al.matches("Myrzka::Spike", agent) is True
        assert al.matches("Myrzka::Spike::Sub", agent) is True
        assert al.matches("Myrzka", agent) is True

    def test_agent_patterns_dont_leak_to_other_agents(self, al):
        assert al.matches("Myrzka::Spike", None) is False

    def test_unmatched_deck(self, al):
        assert al.matches("OffLimits", al.agents["Myrzka"]) is False


class TestHasNewCapability:
    def test_with_new(self):
        agent = AgentEntry("X", (), (), True)
        al = Allowlist(universal=(), agents={"X": agent})
        assert al.has_new_capability(agent) is True

    def test_without_new(self):
        agent = AgentEntry("X", (), (), False)
        al = Allowlist(universal=(), agents={"X": agent})
        assert al.has_new_capability(agent) is False

    def test_no_agent(self):
        al = Allowlist(universal=(), agents={})
        assert al.has_new_capability(None) is False


class TestProfileVsAgentIdentity:
    """Pin: the allowlist resolver depends on Linux UID, not Anki profile.

    The literal Anki profile named "sorotassu" introduced in #8 shares its
    name with a Linux-user alias under [Myrzka]. These are independent:
    a Khezzura process can call into the container with the "sorotassu"
    profile loaded, and the allowlist must still resolve to [Khezzura]
    (or no section, if Khezzura's aliases don't claim that Linux user).

    See module docstring for the long explanation. These tests guard
    against future code that conflates the two.
    """

    @pytest.fixture
    def al(self, tmp_path):
        return Allowlist.load(_write(tmp_path, """
universal = []

[Myrzka]
allowed = ["Myrzka::*", "<new>"]
aliases = ["sorotassu"]

[Khezzura]
allowed = ["Khezzura::*", "<new>"]
aliases = ["khezzura-user"]
"""))

    def test_sorotassu_linux_user_resolves_to_myrzka(self, al):
        agent = al.resolve_agent(linux_user="sorotassu")
        assert agent is not None
        assert agent.name == "Myrzka"

    def test_khezzura_user_resolves_to_khezzura_even_with_sorotassu_alias_nearby(self, al):
        # The fact that "sorotassu" appears as an alias under [Myrzka]
        # doesn't taint Khezzura's resolution. Different Linux user,
        # different agent.
        agent = al.resolve_agent(linux_user="khezzura-user")
        assert agent is not None
        assert agent.name == "Khezzura"

    def test_khezzura_can_write_khezzura_decks_against_any_profile(self, al):
        # The profile axis is invisible to allowlist resolution; deck
        # matching only depends on the resolved agent's patterns.
        agent = al.resolve_agent(linux_user="khezzura-user")
        assert al.matches("Khezzura::Cards", agent) is True
        assert al.matches("Myrzka::Octopus", agent) is False

    def test_khezzura_cannot_write_myrzka_decks_via_sorotassu_alias(self, al):
        # Even though "sorotassu" is listed under [Myrzka], that doesn't
        # grant Khezzura any Myrzka access. Only the Linux UID matters.
        agent = al.resolve_agent(linux_user="khezzura-user")
        assert agent.name == "Khezzura"
        assert al.matches("Myrzka::Octopus", agent) is False
        assert al.matches("Myrzka", agent) is False

    def test_unknown_linux_user_gets_no_agent(self, al):
        # A Linux user not claimed by any aliases list (e.g. a profile
        # name that isn't also an alias) resolves to None — universal
        # patterns only.
        agent = al.resolve_agent(linux_user="some-unknown-user")
        assert agent is None

    def test_anki_profile_literal_not_an_alias_doesnt_match(self, al):
        # The point: even though we have a profile called "sorotassu"
        # at the Anki layer, that's invisible to allowlist resolution.
        # Only the explicit `aliases = ["sorotassu"]` entry matters,
        # and only as a Linux-user → agent map.
        # This is checked by: the resolver takes a linux_user string and
        # doesn't look at any profile-related state. We verify by
        # observation — no profile parameter exists on resolve_agent.
        import inspect
        sig = inspect.signature(al.resolve_agent)
        assert "profile" not in sig.parameters
        assert "linux_user" in sig.parameters
