# anki-manager

Lifecycle + domain manager for a headless Anki Desktop container. Layer 2 of the [Myrzka anki-skill stack](https://github.com/Great-Sarak/myrzka).

Wraps [`anki-rpc`](https://github.com/Great-Sarak/anki-rpc) (Layer 1) with:

- **Container lifecycle.** Talks to systemd (`kryshanti-anki.service`) directly on the host, or through the narrow Unix-socket lifecycle broker when explicitly configured; ensures the container is running and AnkiConnect is answering before any domain op.
- **Live schema validation.** Every `add_note` queries the model's field names from Anki and fails fast if the provided fields don't match — protects against silent corruption when the user renames a field in Anki Desktop.
- **CLI.** `anki-manager start / stop / status / list-models / add-deck / add-note / sync / force-upload / force-download`.

## Host prerequisites

The kryshanti-anki systemd unit must be installed (done once-off via the `host-setup.sh` script in [`ops/container/`](ops/container/README.md)). The invoking user must be in the `kryshanti-anki-users` group.

Inside the containerized OpenClaw gateway, set
`ANKI_MANAGER_LIFECYCLE_SOCKET` to the mounted broker socket. The gateway
deployment also forwards its loopback `127.0.0.1:8765` to the host-owned
AnkiConnect Unix socket, so the existing `host`/`port` defaults remain valid.
If the configured lifecycle socket is absent or malformed, lifecycle calls
fail closed with `LifecycleError`; they do not fall back to `systemctl`.

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

### Batch add-notes

For bulk import, use `add-notes` with a JSONL or queue-style-markdown input:

```sh
# JSONL: one note per line, fields as a JSON object
anki-manager add-notes --from-file notes.jsonl

# Markdown: anki-translator's queue file format (## Card N — <shape> blocks)
anki-manager add-notes --from-markdown queue.md

# Or stream JSONL on stdin
cat notes.jsonl | anki-manager add-notes --from-stdin

# Strict-add mode: fails if any stable_guid collides (default is idempotent upsert)
anki-manager add-notes --from-file notes.jsonl --add-only

# Validate without writing
anki-manager add-notes --from-file notes.jsonl --dry-run
```

JSONL line schema:

```json
{"deck": "Reading", "model": "AT Basic",
 "fields": {"Front": "Q", "Back": "A", "Source": "...", "Position": "..."},
 "tags": ["biology"], "stable_guid": null}
```

Behavior:

- **All-or-nothing validation**: every entry is dry-run through the same code paths as one-at-a-time writes (allowlist, schema, GUID derivation). If any entry fails validation, zero entries are written. Errors are reported per-index.
- Default mode is **upsert** (idempotent — re-running on the same file is safe).
- `--add-only` strictly adds; any stable-GUID collision fails the entry.
- `--dry-run` runs both phases (validate + would-write) without modifying the collection.

Output JSON:

```json
{"created": 5, "updated": 2, "skipped": 0,
 "failed": [{"index": 3, "error": "DeckNotAllowedError: ..."}],
 "dry_run": false}
```

Exit code is `0` on full success, `1` if any entry failed.

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

### Profile vs. agent identity

The kryshanti-anki container loads exactly one Anki **profile** at a
time (selected via `KRYSHANTI_ANKI_DEFAULT_PROFILE`; see
[`ops/container/README.md`](ops/container/README.md)). The allowlist's
**agent identity** is a separate axis. Don't confuse them:

| Axis | What it picks | Driven by | Where it's checked |
|---|---|---|---|
| **Profile** | Which `collection.anki2` is loaded — *whose collection* AnkiConnect serves. | `KRYSHANTI_ANKI_DEFAULT_PROFILE` env var at container start. | Anki Desktop's `-p` flag. |
| **Agent identity** | Which `[Section]` of `allowlist.toml` resolves — *what an agent is permitted to write*. | The invoking process's Linux UID, matched against `aliases` lists. | `Allowlist.resolve_agent()`. |

A literal coincidence: the user `sorotassu` is both a Linux username
(listed as an alias under `[Myrzka]` in the example) and the name of
the Anki profile that holds their real collection. These are
independent. The allowlist doesn't know or care which profile is loaded
— a Khezzura process calling into AnkiConnect against the `sorotassu`
profile still resolves to the `[Khezzura]` agent section (or no section,
if Khezzura's aliases don't claim that Linux user), not Myrzka's.

This separation is intentional. Profiles partition *whose collection*;
the allowlist partitions *what an agent can do inside that collection*.
Both mechanisms must fail closed independently for the safety property
to hold. Pinned by `TestProfileVsAgentIdentity` in
`tests/test_allowlist.py`.

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

`anki-manager` is also usable as a Python library — the CLI is a thin dispatch wrapper over the same `AnkiManager` class, so library callers get the same verification (allowlist enforcement, live schema check, stable-GUID handling, backup-on-write, writer lock).

### Quick start

```python
from anki_manager import AnkiManager

mgr = AnkiManager()
mgr.ensure_running()

models = mgr.list_models()      # {model_name: [field_names]} — live, never cached

add = mgr.add_note(
    deck="Myrzka::Daily", model="Myrzka Basic",
    fields={"Front": "Q", "Back": "A", "Source": "...", "Tags": ""},
)
# → AddResult(note_id=..., stable_guid="anki-manager::...", dry_run=False)

upsert = mgr.upsert_note(
    deck="Myrzka::Daily", model="Myrzka Basic",
    fields={"Front": "Q", "Back": "A revised answer", "Source": "...", "Tags": ""},
)
# → UpsertResult(note_id=..., stable_guid="...", created=False, dry_run=False)

mgr.sync()
```

### Public surface

Everything in `anki_manager.__all__` is supported and follows semver. Anything not in `__all__` is internal — may change without notice.

| Type | Purpose |
|---|---|
| `AnkiManager` | the top-level facade — instantiate once per process |
| `AddResult`, `UpsertResult` | typed return values for `add_note` / `upsert_note` |
| `Config` | optional construction-time tuning (host, port, lifecycle socket/timeouts, lock path, auto_backup) |
| `Status` | snapshot returned by `mgr.status()` |
| `Lifecycle`, `Allowlist`, `AgentEntry` | injectable for testing |
| `AnkiManagerError`, `InvalidNoteError`, `NoteExistsError`, `NoteNotFoundError`, `DeckNotAllowedError`, `AllowlistError`, `LifecycleError`, `NotReadyError`, `PermissionsHelperError`, `LockTimeoutError` | the exception hierarchy — all derive from `AnkiManagerError` (except `LockTimeoutError`, which is unrelated to domain errors) |
| `compute_guid`, `GUID_NAMESPACE`, `GUID_TAG_PREFIX`, `DRY_RUN_NOTE_ID` | constants and helpers for callers that need to derive GUIDs themselves |
| `file_lock`, `NEW_SENTINEL` | low-level helpers |

### Stability contract

- **Public** (`__all__`): the methods and types above. Removed or signature-changed methods will go through a deprecation cycle of at least one minor release.
- **Internal** (anything else, including private attrs and modules): may change at any time. If you need something not in `__all__`, file an issue requesting it be promoted.

### Verification parity with the CLI

Every check the CLI runs lives in the `AnkiManager` class itself, not in CLI dispatch:

| Check | Where |
|---|---|
| Allowlist enforcement | `AnkiManager._require_allowed()` invoked by `add_deck`, `add_note`, `upsert_note` |
| Live schema validation | `AnkiManager._validate_fields()` invoked by `add_note`, `upsert_note` |
| Stable GUID derivation + collision check | `compute_guid()` + `find_by_guid()` invoked by all three write paths |
| Auto-backup before first write | `_auto_backup_if_needed()` invoked by all three write paths |
| Cross-process writer lock | `_writer_lock()` held across lookup+write window |
| Lifecycle readiness gate | `ensure_running()` callable; AnkiConnect calls fail clearly if the unit isn't up |

So `mgr.add_note(...)` and `anki-manager add-note ...` produce identical pre-write state. Library callers can rely on this.

### Escape hatch

For AnkiConnect actions not in the typed surface (e.g. `changeDeck`, `gui*`, model creation):

```python
result = mgr.call("createModel", modelName="...", inOrderFields=[...], cardTemplates=[...], isCloze=False)
```

`call(action, **params)` is a generic passthrough to anki-rpc. No allowlist enforcement, no schema validation — caller's responsibility.

## Architecture

```
┌─────────────────────────────────────────────┐
│  AnkiManager        (domain + lifecycle)    │
│    Lifecycle  ─────► systemctl or broker    │
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

## Concurrency (writer lock)

All write paths (`add_note`, `update_note`, `upsert_note`) hold a
cross-process `flock` on `/var/lib/kryshanti-anki/writer.lock` while
running their GUID-collision lookup + RPC write. This closes the
TOCTOU race where two concurrent agents could both run `find_by_guid`,
both see "no existing note", and both call `addNote` with the same
stable GUID — producing two notes with identical identifiers.

The lock is held only across the lookup + write window, **not** across
`sync()` (which can take minutes and isn't write-critical).

`config.lock_timeout` defaults to 30s; `LockTimeoutError` is raised if
the lock can't be acquired in that window. Pass `Config(lock_path=None)`
to disable locking entirely (single-process tests only).

## Backup

A backup of the collection is automatically created once per AnkiManager
instance, just before the first mutating write (`add_note` /
`update_note` / `upsert_note`). The backup uses Anki's own
`create_backup_now()` machinery — atomic snapshot to a `.colpkg` in the
profile's `backups/` folder. Retention is governed by Anki's own
preferences (Tools → Preferences → Backups inside the container), not
by this package.

Manual trigger:

```sh
anki-manager create-backup
```

Disable per-session via `Config(auto_backup=False)`. Dry-run writes
also skip the backup (nothing's being changed).

The `createBackup` action is not in upstream AnkiConnect — it lives in
the spike's local patch `patches/0002-add-createBackup.patch`, separate
from the force-sync patch so it can be PR'd to upstream independently.

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

This package implements lifecycle + golden-path domain ops + stable-GUID update/upsert + deck allowlist + dry-run + writer lock + backup-on-write. All originally-planned Layer 2 features are in.

Retention is currently delegated to Anki's own backup preferences (Tools → Preferences → Backups in the container). If you want stricter rolling retention than Anki's defaults, configure those prefs; we can add agent-side pruning later if needed.
