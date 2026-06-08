#!/usr/bin/env bats
#
# Test suite for ops/container/bootstrap-profile.sh.
#
# Coverage matrix:
#   A. Arg parsing / usage
#   B. File-type validation (.apkg accepted, .colpkg rejected with workflow
#      pointer, other extensions rejected, missing file rejected)
#   C. Profile preflight (existing-non-empty without --force, --force clears,
#      reserved names rejected)
#   D. AnkiConnect happy path (mocked) — script makes the expected calls in
#      the expected order and reports success
#   E. AnkiConnect failure paths (mocked) — importPackage returns null+error,
#      deck count didn't grow, AnkiConnect doesn't come up, systemctl fails
#   F. Credential handling (--ankiweb-pass-file mode-check, no password on
#      command line)
#   G. Env-file save/restore (PR #23): ORIGINAL_DEFAULT_PROFILE preserved
#      after success and after failure
#
# Each test runs in an isolated tmpdir with stubbed systemctl + mock
# AnkiConnect. Real /var/lib/kryshanti-anki is never touched.

setup() {
  TMPDIR_TEST="$(mktemp -d -t bats-bootstrap-XXXXXX)"

  # State paths — script reads these via env-var fallback (see PR #21 change).
  export STATE_DIR="$TMPDIR_TEST/state"
  export DATA_DIR="$STATE_DIR/data"
  export ENV_FILE="$STATE_DIR/anki.env"
  export IMPORT_DIR="$DATA_DIR/_import"
  mkdir -p "$DATA_DIR"

  # Pre-seed an anki.env with a known default so save/restore tests have
  # something to assert on. Individual tests may overwrite this.
  cat > "$ENV_FILE" <<EOF
KRYSHANTI_ANKI_DEFAULT_PROFILE=_anki_skill_testrun
ANKIWEB_USERNAME=stub@example.com
EOF
  chmod 600 "$ENV_FILE"

  # systemctl mock via PATH (script calls `systemctl` directly; our stub wins).
  export PATH="$BATS_TEST_DIRNAME/_fixtures:$PATH"
  export SYSTEMCTL_LOG="$TMPDIR_TEST/systemctl.log"
  export SYSTEMCTL_BEHAVIOR=ok

  # Override UNIT_NAME so the mock systemctl log is clear about what we asked.
  export UNIT_NAME="kryshanti-anki-bats.service"

  # AnkiConnect knobs. Tests that need the mock will start it and overwrite
  # ANKICONNECT_URL with the actual port. Tests that don't need the mock can
  # leave it pointing at a definitely-closed port and rely on validation
  # paths firing before any HTTP call.
  export ANKICONNECT_URL="http://127.0.0.1:65535"  # closed port (default)
  export ANKICONNECT_WAIT_MAX=3                    # snappy timeouts for tests

  # The script under test.
  SCRIPT="$BATS_TEST_DIRNAME/../bootstrap-profile.sh"

  # Default .apkg fixture (validation-only tests don't actually open the file).
  FAKE_APKG="$TMPDIR_TEST/fixture.apkg"
  echo "fake apkg payload" > "$FAKE_APKG"
}

teardown() {
  # Best-effort: kill any mock AnkiConnect we started.
  if [[ -n "${MOCK_PID:-}" ]]; then
    kill "$MOCK_PID" 2>/dev/null || true
    wait "$MOCK_PID" 2>/dev/null || true
  fi
  rm -rf "$TMPDIR_TEST"
}

