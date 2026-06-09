#!/usr/bin/env bats
#
# Test suite for ops/container/bootstrap-profile.sh.
#
# Coverage matrix:
#   A. Arg parsing / usage
#   B. File-type validation (.apkg accepted, .colpkg rejected with workflow
#      pointer, other extensions rejected, missing file rejected)
#   C. Profile preflight (reserved names; existing non-empty profile dir
#      without/with --force; missing STATE_DIR)
#   D. Sidecar happy path (mocked) — script makes the expected docker run
#      with -e KRYSHANTI_ANKI_DEFAULT_PROFILE=<target>, calls importPackageWithLog,
#      stops the transient container, restarts the persistent unit
#   E. AnkiConnect failure paths (mocked) — importPackageWithLog errors,
#      deck count too low, AnkiConnect doesn't come up, wrong profile loaded
#   F. Credential handling (--ankiweb-pass-file mode-check, missing file)
#   G. Steady-state property (PR #30): anki.env's
#      KRYSHANTI_ANKI_DEFAULT_PROFILE is byte-identical after success AND
#      after each failure path
#
# Each test runs in an isolated tmpdir. systemctl + docker + AnkiConnect
# are all mocked. Real /var/lib/kryshanti-anki is never touched.

setup() {
  TMPDIR_TEST="$(mktemp -d -t bats-bootstrap-XXXXXX)"

  # State paths (script reads via env-var fallback)
  export STATE_DIR="$TMPDIR_TEST/state"
  export DATA_DIR="$STATE_DIR/data"
  export ENV_FILE="$STATE_DIR/anki.env"
  export IMPORT_DIR="$DATA_DIR/_import"
  mkdir -p "$DATA_DIR"

  # Pre-seed anki.env with a known default so the steady-state tests can
  # assert against it. The value is intentionally NOT the bootstrap target.
  cat > "$ENV_FILE" <<EOF
KRYSHANTI_ANKI_DEFAULT_PROFILE=_anki_skill_testrun
ANKIWEB_USERNAME=stub@example.com
EOF
  chmod 600 "$ENV_FILE"

  # Capture pre-state of the env file as a checksum, for G* tests.
  ENV_FILE_PRE_SHA="$(sha256sum "$ENV_FILE" | awk '{print $1}')"
  export ENV_FILE_PRE_SHA

  # PATH-shim mocks for systemctl + docker.
  export PATH="$BATS_TEST_DIRNAME/_fixtures:$PATH"
  export SYSTEMCTL_LOG="$TMPDIR_TEST/systemctl.log"
  export SYSTEMCTL_BEHAVIOR=ok
  export DOCKER_LOG="$TMPDIR_TEST/docker.log"
  export DOCKER_BEHAVIOR=ok

  # Make sure UNIT_NAME and IMAGE_TAG are distinct enough in the mock logs.
  export UNIT_NAME="kryshanti-anki-bats.service"
  export IMAGE_TAG="kryshanti-anki:bats"
  export BOOTSTRAP_CONTAINER_NAME="kryshanti-anki-bootstrap-bats"

  # AnkiConnect URL points at a closed port by default; tests that need the
  # mock start it explicitly via start_mock_ankiconnect and overwrite this.
  export ANKICONNECT_URL="http://127.0.0.1:65535"
  export ANKICONNECT_WAIT_MAX=3

  SCRIPT="$BATS_TEST_DIRNAME/../bootstrap-profile.sh"
  FAKE_APKG="$TMPDIR_TEST/fixture.apkg"
  echo "fake apkg payload" > "$FAKE_APKG"
}

teardown() {
  if [[ -n "${MOCK_PID:-}" ]]; then
    kill "$MOCK_PID" 2>/dev/null || true
    wait "$MOCK_PID" 2>/dev/null || true
  fi
  rm -rf "$TMPDIR_TEST"
}

