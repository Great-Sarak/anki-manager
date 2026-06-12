from __future__ import annotations

import json
from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent
SKILL = ROOT / "skills" / "anki-manager" / "SKILL.md"


def _frontmatter() -> dict[str, str]:
    text = SKILL.read_text()
    assert text.startswith("---\n")
    frontmatter = text.split("---", 2)[1]
    parsed: dict[str, str] = {}
    for line in frontmatter.splitlines():
        if not line.strip():
            continue
        key, sep, value = line.partition(":")
        assert sep, line
        parsed[key.strip()] = value.strip()
    return parsed


def test_skill_frontmatter_declares_name_description_and_binary() -> None:
    parsed = _frontmatter()
    assert parsed["name"] == "anki-manager"
    assert parsed["description"].startswith('"Manage the host')

    metadata = json.loads(parsed["metadata"])
    assert metadata["openclaw"]["requires"]["bins"] == ["anki-manager"]


def test_skill_mentions_live_model_check_and_sync() -> None:
    text = SKILL.read_text()
    assert "Always inspect live models before composing a note" in text
    assert "anki-manager list-models" in text
    assert "anki-manager sync" in text
