# bootstrap-profile.sh test suite

[bats](https://github.com/bats-core/bats-core) unit tests for the profile-bootstrap
script. Mocks `systemctl`, `docker`, and AnkiConnect — runs on any host without
root or the real `kryshanti-anki` unit.

## Running

```sh
ops/container/tests/run.sh
```

Prints install hints if `bats` isn't on `PATH`.

## Coverage

31 tests across seven groups:

| Group | What's covered |
|---|---|
| A. Arg parsing | usage, missing args, unknown flags, `--help` |
| B. File-type validation | `.apkg` accepted, `.colpkg` rejected with workflow + #19 link, unknown extensions, missing file, `.APKG`/`.COLPKG` case-insensitive |
| C. Profile preflight | reserved names (`_import`, `addons21`, `.dotted`), existing non-empty profile dir (without/with `--force`), missing `STATE_DIR` |
| D. Sidecar happy path | script completes; `docker run` invoked with `-e KRYSHANTI_ANKI_DEFAULT_PROFILE=<target>`; `systemctl` sequence is stop → start (no restart-around-import); AnkiConnect calls in order `getActiveProfile → importPackageWithLog` |
| E. AnkiConnect failure paths | `importPackageWithLog` errors; `deck_count_after` returns 1; AnkiConnect doesn't come up; wrong profile active; `docker run` fails — **all assert `anki.env` byte-identical to pre-state** |
| F. Credentials | `--ankiweb-pass-file` mode 644 rejected; mode 600 accepted; missing file rejected |
| G. Steady-state property | (PR #30's closing assertion for #20) `anki.env` byte-identical after success; identical even when target == current default; credentials write does NOT change `KRYSHANTI_ANKI_DEFAULT_PROFILE` |

## How the mocks work

### `_fixtures/systemctl`

PATH-shim. Wins over the real binary because bats prepends `_fixtures/` to `$PATH`.

- `SYSTEMCTL_LOG`: appends `<argv>` per call.
- `SYSTEMCTL_BEHAVIOR`: `ok` (default), `fail-start`, `fail-stop`, `fail-restart`.

### `_fixtures/docker`

Same pattern as `systemctl`. Handles `run`/`stop`/`cp` etc.

- `DOCKER_LOG`: appends `<argv>` per call.
- `DOCKER_BEHAVIOR`: `ok` (default), `fail-run`, `fail-stop`.
- `docker run -d` emits a fake container id on stdout (to mimic real behavior so the script can capture it if it wants).

### `_fixtures/mock_ankiconnect.py`

Python `http.server` mock. Programmable per-action responses (dict for fixed, list for queue-with-exhaustion-error). Logs received requests as JSON lines.

### Script-side env-var hooks

`bootstrap-profile.sh` exposes env-var fallbacks for all its config knobs and a
`BOOTSTRAP_ALLOW_NON_ROOT=1` bypass for the EUID check. These are inert outside
the test fixtures — production callers don't set them.

| Var | Default | Used for |
|---|---|---|
| `STATE_DIR` | `/var/lib/kryshanti-anki` | redirect state root |
| `DATA_DIR` / `ENV_FILE` / `IMPORT_DIR` | derived from `STATE_DIR` | individual overrides |
| `UNIT_NAME` | `kryshanti-anki.service` | mock-systemctl assertions |
| `IMAGE_TAG` | `kryshanti-anki:25.02.7` | mock-docker assertions |
| `BOOTSTRAP_CONTAINER_NAME` | `kryshanti-anki-bootstrap` | mock-docker assertions |
| `ANKICONNECT_URL` | `http://127.0.0.1:8765` | mock-AnkiConnect port |
| `ANKICONNECT_WAIT_MAX` | `60` | snappier timeouts in tests |
| `BOOTSTRAP_ALLOW_NON_ROOT` | `0` | bypass EUID check |

## Live integration test (not in this suite)

This suite is unit-level. The live test against the running `kryshanti-anki`
container with the user's real `.apkg` is the v0.1 smoke acceptance for
`anki-translator#43`, driven by Sorotassu directly. See PR description for
the live-smoke procedure.