# Helper: start the mock AnkiConnect on a free localhost port and export
# ANKICONNECT_URL to point at it. Programs the canned responses from the
# given JSON file (or empty file if no responses programmed).
start_mock_ankiconnect() {
  local responses_file="${1:-}"
  local port

  # Find a free port by binding-and-releasing.
  port=$(python3 -c '
import socket
s = socket.socket()
s.bind(("127.0.0.1", 0))
print(s.getsockname()[1])
s.close()
')
  local responses_arg=""
  if [[ -n "$responses_file" ]]; then
    responses_arg="--responses $responses_file"
  fi

  local out="$TMPDIR_TEST/mock.stdout"
  : > "$out"
  python3 "$BATS_TEST_DIRNAME/_fixtures/mock_ankiconnect.py" \
    --port "$port" \
    $responses_arg \
    --log "$TMPDIR_TEST/ankiconnect.log" \
    > "$out" 2>&1 &
  MOCK_PID=$!

  # Wait for READY (max 5s)
  local deadline=$(( $(date +%s) + 5 ))
  while (( $(date +%s) < deadline )); do
    if grep -q '^READY ' "$out" 2>/dev/null; then
      export ANKICONNECT_URL="http://127.0.0.1:$port"
      return 0
    fi
    sleep 0.1
  done
  echo "mock AnkiConnect didn't come up; stdout:" >&2
  cat "$out" >&2
  return 1
}

# Helper: write a canned-responses file from a here-string.
write_responses() {
  local path="$TMPDIR_TEST/responses.json"
  cat > "$path"
  echo "$path"
}

# ========================================================================
# A. Arg parsing / usage
# ========================================================================

@test "A1: no args -> usage, missing profile error" {
  run "$SCRIPT"
  [ "$status" -ne 0 ]
  [[ "$output" == *"Missing <profile-name>"* ]]
  [[ "$output" == *"Usage:"* ]]
}

@test "A2: missing --import -> usage error" {
  run "$SCRIPT" some_profile
  [ "$status" -ne 0 ]
  [[ "$output" == *"Missing --import"* ]]
}

@test "A3: unknown flag -> usage" {
  run "$SCRIPT" some_profile --import "$FAKE_APKG" --bogus-flag
  [ "$status" -ne 0 ]
  [[ "$output" == *"Unknown flag: --bogus-flag"* ]]
}

@test "A4: help flag prints usage" {
  run "$SCRIPT" --help
  [ "$status" -ne 0 ]
  [[ "$output" == *"Usage:"* ]]
}

# ========================================================================
# B. File-type validation
# ========================================================================

@test "B1: .colpkg rejected with workflow pointer + issue link" {
  touch "$TMPDIR_TEST/x.colpkg"
  run "$SCRIPT" testprof --import "$TMPDIR_TEST/x.colpkg"
  [ "$status" -eq 2 ]
  [[ "$output" == *".colpkg is not supported"* ]]
  [[ "$output" == *"anki-manager#19"* || "$output" == *"anki-manager/issues/19"* ]]
  [[ "$output" == *"File > Export"* ]]
}

@test "B2: unknown extension rejected" {
  touch "$TMPDIR_TEST/x.zip"
  run "$SCRIPT" testprof --import "$TMPDIR_TEST/x.zip"
  [ "$status" -eq 2 ]
  [[ "$output" == *"Unrecognized import file extension"* ]]
  [[ "$output" == *"Expected .apkg"* ]]
}

@test "B3: missing import file rejected" {
  run "$SCRIPT" testprof --import "$TMPDIR_TEST/does-not-exist.apkg"
  [ "$status" -eq 1 ]
  [[ "$output" == *"Import file not found"* ]]
}

@test "B4: .apkg extension is accepted (validation passes; later steps run)" {
  # We expect this to make it past file-type validation and fail later
  # (because EUID check or AnkiConnect mock not set up). Either way, the
  # .colpkg / wrong-extension messages must NOT appear.
  run "$SCRIPT" testprof --import "$FAKE_APKG"
  [[ "$output" != *"Unrecognized import file extension"* ]]
  [[ "$output" != *".colpkg is not supported"* ]]
}

@test "B5: .APKG (uppercase) accepted (case-insensitive extension check)" {
  cp "$FAKE_APKG" "$TMPDIR_TEST/upper.APKG"
  run "$SCRIPT" testprof --import "$TMPDIR_TEST/upper.APKG"
  [[ "$output" != *"Unrecognized import file extension"* ]]
}

@test "B6: .COLPKG (uppercase) still rejected" {
  touch "$TMPDIR_TEST/upper.COLPKG"
  run "$SCRIPT" testprof --import "$TMPDIR_TEST/upper.COLPKG"
  [ "$status" -eq 2 ]
  [[ "$output" == *".colpkg is not supported"* ]]
}

# ========================================================================
# C. Profile preflight
# ========================================================================

@test "C1: reserved profile name '_import' rejected" {
  run "$SCRIPT" _import --import "$FAKE_APKG"
  [ "$status" -eq 1 ]
  [[ "$output" == *"Reserved profile name"* ]]
}

@test "C2: reserved profile name 'addons21' rejected" {
  run "$SCRIPT" addons21 --import "$FAKE_APKG"
  [ "$status" -eq 1 ]
  [[ "$output" == *"Reserved profile name"* ]]
}

@test "C3: profile name with leading dot rejected" {
  run "$SCRIPT" ".hidden" --import "$FAKE_APKG"
  [ "$status" -eq 1 ]
  [[ "$output" == *"Reserved profile name"* ]]
}

@test "C4: existing non-empty profile dir without --force fails" {
  # Need to bypass the EUID check; this is a state-dependent check that runs
  # after privilege validation. BOOTSTRAP_ALLOW_NON_ROOT=1 is the test hook.
  mkdir -p "$DATA_DIR/testprof"
  touch "$DATA_DIR/testprof/collection.anki2"
  BOOTSTRAP_ALLOW_NON_ROOT=1 run "$SCRIPT" testprof --import "$FAKE_APKG"
  [ "$status" -eq 1 ]
  [[ "$output" == *"already exists and is non-empty"* ]]
  [[ "$output" == *"--force"* ]]
}

@test "C5: --force clears existing non-empty profile dir and proceeds" {
  mkdir -p "$DATA_DIR/testprof"
  touch "$DATA_DIR/testprof/collection.anki2"
  # Without AnkiConnect set up, this will fail later — but past the
  # profile-dir check. We assert the --force message appeared and the
  # dir is no longer non-empty.
  BOOTSTRAP_ALLOW_NON_ROOT=1 run "$SCRIPT" testprof --import "$FAKE_APKG" --force
  [[ "$output" == *"--force: removing existing"* ]]
  # Either the dir is gone or empty
  [[ ! -d "$DATA_DIR/testprof" || -z "$(ls -A "$DATA_DIR/testprof" 2>/dev/null)" ]]
}

@test "C6: missing STATE_DIR fails with host-setup hint" {
  rm -rf "$STATE_DIR"
  BOOTSTRAP_ALLOW_NON_ROOT=1 run "$SCRIPT" testprof --import "$FAKE_APKG"
  [ "$status" -eq 1 ]
  [[ "$output" == *"State dir"* && "$output" == *"missing"* ]]
  [[ "$output" == *"host-setup.sh"* ]]
}

# ========================================================================
# D. AnkiConnect happy path (mocked)
# ========================================================================

# Build a JSON response file for happy-path mocked AnkiConnect.
happy_responses() {
  cat <<'JSON'
{
  "version": {"result": 6, "error": null},
  "getActiveProfile": [
    {"result": "testprof", "error": null},
    {"result": "testprof", "error": null}
  ],
  "importPackage": {"result": true, "error": null},
  "deckNames": {"result": ["Default", "Imported::Sub", "Imported::Other"], "error": null}
}
JSON
}

@test "D1: happy path — script completes successfully" {
  local responses
  responses=$(happy_responses | write_responses)
  start_mock_ankiconnect "$responses"

  BOOTSTRAP_ALLOW_NON_ROOT=1 run "$SCRIPT" testprof --import "$FAKE_APKG" --skip-credentials
  [ "$status" -eq 0 ]
  [[ "$output" == *"Bootstrap complete"* ]]
  [[ "$output" == *"Profile: testprof"* ]]
}

@test "D2: happy path — calls AnkiConnect in expected sequence" {
  local responses
  responses=$(happy_responses | write_responses)
  start_mock_ankiconnect "$responses"

  BOOTSTRAP_ALLOW_NON_ROOT=1 run "$SCRIPT" testprof --import "$FAKE_APKG" --skip-credentials
  [ "$status" -eq 0 ]

  # Expected sequence (in order): version (polling), getActiveProfile,
  # importPackage, deckNames, [optional version polling at end]
  local log="$TMPDIR_TEST/ankiconnect.log"
  [ -f "$log" ]
  local actions
  actions=$(python3 -c '
import json
with open("'"$log"'") as f:
    for line in f:
        rec = json.loads(line)
        print(rec["action"])
' | grep -v '^$' | tr '\n' ',')
  # Strip trailing comma
  actions="${actions%,}"
  # First non-version call must be getActiveProfile (after polling), then importPackage, then deckNames
  [[ "$actions" == *"getActiveProfile,importPackage,deckNames"* ]]
}

@test "D3: happy path — systemctl invoked stop, start, then restart for restore" {
  # ORIGINAL == _anki_skill_testrun, target == testprof, so steps 8/9 fire
  local responses
  responses=$(happy_responses | write_responses)
  start_mock_ankiconnect "$responses"

  BOOTSTRAP_ALLOW_NON_ROOT=1 run "$SCRIPT" testprof --import "$FAKE_APKG" --skip-credentials
  [ "$status" -eq 0 ]

  # We expect: stop (step 1), start (step 3), restart (step 9). Cred restart
  # is skipped via --skip-credentials.
  local log="$SYSTEMCTL_LOG"
  [ -f "$log" ]
  grep -q "^stop $UNIT_NAME" "$log"
  grep -q "^start $UNIT_NAME" "$log"
  grep -q "^restart $UNIT_NAME" "$log"
}

# ========================================================================
# E. AnkiConnect failure paths (mocked)
# ========================================================================

@test "E1: importPackage error -> script fails, env file restored" {
  local responses
  responses=$(cat <<'JSON' | write_responses
{
  "version": {"result": 6, "error": null},
  "getActiveProfile": {"result": "testprof", "error": null},
  "importPackage": {"result": null, "error": "mock-induced failure"}
}
JSON
)
  start_mock_ankiconnect "$responses"

  BOOTSTRAP_ALLOW_NON_ROOT=1 run "$SCRIPT" testprof --import "$FAKE_APKG" --skip-credentials
  [ "$status" -ne 0 ]
  [[ "$output" == *"importPackage failed: mock-induced failure"* ]]

  # Steady-state property: env file must be restored to original.
  grep -q "^KRYSHANTI_ANKI_DEFAULT_PROFILE=_anki_skill_testrun$" "$ENV_FILE"
  [[ "$output" == *"restoring"* ]]
}

@test "E2: deckNames returns only Default -> script fails with helpful msg" {
  local responses
  responses=$(cat <<'JSON' | write_responses
{
  "version": {"result": 6, "error": null},
  "getActiveProfile": {"result": "testprof", "error": null},
  "importPackage": {"result": true, "error": null},
  "deckNames": {"result": ["Default"], "error": null}
}
JSON
)
  start_mock_ankiconnect "$responses"

  BOOTSTRAP_ALLOW_NON_ROOT=1 run "$SCRIPT" testprof --import "$FAKE_APKG" --skip-credentials
  [ "$status" -ne 0 ]
  [[ "$output" == *"Only 1 deck"* ]]
  # Env file restored.
  grep -q "^KRYSHANTI_ANKI_DEFAULT_PROFILE=_anki_skill_testrun$" "$ENV_FILE"
}

@test "E3: AnkiConnect never comes up -> script fails" {
  # No mock started; ANKICONNECT_URL points at port 65535 (closed).
  BOOTSTRAP_ALLOW_NON_ROOT=1 run "$SCRIPT" testprof --import "$FAKE_APKG" --skip-credentials
  [ "$status" -ne 0 ]
  [[ "$output" == *"AnkiConnect didn't come up"* ]]
  # Env file restored.
  grep -q "^KRYSHANTI_ANKI_DEFAULT_PROFILE=_anki_skill_testrun$" "$ENV_FILE"
}

@test "E4: wrong profile active after start -> script fails" {
  local responses
  responses=$(cat <<'JSON' | write_responses
{
  "version": {"result": 6, "error": null},
  "getActiveProfile": {"result": "different_profile", "error": null}
}
JSON
)
  start_mock_ankiconnect "$responses"

  BOOTSTRAP_ALLOW_NON_ROOT=1 run "$SCRIPT" testprof --import "$FAKE_APKG" --skip-credentials
  [ "$status" -ne 0 ]
  [[ "$output" == *"Expected profile testprof"* ]]
  [[ "$output" == *"got different_profile"* ]]
  grep -q "^KRYSHANTI_ANKI_DEFAULT_PROFILE=_anki_skill_testrun$" "$ENV_FILE"
}

@test "E5: systemctl start fails -> script fails, env file restored" {
  export SYSTEMCTL_BEHAVIOR=fail-start
  BOOTSTRAP_ALLOW_NON_ROOT=1 run "$SCRIPT" testprof --import "$FAKE_APKG" --skip-credentials
  [ "$status" -ne 0 ]
  # No matter why we failed, env file must be restored.
  grep -q "^KRYSHANTI_ANKI_DEFAULT_PROFILE=_anki_skill_testrun$" "$ENV_FILE"
}

# ========================================================================
# F. Credential handling
# ========================================================================

@test "F1: --ankiweb-pass-file with mode 644 rejected" {
  local pass_file="$TMPDIR_TEST/pw"
  echo "supersecret" > "$pass_file"
  chmod 644 "$pass_file"
  BOOTSTRAP_ALLOW_NON_ROOT=1 run "$SCRIPT" testprof \
    --import "$FAKE_APKG" \
    --ankiweb-user test@example.com \
    --ankiweb-pass-file "$pass_file"
  [ "$status" -ne 0 ]
  [[ "$output" == *"must be chmod 600"* ]]
}

@test "F2: --ankiweb-pass-file with mode 600 accepted" {
  local pass_file="$TMPDIR_TEST/pw"
  echo "supersecret" > "$pass_file"
  chmod 600 "$pass_file"
  # Validation only — expect script to advance past the mode check.
  # The downstream AnkiConnect path will fail (no mock), but the pass-file
  # mode message must not be in the output.
  BOOTSTRAP_ALLOW_NON_ROOT=1 run "$SCRIPT" testprof \
    --import "$FAKE_APKG" \
    --ankiweb-user test@example.com \
    --ankiweb-pass-file "$pass_file"
  [[ "$output" != *"must be chmod 600"* ]]
}

@test "F3: --ankiweb-pass-file missing file rejected" {
  BOOTSTRAP_ALLOW_NON_ROOT=1 run "$SCRIPT" testprof \
    --import "$FAKE_APKG" \
    --ankiweb-user test@example.com \
    --ankiweb-pass-file "$TMPDIR_TEST/no-such-file"
  [ "$status" -ne 0 ]
  [[ "$output" == *"Pass file not found"* ]]
}

# ========================================================================
# G. Env-file save/restore (PR #23's steady-state property)
# ========================================================================

@test "G1: bootstrap success preserves ORIGINAL KRYSHANTI_ANKI_DEFAULT_PROFILE" {
  # Pre-state: ENV_FILE has KRYSHANTI_ANKI_DEFAULT_PROFILE=_anki_skill_testrun
  # Target: testprof (different)
  local responses
  responses=$(happy_responses | write_responses)
  start_mock_ankiconnect "$responses"

  BOOTSTRAP_ALLOW_NON_ROOT=1 run "$SCRIPT" testprof --import "$FAKE_APKG" --skip-credentials
  [ "$status" -eq 0 ]
  # Post-state: must be back to _anki_skill_testrun
  grep -q "^KRYSHANTI_ANKI_DEFAULT_PROFILE=_anki_skill_testrun$" "$ENV_FILE"
}

@test "G2: ORIGINAL == TARGET -> step 8 reports no-op" {
  # Pre-state: KRYSHANTI_ANKI_DEFAULT_PROFILE=testprof (same as target)
  cat > "$ENV_FILE" <<EOF
KRYSHANTI_ANKI_DEFAULT_PROFILE=testprof
EOF
  chmod 600 "$ENV_FILE"

  local responses
  responses=$(happy_responses | write_responses)
  start_mock_ankiconnect "$responses"

  BOOTSTRAP_ALLOW_NON_ROOT=1 run "$SCRIPT" testprof --import "$FAKE_APKG" --skip-credentials
  [ "$status" -eq 0 ]
  [[ "$output" == *"no restore needed"* ]]
  grep -q "^KRYSHANTI_ANKI_DEFAULT_PROFILE=testprof$" "$ENV_FILE"
}

@test "G3: pre-state with NO env var set -> bootstrap adds target, then removes on restore" {
  # Pre-state: ENV_FILE has unrelated vars only
  cat > "$ENV_FILE" <<EOF
SOMETHING_ELSE=foo
EOF
  chmod 600 "$ENV_FILE"

  local responses
  responses=$(happy_responses | write_responses)
  start_mock_ankiconnect "$responses"

  BOOTSTRAP_ALLOW_NON_ROOT=1 run "$SCRIPT" testprof --import "$FAKE_APKG" --skip-credentials
  [ "$status" -eq 0 ]
  # Post-state: no KRYSHANTI_ANKI_DEFAULT_PROFILE line in the file
  ! grep -q "^KRYSHANTI_ANKI_DEFAULT_PROFILE=" "$ENV_FILE"
  # Other vars preserved
  grep -q "^SOMETHING_ELSE=foo$" "$ENV_FILE"
}

