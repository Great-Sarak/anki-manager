---
name: anki-manager
description: "Manage the host's Anki Desktop collection through the kryshanti-anki container: lifecycle, models, decks, notes, permissions, and sync."
metadata: {"openclaw":{"emoji":"🃏","requires":{"bins":["anki-manager"]}}}
---

# anki-manager

Use for operations against the host's Anki collection through the
`kryshanti-anki.service` headless Anki container:
- start, stop, restart, or inspect the container;
- list live Anki note types and fields;
- add, update, upsert, or batch-import already-shaped notes;
- inspect or change deck allowlist permissions;
- sync with AnkiWeb.

For turning documents into card candidates, use the Layer 3 `anki-translator`
skill. `anki-manager` takes pre-formed `{field: value}` payloads and writes
them safely.

## Runtime Requirements

- `anki-manager` is on `PATH`.
- The invoking identity can start `kryshanti-anki.service` through the installed
  polkit rule, normally by membership in `kryshanti-anki-users`.
- Writes are allowed by `/var/lib/kryshanti-anki/allowlist.toml`; missing or
  unmatched allowlist entries fail closed.
- The currently active Anki profile is the collection being mutated. Profile
  selection is separate from agent deck permissions.

## CLI Surface

```sh
anki-manager start
anki-manager stop
anki-manager restart
anki-manager status
anki-manager sync
anki-manager force-upload
anki-manager force-download
anki-manager create-backup
anki-manager list-models
```

Write commands:

```sh
anki-manager add-deck "Myrzka::Daily"

anki-manager add-note \
  --deck "Myrzka::Daily" \
  --model "Myrzka Basic" \
  --field "Front=Q" \
  --field "Back=A" \
  --field "Source=..." \
  --field "Tags=" \
  --tag mytag

anki-manager update-note \
  --stable-guid "anki-manager::<hash>" \
  --field "Back=revised"

anki-manager upsert-note \
  --deck "Myrzka::Daily" \
  --model "Myrzka Basic" \
  --field "Front=Q" \
  --field "Back=A" \
  --field "Source=..." \
  --field "Tags="

anki-manager find-by-guid "anki-manager::<hash>"
```

Batch import:

```sh
anki-manager add-notes --from-file notes.jsonl
anki-manager add-notes --from-markdown queue.md
cat notes.jsonl | anki-manager add-notes --from-stdin
anki-manager add-notes --from-file notes.jsonl --dry-run
anki-manager add-notes --from-file notes.jsonl --add-only
```

Permissions:

```sh
anki-manager permissions show
anki-manager permissions add --pattern "Myrzka::NewDeck"
anki-manager permissions remove --pattern "Old::Deck"
anki-manager permissions grant-new
anki-manager permissions revoke-new
```

Use `--agent NAME` or `--universal` on permission mutations only when the user
has explicitly asked for that target.

## Standard Add Workflow

Always inspect live models before composing a note. Field names are user-editable
inside Anki Desktop.

```sh
anki-manager start
anki-manager list-models
anki-manager add-note \
  --deck "Myrzka::Daily" \
  --model "Myrzka Basic" \
  --field "Front=When did the hardening project start?" \
  --field "Back=2026-05-12, with the org migration." \
  --field "Source=memory/hardening_project.md" \
  --field "Tags=" \
  --tag fleet-history
anki-manager sync
```

If the same card may be imported repeatedly, prefer `upsert-note` or
`add-notes` default mode. Re-imports with the same stable GUID update instead
of duplicating.

## Stable GUIDs

Agent-created notes get an `anki-manager::<sha256>` tag. By default the GUID is
derived from `Source` plus `Front`, or `Source` plus `Text` for cloze-style
models. Pass `--stable-guid` explicitly for models without those fields.

Changing source or front/text intentionally creates a new GUID. To amend an
existing card, use `update-note --stable-guid`.

## Sync Conflict Handling

Use normal `anki-manager sync` after agent writes. If sync reports an
unresolvable full-sync condition, stop and ask which side is canonical:
- `force-upload` pushes local over AnkiWeb.
- `force-download` pulls AnkiWeb over local and takes a backup first.

Do not choose a force direction without explicit user instruction.

## Python API

For multi-step scripts, import `AnkiManager` and use the same methods the CLI
dispatches to:

```python
from anki_manager import AnkiManager

mgr = AnkiManager()
mgr.ensure_running()
models = mgr.list_models()
result = mgr.upsert_note(
    deck="Myrzka::Daily",
    model="Myrzka Basic",
    fields={"Front": "Q", "Back": "A", "Source": "...", "Tags": ""},
)
mgr.sync()
```

Everything exported from `anki_manager.__all__` is the supported Python surface.
Private modules and attributes are internal.
