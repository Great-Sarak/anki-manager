# bootstrap-profile.sh test suite

[bats](https://github.com/bats-core/bats-core)-based unit tests for the
profile-bootstrap script. Mocks systemctl and AnkiConnect so tests run on
any host without root or the real `kryshanti-anki` unit.

## Running

```sh
ops/container/tests/run.sh
```

The runner checks for `bats` on `PATH` and prints installation hints if
absent. Verbose output and filtering:

```sh
ops/container/tests/run.sh --verbose-run                  # show stdout of each test
ops/container/tests/run.sh --filter 'B[0-9]+:'            # only file-type tests
ops/container/tests/run.sh --filter 'happy path'          # only D1/D2/D3
```

## Coverage

30 tests across seven categories:

| Group | What's covered |
|---|---|
| A. Arg parsing | usage, missing args, unknown flags, --help |
| B. File-type validation | `.apkg` accepted, `.colpkg` rejected with workflow pointer, unknown extensions rejected, missing file rejected, case-insensitive extension matching |
| C. Profile preflight | reserved names (`_import`, `addons21`, `.dotted`), existing non-empty profile dir (with/without `--force`), missing STATE_DIR |
| D. AnkiConnect happy path | script completes successfully; calls made in expected order (`getActiveProfile → importPackage → deckNames`); systemctl invoked stop/start/restart |
| E. AnkiConnect failure paths | `importPackage` returns error, deckNames doesn't grow, AnkiConnect doesn't come up, wrong profile active, systemctl start fails — all restore the env file |
| F. Credentials | `--ankiweb-pass-file` mode check (rejects 644, accepts 600), missing pass file rejected |
| G. Env-file save/restore | success path preserves `ORIGINAL_DEFAULT_PROFILE`; `ORIGINAL == TARGET` triggers no-op restore; pre-state without the env var → bootstrap appends and removes |

## How the mocks work

### `_fixtures/systemctl`

PATH-shim that bats prepends to `$PATH`. Wins over the real `systemctl` so
the script's `systemctl stop|start|restart $UNIT_NAME` calls hit the mock.

Controlled by env vars:

- `SYSTEMCTL_LOG` (path): if set, appends `<argv>` per invocation.
- `SYSTEMCTL_BEHAVIOR`: `ok` (default), `fail-start`, `fail-stop`, `fail-restart`.

### `_fixtures/mock_ankiconnect.py`

Python `http.server` that listens on a configurable port and returns canned
JSON responses for AnkiConnect actions. Each test programs responses via a
JSON file:

```json
{
  "version": {"result": 6, "error": null},
  "getActiveProfile": [
    {"result": "testprof", "error": null},
    {"result": "testprof", "error": null}
  ],
  "importPackage": {"result": true, "error": null},
  "deckNames": {"result": ["Default", "Imported::A"], "error": null}
}
```

Per-action entries can be:

- A dict — returned for every call to that action.
- A list — queue semantics; one response per call in order; exhaustion returns an error response.

Logs every received request as one JSON line to the path given by `--log`.

### Test-only env-var hooks in the script under test

`bootstrap-profile.sh` exposes a few env-var fallbacks so the bats suite can
sandbox state without root or touching `/var/lib/kryshanti-anki`:

| Env var | Default | Used for |
|---|---|---|
| `STATE_DIR` | `/var/lib/kryshanti-anki` | redirect state root |
| `DATA_DIR` / `ENV_FILE` / `IMPORT_DIR` | derived from `STATE_DIR` | individual overrides |
| `UNIT_NAME` | `kryshanti-anki.service` | mock-systemctl assertions |
| `ANKICONNECT_URL` | `http://127.0.0.1:8765` | mock-AnkiConnect port |
| `ANKICONNECT_WAIT_MAX` | `60` | snappier timeouts in tests |
| `BOOTSTRAP_ALLOW_NON_ROOT` | `0` | bypass EUID check (for body tests) |

Production callers never set these. The fallbacks are inert outside test
fixtures.

## Adding a test

```bash
@test "ID: human-readable description" {
  # Setup: state files in $TMPDIR_TEST (cleaned up automatically),
  # mock AnkiConnect if needed via start_mock_ankiconnect.

  local responses
  responses=$(write_responses <<'JSON'
{ ... }
JSON
)
  start_mock_ankiconnect "$responses"

  BOOTSTRAP_ALLOW_NON_ROOT=1 run "$SCRIPT" <args>
  [ "$status" -eq 0 ]
  [[ "$output" == *"expected substring"* ]]
}
```

`run` is bats's wrapper that captures `$status` and `$output` from the
command, suppressing `set -e` propagation. Use it for any invocation
where you want to assert against the result rather than letting a failure
abort the test.

## Live integration test (not in this suite)

This suite is unit-level: every external dependency is mocked. The live
integration test — actually running `bootstrap-profile.sh` against the
running `kryshanti-anki:25.02.7` container with a real `.apkg` — is the
v0.1 smoke acceptance for [anki-translator#43](https://github.com/Great-Sarak/anki-translator/issues/43), driven by Sorotassu
on his host.
