# anki-tools install (`/opt`, agent-independent)

`install-tools.sh` puts anki-rpc, anki-manager, and anki-translator under
`/opt/anki-tools/` — out of any agent's hut — so the tools outlive a specific
Beastfolk and keep working if a hut is wiped or relocated (anki-manager #32).

## Layout it produces

```
/opt/anki-tools/
├── anki-rpc/        + .venv         # Layer-1 dependency
├── anki-manager/    + .venv         # built on anki-rpc
└── anki-translator/ + .venv         # built on anki-rpc + anki-manager

/usr/local/bin/anki-manager      -> /opt/anki-tools/anki-manager/.venv/bin/anki-manager
/usr/local/bin/anki-translator   -> wrapper → /opt/anki-tools/anki-translator/.venv/bin/anki-translator
                                    (exports ANKI_TRANSLATOR_STATE_DIR, overridable)

/var/lib/anki-translator/{queue,qa,trimmed,committed}   # translator state (group-owned)
/var/lib/kryshanti-anki/                                # container state — UNCHANGED
```

## Run order

1. `sudo ops/container/host-setup.sh` — kryshanti-anki container, the
   `kryshanti-anki-users` group, container state. (Prerequisite: this script
   chowns the translator state dir to that group.)
2. `sudo ops/install/install-tools.sh [user]` — the `/opt` tools tree + venvs +
   CLI entrypoints + `/var/lib/anki-translator/`.

Both are idempotent.

## Pinning

Defaults to the **`v0.1`** tag for all three repos. Override:

```sh
sudo ANKI_TOOLS_REF=v0.2 ops/install/install-tools.sh
```

## State

anki-translator writes to `$ANKI_TRANSLATOR_STATE_DIR` (the `/usr/local/bin`
wrapper defaults it to `/var/lib/anki-translator/`; an explicit env wins). Unset,
the tool falls back to cwd-relative dirs — the in-hut dev behavior. The
`--queue-dir`/`--qa-dir`/`--trimmed-dir` flags override either way.

## Migration (manual — do once, after the smoke passes)

The installer does **not** move state or remove the old installs. Per #32:

1. Run the installer; confirm `anki-manager --help` and `anki-translator --help`
   resolve to `/opt`.
2. Run the v0.1 smoke (ops/container/README, #17 runbook) against the new install.
3. Move existing `*_hut/*/anki-translator_main/{queue,qa}` into
   `/var/lib/anki-translator/` (then `chgrp -R kryshanti-anki-users` + `chmod g+rwXs`).
4. Delete the hut `.venv`s once parity holds. The hut clones stay as dev working dirs.
