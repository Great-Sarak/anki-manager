# kryshanti-anki container

Container that runs Anki Desktop 25.02.7 headless under Xvfb with the
AnkiConnect addon listening on port 8765. This is `anki-manager`'s
deployment target — `anki-manager` calls `127.0.0.1:8765` against a
running instance of this image.

The container source lived in the Myrzka agent workspace at
`spikes/anki-docker/` through Phase 1; adopted here as the canonical
home in #10.

## Production-bound install (managed by systemd)

Once-off host setup:

```sh
sudo ./host-setup.sh
```

This creates the `liora-mosspelt` system user, the
`kryshanti-anki-users` access group, `/var/lib/kryshanti-anki/`, the
image `kryshanti-anki:25.02.7`, the `kryshanti-anki.service` systemd
unit, and a polkit rule so members of `kryshanti-anki-users` can
start/stop the unit without a password. See the script header for the
full list. Migrates any pre-existing `data/` and `.env` adjacent to this
file into `/var/lib/kryshanti-anki/`. Idempotent.

Day-to-day after that:

```sh
systemctl start  kryshanti-anki     # no sudo needed
systemctl stop   kryshanti-anki
systemctl status kryshanti-anki
```

This is what `anki-manager` drives.

## Profiles

The container loads one Anki profile per launch. The profile is selected
by the `KRYSHANTI_ANKI_DEFAULT_PROFILE` env var (forwarded via `--env-file`
from `/var/lib/kryshanti-anki/anki.env`); default is `_anki_skill_testrun`.

Profile directories live under the bind-mounted data dir as
`/var/lib/kryshanti-anki/data/<profile>/`. Switching profiles requires a
container restart with a different env var; only one profile is live per
container at a time.

| Profile | Purpose | AnkiWeb account |
|---|---|---|
| `_anki_skill_testrun` | Integration tests (renamed from `User 1`) | Throwaway test account |
| `sorotassu` | User's real collection (added in #8) | User's primary account |

AnkiWeb credentials are per-profile, read by the SeedLogin addon from
env vars named `ANKIWEB_USERNAME_<profile>` and
`ANKIWEB_PASSWORD_<profile>` (note the underscore before the profile
name when the profile itself starts with underscore — e.g.
`ANKIWEB_USERNAME__anki_skill_testrun` for the test profile). The
unscoped legacy names `ANKIWEB_USERNAME` / `ANKIWEB_PASSWORD` are
honored as a fallback for one release cycle.

### Bootstrap a new profile from a `.colpkg`

```sh
sudo ./bootstrap-profile.sh sorotassu \
    --import /path/to/all_decks.colpkg \
    --ankiweb-user sorotassu@example.com
```

Drives the new profile through AnkiConnect's `importPackage` action
(driving Anki's real import path — not unzipping the `.colpkg` into the
profile dir, which doesn't work because `.colpkg` stores media as
numbered blobs that need translation through a JSON map). Flips
`KRYSHANTI_ANKI_DEFAULT_PROFILE` in `anki.env`, restarts the unit, runs
the import, verifies decks loaded, and (optionally) writes per-profile
AnkiWeb credentials. The `.colpkg` is supplied at runtime; no sample
collection lives in the repo.

For non-interactive credential setup, point at a `chmod 600` password
file:

```sh
sudo ./bootstrap-profile.sh sorotassu \
    --import ./all_decks.colpkg \
    --ankiweb-user sorotassu@example.com \
    --ankiweb-pass-file ~/secrets/ankiweb.pass
```

First sync after bootstrap will be a FULL_DOWNLOAD reconciliation
against AnkiWeb. See `--help` for `--force`, `--skip-credentials`, and
other flags.

## Manual run (development of the AnkiConnect patch)

For iterating on the AnkiConnect patch in `patches/` without the
systemd-managed unit in the way:

```sh
mkdir -p data
docker build -t myrzka/anki-spike:25.02.7 .
docker run -d --name anki-spike \
  --env-file .env \
  -p 127.0.0.1:8765:8765 \
  -v "$PWD/data":/data \
  myrzka/anki-spike:25.02.7
```

Stop the systemd-managed unit first if it's running
(`systemctl stop kryshanti-anki`) — port 8765 only allows one.

