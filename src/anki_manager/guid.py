"""Stable, content-derived identifiers for agent-managed Anki notes.

A "stable GUID" here is a deterministic tag of the form

    anki-manager::<sha256(source || NUL || front)[:16]>

stored on the note as a tag.  Two notes with the same source and front
text produce the same GUID; an agent can look up or update a note by
re-computing the GUID rather than tracking Anki's internal note IDs.

This is not Anki's built-in `note.guid` field (which can't be set via
AnkiConnect today).  Using a tag-namespaced identifier keeps the scheme
patch-free.

The GUID changes when source or front changes — that's by design.  A
meaningful content change is a new note, not an amendment.  Use
`update_note(guid, fields=...)` directly to amend without producing a
new GUID.
"""

from __future__ import annotations

import hashlib

GUID_NAMESPACE = "anki-manager"
GUID_TAG_PREFIX = f"{GUID_NAMESPACE}::"
GUID_LENGTH = 16  # hex chars from sha256, 64 bits — collision-safe at our scale


def compute_guid(source: str, front: str) -> str:
    """Return the full GUID tag (`anki-manager::<16hex>`)."""
    digest = hashlib.sha256(
        source.encode("utf-8") + b"\x00" + front.encode("utf-8"),
    ).hexdigest()
    return f"{GUID_TAG_PREFIX}{digest[:GUID_LENGTH]}"


def derive_source(fields: dict[str, str]) -> str:
    """Extract the source/provenance value for GUID derivation.

    Looks for a field named (case-insensitive) `source`.  Returns the
    empty string if no such field exists — the GUID is still
    deterministic for the given (source, front) tuple.
    """
    for name, value in fields.items():
        if name.lower() == "source":
            return value
    return ""


def derive_front(fields: dict[str, str], model_field_order: list[str]) -> str:
    """Extract the front-or-text value for GUID derivation.

    Heuristic order:
      1. A field named (case-insensitive) `front`
      2. A field named (case-insensitive) `text`  (cloze convention)
      3. The first field in the model's declared order

    Raises ValueError if no suitable field can be found.
    """
    lower_lookup = {name.lower(): name for name in fields}
    for preferred in ("front", "text"):
        if preferred in lower_lookup:
            return fields[lower_lookup[preferred]]
    if model_field_order:
        first = model_field_order[0]
        if first in fields:
            return fields[first]
    raise ValueError(
        "Cannot derive front-or-text for GUID: no 'Front' or 'Text' field "
        "and no model_field_order provided. Pass stable_guid explicitly."
    )
