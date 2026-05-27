# anki-manager

Lifecycle + domain manager for a headless Anki Desktop container. Layer 2 of the [Myrzka anki-skill stack](https://github.com/Great-Sarak/myrzka).

Wraps [`anki-rpc`](https://github.com/Great-Sarak/anki-rpc) (Layer 1) with:

- **Container lifecycle.** Talks to systemd (`kryshanti-anki.service`) via `systemctl`; ensures the container is running and AnkiConnect is answering before any domain op.
- **Live schema validation.** Every `add_note` queries the model's field names from Anki and fails fast if the provided fields don't match — protects against silent corruption when the user renames a field in Anki Desktop.
- **CLI.** `anki-manager start / stop / status / list-models / add-deck / add-note / sync / force-upload / force-download`.

## Host prerequisites

The kryshanti-anki systemd unit must be installed (done once-off via the `host-setup.sh` script in the [myrzka workspace](https://github.com/Great-Sarak/myrzka)'s `spikes/anki-docker/`). The invoking user must be in the `kryshanti-anki-users` group.

## Install

```sh
python3 -m venv .venv
.venv/bin/pip install -e ../anki-rpc_main   # sibling repo
.venv/bin/pip install -e .
```

## CLI

```sh
anki-manager start         # ensure unit running + AnkiConnect ready
anki-manager status        # JSON: active / ready / sub_state
anki-manager list-models   # JSON: {model: [fields, ...]}
anki-manager add-deck "Myrzka::Daily"
anki-manager add-note \
    --deck "Myrzka::Daily" \
    --model "Myrzka Basic" \
    --field "Front=Q" \
    --field "Back=A" \
    --field "Source=..." \
    --field "Tags=" \
    --tag mytag
# → {"note_id": ..., "stable_guid": "anki-manager::<hash>"}

anki-manager update-note --stable-guid "anki-manager::<hash>" --field "Back=new"
anki-manager upsert-note --deck "..." --model "..." --field "Front=..." --field "..."
# → {"note_id": ..., "stable_guid": "...", "created": true|false}

anki-manager find-by-guid "anki-manager::<hash>"   # → note_id or null
anki-manager sync
```

## Permissions (deck allowlist)

Writes are gated by a system-wide TOML allowlist at
`/var/lib/kryshanti-anki/allowlist.toml`, installed (with a sensible
starter) by `host-setup.sh`. Each agent has a section claiming a set
of Linux usernames as aliases and listing patterns it may write to:

```toml
universal = []  # patterns every agent gets, regardless of identity

[Myrzka]
allowed = ["Myrzka::*", "Myrzka", "<new>"]
aliases = ["sorotassu"]

[Tava]
allowed = ["Tava::*"]
aliases = ["tava-user"]
```

Patterns use `fnmatch` semantics — `*` matches any sequence including
`::`. The literal `<new>` is a capability flag, not a pattern: when
present, the agent may call `add-deck` for a deck name not yet matched
and the new name is appended to its section by the privileged
`grant-deck` helper (invoked via pkexec, group-gated by polkit).

```sh
anki-manager permissions show
anki-manager permissions add --pattern "Myrzka::NewDeck"       # invokes helper via pkexec
anki-manager permissions remove --pattern "Old::Deck"
anki-manager permissions grant-new                              # add <new> to invoker's agent
anki-manager permissions revoke-new
```

Without `<new>`, attempting to write to a deck that isn't already in
the agent's effective allowlist raises `DeckNotAllowedError` — fail
closed; a missing allowlist file raises `AllowlistError`.

## Stable GUIDs

Every agent-added note is tagged with a deterministic identifier of the form
`anki-manager::<sha256(source + NUL + front)[:16]>`. The tag is the lookup
key for `update-note`, `find-by-guid`, and `upsert-note` — letting the agent
amend or re-detect a note later without tracking Anki's internal note IDs.

The GUID is derived automatically from the `Source` and `Front` (or `Text`,
for cloze models) field values in the supplied `--field` payload. Pass
`--stable-guid anki-manager::<hex>` explicitly when the heuristic doesn't
apply (e.g. a non-Myrzka model with unusual field naming).

A content change to either the source or front fields produces a **new**
GUID — by design, that's a new note, not an amendment. To amend an existing
note, use `update-note --stable-guid ...` with the original GUID.

## Python API

```python
from anki_manager import AnkiManager

mgr = AnkiManager()
mgr.ensure_running()

models = mgr.list_models()      # live; never cached
note_id = mgr.add_note(
    "Myrzka::Daily", "Myrzka Basic",
    fields={"Front": "Q", "Back": "A", "Source": "...", "Tags": ""},
)
mgr.sync()
```

## Architecture

```
┌─────────────────────────────────────────────┐
│  AnkiManager        (domain + lifecycle)    │
│    Lifecycle  ─────► systemctl              │
│    Client     ─────► AnkiConnect HTTP       │
└─────────────────────────────────────────────┘
         │
         ▼
┌─────────────────────────────────────────────┐
│  anki-rpc          (typed HTTP client)      │
└─────────────────────────────────────────────┘
         │
         ▼
┌─────────────────────────────────────────────┐
│  kryshanti-anki.service  (systemd unit)     │
│    docker run kryshanti-anki:25.02.7        │
│      └─ Anki Desktop + AnkiConnect          │
└─────────────────────────────────────────────┘
```

## Testing

```sh
pytest tests/test_lifecycle.py tests/test_manager.py    # unit (27 tests, no host needed)
ANKI_MANAGER_INTEGRATION=1 pytest tests/test_integration.py    # 5 tests, requires live unit
```

## Dry-run

All three write paths support a `dry_run=True` kwarg (`--dry-run` on the CLI). When set, the full validation pipeline runs — allowlist check, schema check, GUID-collision lookup — but no RPC write is sent.

```sh
anki-manager add-note --deck "Myrzka::Daily" --model "Myrzka Basic" \
    --field "Front=..." --field "Back=..." --field "Source=..." --field "Tags=" \
    --dry-run
# → {"note_id": 0, "stable_guid": "anki-manager::...", "dry_run": true}
```

For `add-note`, `note_id` is `0` (sentinel — real Anki note IDs are positive). For `upsert-note`, `created` reflects what would have happened. For `update-note`, the lookup runs and the real note_id is returned (so you can confirm the target before applying the change).

## Deferred to a follow-up

This package implements lifecycle + golden-path domain ops + stable-GUID update/upsert + deck allowlist + dry-run. The original plan also calls for:

- Single-writer lock (preventing two concurrent agents from writing to the same collection)
- Backup-on-write with 3-day rolling retention

These will land in subsequent commits.