start_mock_ankiconnect() {
  local responses_file="${1:-}"
  local port
  port=$(python3 -c 'import socket
s=socket.socket()
s.bind(("127.0.0.1", 0))
print(s.getsockname()[1])
s.close()')
  local responses_arg=""
  [[ -n "$responses_file" ]] && responses_arg="--responses $responses_file"

  local out="$TMPDIR_TEST/mock.stdout"
  : > "$out"
  python3 "$BATS_TEST_DIRNAME/_fixtures/mock_ankiconnect.py" \
    --port "$port" \
    $responses_arg \
    --log "$TMPDIR_TEST/ankiconnect.log" \
    > "$out" 2>&1 &
  MOCK_PID=$!

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

write_responses() {
  local path="$TMPDIR_TEST/responses.json"
  cat > "$path"
  echo "$path"
}

env_file_sha() {
  sha256sum "$ENV_FILE" | awk '{print $1}'
}

# Programmed responses for sidecar-happy-path tests
happy_responses() {
  cat <<'JSON'
{
  "version": {"result": 6, "error": null},
  "getActiveProfile": {"result": "testprof", "error": null},
  "importPackageWithLog": {"result": {
    "found_notes": 100,
    "new": 100,
    "updated": 0,
    "duplicate": 0,
    "conflicting": 0,
    "deck_count_after": 12
  }, "error": null}
}
JSON
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

@test "A4: --help prints usage" {
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
}

@test "B3: missing import file rejected" {
  run "$SCRIPT" testprof --import "$TMPDIR_TEST/missing.apkg"
  [ "$status" -eq 1 ]
  [[ "$output" == *"Import file not found"* ]]
}

@test "B4: .apkg accepted (passes file-type validation)" {
  run "$SCRIPT" testprof --import "$FAKE_APKG"
  [[ "$output" != *"Unrecognized import file extension"* ]]
  [[ "$output" != *".colpkg is not supported"* ]]
}

@test "B5: .APKG (uppercase) accepted" {
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
  run "$SCRIPT" ".dotted" --import "$FAKE_APKG"
  [ "$status" -eq 1 ]
  [[ "$output" == *"Reserved profile name"* ]]
}

@test "C4: existing non-empty profile dir without --force fails" {
  mkdir -p "$DATA_DIR/testprof"
  touch "$DATA_DIR/testprof/collection.anki2"
  BOOTSTRAP_ALLOW_NON_ROOT=1 run "$SCRIPT" testprof --import "$FAKE_APKG"
  [ "$status" -eq 1 ]
  [[ "$output" == *"already exists and is non-empty"* ]]
  [[ "$output" == *"--force"* ]]
}

@test "C5: --force clears existing non-empty profile dir" {
  mkdir -p "$DATA_DIR/testprof"
  touch "$DATA_DIR/testprof/collection.anki2"
  BOOTSTRAP_ALLOW_NON_ROOT=1 run "$SCRIPT" testprof --import "$FAKE_APKG" --force
  [[ "$output" == *"--force: removing existing"* ]]
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
# D. Sidecar happy path (mocked)
# ========================================================================

@test "D1: happy path — script completes successfully" {
  local responses
  responses=$(happy_responses | write_responses)
  start_mock_ankiconnect "$responses"

  BOOTSTRAP_ALLOW_NON_ROOT=1 run "$SCRIPT" testprof --import "$FAKE_APKG" --skip-credentials
  [ "$status" -eq 0 ]
  [[ "$output" == *"Bootstrap complete"* ]]
  [[ "$output" == *"Profile: testprof"* ]]
}

@test "D2: docker run invoked with -e KRYSHANTI_ANKI_DEFAULT_PROFILE=<target>" {
  local responses
  responses=$(happy_responses | write_responses)
  start_mock_ankiconnect "$responses"

  BOOTSTRAP_ALLOW_NON_ROOT=1 run "$SCRIPT" testprof --import "$FAKE_APKG" --skip-credentials
  [ "$status" -eq 0 ]

  # The docker log should show: run ... -e KRYSHANTI_ANKI_DEFAULT_PROFILE=testprof
  grep -qE "^run .*-e KRYSHANTI_ANKI_DEFAULT_PROFILE=testprof" "$DOCKER_LOG"
}

@test "D3: systemctl sequence is stop then start (not restart-around-import)" {
  local responses
  responses=$(happy_responses | write_responses)
  start_mock_ankiconnect "$responses"

  BOOTSTRAP_ALLOW_NON_ROOT=1 run "$SCRIPT" testprof --import "$FAKE_APKG" --skip-credentials
  [ "$status" -eq 0 ]

  # Expected systemctl calls in order: stop <unit>, start <unit>.
  # No `restart` (the old design used restart-around-import; this design doesn't).
  local stops starts restarts
  stops=$(grep -cE "^stop $UNIT_NAME$" "$SYSTEMCTL_LOG" || true)
  starts=$(grep -cE "^start $UNIT_NAME$" "$SYSTEMCTL_LOG" || true)
  restarts=$(grep -cE "^restart $UNIT_NAME$" "$SYSTEMCTL_LOG" || true)
  [ "$stops" -ge 1 ]
  [ "$starts" -ge 1 ]
  [ "$restarts" -eq 0 ]
}

@test "D4: AnkiConnect calls in expected order: getActiveProfile, importPackageWithLog" {
  local responses
  responses=$(happy_responses | write_responses)
  start_mock_ankiconnect "$responses"

  BOOTSTRAP_ALLOW_NON_ROOT=1 run "$SCRIPT" testprof --import "$FAKE_APKG" --skip-credentials
  [ "$status" -eq 0 ]

  local actions
  actions=$(python3 -c "
import json
with open('$TMPDIR_TEST/ankiconnect.log') as f:
    for line in f:
        rec = json.loads(line)
        if rec['action']:
            print(rec['action'])
" | grep -vE '^version$' | head -5 | tr '\n' ',')
  [[ "$actions" == *"getActiveProfile,importPackageWithLog"* ]]
}

# ========================================================================
# E. AnkiConnect failure paths (mocked)
# ========================================================================

@test "E1: importPackageWithLog error -> script fails; env file unchanged" {
  local responses
  responses=$(cat <<'JSON' | write_responses
{
  "version": {"result": 6, "error": null},
  "getActiveProfile": {"result": "testprof", "error": null},
  "importPackageWithLog": {"result": null, "error": "mock-induced failure"}
}
JSON
)
  start_mock_ankiconnect "$responses"

  BOOTSTRAP_ALLOW_NON_ROOT=1 run "$SCRIPT" testprof --import "$FAKE_APKG" --skip-credentials
  [ "$status" -ne 0 ]
  [[ "$output" == *"importPackageWithLog failed: mock-induced failure"* ]]
  [ "$(env_file_sha)" = "$ENV_FILE_PRE_SHA" ]
}

@test "E2: deck_count_after returns 1 -> script fails with helpful msg; env unchanged" {
  local responses
  responses=$(cat <<'JSON' | write_responses
{
  "version": {"result": 6, "error": null},
  "getActiveProfile": {"result": "testprof", "error": null},
  "importPackageWithLog": {"result": {
    "found_notes": 0, "new": 0, "updated": 0, "duplicate": 0,
    "conflicting": 0, "deck_count_after": 1
  }, "error": null}
}
JSON
)
  start_mock_ankiconnect "$responses"

  BOOTSTRAP_ALLOW_NON_ROOT=1 run "$SCRIPT" testprof --import "$FAKE_APKG" --skip-credentials
  [ "$status" -ne 0 ]
  [[ "$output" == *"Only 1 deck"* ]]
  [ "$(env_file_sha)" = "$ENV_FILE_PRE_SHA" ]
}

@test "E3: AnkiConnect never comes up -> script fails; env unchanged" {
  # No mock started; ANKICONNECT_URL points at closed port.
  BOOTSTRAP_ALLOW_NON_ROOT=1 run "$SCRIPT" testprof --import "$FAKE_APKG" --skip-credentials
  [ "$status" -ne 0 ]
  [[ "$output" == *"AnkiConnect didn't come up"* ]]
  [ "$(env_file_sha)" = "$ENV_FILE_PRE_SHA" ]
}

@test "E4: wrong profile active after start -> script fails; env unchanged" {
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
  [ "$(env_file_sha)" = "$ENV_FILE_PRE_SHA" ]
}

@test "E5: docker run fails -> script fails; env unchanged" {
  export DOCKER_BEHAVIOR=fail-run
  BOOTSTRAP_ALLOW_NON_ROOT=1 run "$SCRIPT" testprof --import "$FAKE_APKG" --skip-credentials
  [ "$status" -ne 0 ]
  [ "$(env_file_sha)" = "$ENV_FILE_PRE_SHA" ]
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

@test "F2: --ankiweb-pass-file with mode 600 advances past mode check" {
  local pass_file="$TMPDIR_TEST/pw"
  echo "supersecret" > "$pass_file"
  chmod 600 "$pass_file"
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
# G. Steady-state property (PR #30's #20-closing behavior)
# ========================================================================

@test "G1: anki.env unchanged after successful bootstrap" {
  local responses
  responses=$(happy_responses | write_responses)
  start_mock_ankiconnect "$responses"

  BOOTSTRAP_ALLOW_NON_ROOT=1 run "$SCRIPT" testprof --import "$FAKE_APKG" --skip-credentials
  [ "$status" -eq 0 ]
  [ "$(env_file_sha)" = "$ENV_FILE_PRE_SHA" ]
  # KRYSHANTI_ANKI_DEFAULT_PROFILE value is the pre-bootstrap one, not the target
  grep -q "^KRYSHANTI_ANKI_DEFAULT_PROFILE=_anki_skill_testrun$" "$ENV_FILE"
}

@test "G2: anki.env unchanged after success even when bootstrap target == default" {
  cat > "$ENV_FILE" <<EOF
KRYSHANTI_ANKI_DEFAULT_PROFILE=testprof
ANKIWEB_USERNAME=stub@example.com
EOF
  chmod 600 "$ENV_FILE"
  ENV_FILE_PRE_SHA="$(env_file_sha)"

  local responses
  responses=$(happy_responses | write_responses)
  start_mock_ankiconnect "$responses"

  BOOTSTRAP_ALLOW_NON_ROOT=1 run "$SCRIPT" testprof --import "$FAKE_APKG" --skip-credentials
  [ "$status" -eq 0 ]
  [ "$(env_file_sha)" = "$ENV_FILE_PRE_SHA" ]
}

@test "G3: credentials write does NOT change KRYSHANTI_ANKI_DEFAULT_PROFILE" {
  local responses
  responses=$(happy_responses | write_responses)
  start_mock_ankiconnect "$responses"

  local pass_file="$TMPDIR_TEST/pw"
  echo "secret" > "$pass_file"
  chmod 600 "$pass_file"

  BOOTSTRAP_ALLOW_NON_ROOT=1 run "$SCRIPT" testprof \
    --import "$FAKE_APKG" \
    --ankiweb-user user@example.com \
    --ankiweb-pass-file "$pass_file"
  [ "$status" -eq 0 ]
  # Default-profile var is still the original value
  grep -q "^KRYSHANTI_ANKI_DEFAULT_PROFILE=_anki_skill_testrun$" "$ENV_FILE"
  # New scoped credentials were appended
  grep -q "^ANKIWEB_USERNAME_testprof=user@example.com$" "$ENV_FILE"
  grep -q "^ANKIWEB_PASSWORD_testprof=secret$" "$ENV_FILE"
}