`--env-file .env` passes AnkiWeb test-account credentials
(`ANKIWEB_USERNAME` / `ANKIWEB_PASSWORD`) into the container so the
`SeedLogin` addon (see below) can exchange them for an `hkey` and
persist auth to `prefs21.db`. `.env` is gitignored. Without it the
container still runs; the addon just no-ops.

First-run dialogs (language picker, language confirmation, profile
picker) currently require manual click-through via `xdotool` from inside
the container. See the project plan's findings addendum.

## Probe

```sh
curl -s -X POST http://127.0.0.1:8765 \
  -d '{"action":"version","version":6}'
# → {"result": 6, "error": null}
```

## What's in here

- `Dockerfile` — Debian bookworm + Anki 25.02.7 binary + AnkiConnect
  from `git.sr.ht/~foosoft/anki-connect` (the maintained source; GitHub
  mirror is archived). `ANKICONNECT_REV` is pinned to a specific commit
  so rebuilds are reproducible — bump intentionally, not silently on
  rebuild.
- `patches/` — local patches applied to the AnkiConnect tarball at
  build time, before the addon is staged. Currently:
  - `0001-add-forceUpload-forceDownload.patch` — adds `forceUpload` /
    `forceDownload` actions to handle the `FULL_SYNC` (status 4) case
    that upstream `sync` rejects. Written in upstream style with README
    docs and upstream-style tests, so the same patch can be offered
    back to sourcehut as a PR.
  - `0002-add-createBackup.patch` — adds a `createBackup` action that
    invokes the same backup pipeline Anki uses on schedule, exposed
    over AnkiConnect so the writer-lock layer can take a backup before
    every write session.
- `grant-deck.py` — privileged allowlist editor for `anki-manager`,
  installed by `host-setup.sh` to
  `/usr/local/libexec/kryshanti-anki/grant-deck`. Runs as root;
  validates pattern syntax, invoker identity, and section authority
  before atomically rewriting `/var/lib/kryshanti-anki/allowlist.toml`.
  Tested via `test_grant_deck.py` (15 unit tests).
- `test_grant_deck.py` — unit tests for the helper, runnable in any
  Python 3.12 env with pytest installed.
- `entrypoint.sh` — starts Xvfb, stages addon into `/data/addons21/`,
  execs Anki as PID 1.
- `ankiconnect-config.json` — AnkiConnect addon config. Sets
  webBindPort 8765 and webBindAddress 0.0.0.0 (the container hides
  this behind a 127.0.0.1 host-port map).
- `data/` (gitignored, developer-only) — bind-mounted Anki base for
  the manual-run path above. Production uses
  `/var/lib/kryshanti-anki/data/` instead.
- `.env` (gitignored, developer-only) — AnkiWeb test-account
  credentials for the SeedLogin addon during local development.
  Production reads from `/var/lib/kryshanti-anki/anki.env`.
- `seedlogin-src/` — one-shot AnkiWeb auth bootstrap addon. Reads
  `ANKIWEB_USERNAME` / `ANKIWEB_PASSWORD` from env, calls
  `Collection.sync_login()`, persists the resulting `hkey` to
  `prefs21.db` via `ProfileManager.set_sync_key()` + `pm.save()`.
  Idempotent: re-running while `syncKey` is already set is a no-op.
  Logs to `/data/seedlogin.log`. Staged into the bind-mounted `data/`
  at container start (so the addon travels with collection state, not
  the image). To reset auth and force re-login on the next start,
  clear `syncKey` in the profile pickle (or `rm` the profile dir to
  wipe the collection entirely).

## Caveats

Upstream AnkiConnect's `sync` action only handles sync statuses 0
(NO_CHANGES) and 1 (NORMAL_SYNC). If the local collection diverges from
AnkiWeb such that Anki returns status 4 (FULL_SYNC required), `sync`
raises an error. The local patch
(`patches/0001-add-forceUpload-forceDownload.patch`) adds `forceUpload`
/ `forceDownload` actions that call
`Collection.full_upload_or_download(upload=True|False)` through the
same lifecycle hooks Anki's desktop UI uses (gui_hooks +
`close_for_full_sync` + `reopen(after_full_sync=True)`), making the
FULL_SYNC case driveable from a headless agent.
