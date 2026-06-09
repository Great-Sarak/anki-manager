#!/usr/bin/env bash
#
# bootstrap-profile.sh — create a new Anki profile inside the kryshanti-anki
# container and seed it from an .apkg deck export.
#
# Run as root (sudo). Run once per user-collection profile being onboarded.
#
# Accepted import format: .apkg only.
#
# .colpkg (full-collection backup) is NOT accepted; see
# https://github.com/Great-Sarak/anki-manager/issues/19 for the reasons
# (the AnkiConnect handler model is fundamentally async-shape-mismatched
# with .colpkg's collection-replacement lifecycle). To use a .colpkg
# backup as the source: open it in Anki Desktop on the source machine,
# then File > Export > "Anki Deck Package (.apkg)" with "Include All
# Decks" checked, and pass the resulting .apkg here.
#
# What it does:
#   1. Stops the persistent kryshanti-anki systemd unit.
#   2. Launches a TRANSIENT bootstrap container against the same data volume,
#      with KRYSHANTI_ANKI_DEFAULT_PROFILE=<target> set as a docker-run env
#      override. This loads the target profile WITHOUT modifying anki.env's
#      persisted default — `docker run -e VAR=value` wins over `--env-file`'s
#      value of the same key for that container instance only.
#   3. Waits for AnkiConnect; verifies the target profile is loaded.
#   4. Copies the .apkg into the bind-mount-accessible temp dir
#      (/var/lib/kryshanti-anki/data/_import/) and calls AnkiConnect's
#      importPackageWithLog action (added by patches/0003-fix-importPackage-anki25.patch).
#   5. Verifies the import landed via the rich log's deck_count_after.
#   6. Removes the temp .apkg.
#   7. Optionally writes ANKIWEB_USERNAME_<profile> / ANKIWEB_PASSWORD_<profile>
#      into anki.env. These are scoped credential vars, distinct from the
#      default-profile var — adding them does NOT change which profile loads
#      by default; it just gives SeedLogin the per-profile creds to use when
#      the target profile is subsequently loaded.
#   8. Stops the transient bootstrap container.
#   9. Starts the persistent kryshanti-anki systemd unit (which uses anki.env's
#      UNCHANGED KRYSHANTI_ANKI_DEFAULT_PROFILE — i.e. whatever was the default
#      before this script ran).
#
# Steady-state property: after the script exits (success OR failure),
# anki.env's KRYSHANTI_ANKI_DEFAULT_PROFILE value is exactly what it was
# before the script ran. The target profile is created and populated on disk
# regardless. The persistent unit comes back up on its pre-bootstrap default
# profile.
#
# The .apkg is NEVER stored in the repo. Always supplied at runtime.

set -euo pipefail

# --- configuration -------------------------------------------------------- #

STATE_DIR="/var/lib/kryshanti-anki"
DATA_DIR="$STATE_DIR/data"
ENV_FILE="$STATE_DIR/anki.env"
IMPORT_DIR="$DATA_DIR/_import"
UNIT_NAME="kryshanti-anki.service"
ANKICONNECT_URL="http://127.0.0.1:8765"
ANKICONNECT_WAIT_MAX=60                          # seconds
IMAGE_TAG="kryshanti-anki:25.02.7"
BOOTSTRAP_CONTAINER_NAME="kryshanti-anki-bootstrap"

# --- arg parsing ---------------------------------------------------------- #

usage() {
  cat <<'USAGE' >&2
Usage:
  bootstrap-profile.sh <profile-name>
    --import <apkg-path>
    [--ankiweb-user <email>]
    [--ankiweb-pass-file <path>]
    [--force]
    [--skip-credentials]

Required:
  <profile-name>             Name of the Anki profile to create (e.g. sorotassu).
  --import <path>            Path to the .apkg deck export to import.
                             .colpkg is NOT accepted; see
                             github.com/Great-Sarak/anki-manager/issues/19
                             for the workflow to convert .colpkg -> .apkg.

Optional:
  --ankiweb-user <email>     AnkiWeb username. If set, you'll be prompted for
                             the password (or supply --ankiweb-pass-file).
  --ankiweb-pass-file <path> File containing the AnkiWeb password, single line.
                             Must be chmod 600. Avoid passing the password on
                             the command line directly (it would leak via ps).
  --force                    Overwrite an existing profile dir. DESTRUCTIVE.
  --skip-credentials         Don't touch anki.env credential vars. Use if
                             credentials will be set out-of-band.

Examples:
  sudo ./bootstrap-profile.sh sorotassu \
      --import ~/assets/all_decks.apkg \
      --ankiweb-user sorotassu@example.com

  sudo ./bootstrap-profile.sh sorotassu \
      --import ~/assets/all_decks.apkg --skip-credentials
USAGE
  exit 1
}

PROFILE=""
IMPORT_PATH=""
ANKIWEB_USER=""
ANKIWEB_PASS_FILE=""
FORCE=0
SKIP_CREDS=0

while [[ $# -gt 0 ]]; do
  case "$1" in
    --import)              IMPORT_PATH="${2:-}"; shift 2 ;;
    --ankiweb-user)        ANKIWEB_USER="${2:-}"; shift 2 ;;
    --ankiweb-pass-file)   ANKIWEB_PASS_FILE="${2:-}"; shift 2 ;;
    --force)               FORCE=1; shift ;;
    --skip-credentials)    SKIP_CREDS=1; shift ;;
    -h|--help)             usage ;;
    -*)                    echo "Unknown flag: $1" >&2; usage ;;
    *)
      if [[ -z "$PROFILE" ]]; then
        PROFILE="$1"
        shift
      else
        echo "Unexpected positional arg: $1" >&2
        usage
      fi
      ;;
  esac
done

[[ -z "$PROFILE" ]]      && { echo "Missing <profile-name>." >&2; usage; }
[[ -z "$IMPORT_PATH" ]]  && { echo "Missing --import <apkg-path>." >&2; usage; }

# --- input validation (before privilege escalation check) ----------------- #

# Reserved profile prefixes that would collide with system dirs.
case "$PROFILE" in
  addons21|.*|_import) echo "Reserved profile name: $PROFILE" >&2; exit 1 ;;
esac

if [[ ! -f "$IMPORT_PATH" ]]; then
  echo "Import file not found: $IMPORT_PATH" >&2
  exit 1
fi

# File-type validation. .colpkg is explicitly rejected with the workflow
# to convert it to .apkg, since AnkiConnect's import surface doesn't
# cleanly handle .colpkg in headless containers (see issue #19).
import_lower="${IMPORT_PATH,,}"
case "$import_lower" in
  *.apkg)
    : # accepted
    ;;
  *.colpkg)
    cat >&2 <<COLPKG_REJECT
ERROR: .colpkg is not supported by this script.
       Path: $IMPORT_PATH

Background: AnkiConnect's importPackage action handles .apkg only.
.colpkg uses a collection-replacement lifecycle (async, dialog-driven)
that can't be cleanly driven through the synchronous AnkiConnect
handler model. See Great-Sarak/anki-manager#19 for the full
investigation and the three dead ends we explored.

To convert .colpkg -> .apkg:
  1. Open Anki Desktop on the source machine.
  2. File > Export...
  3. Export format: "Anki Deck Package (.apkg)"
  4. Check "Include All Decks".
  5. Save the .apkg, then re-run this script with --import <that-path>.

If you actually need .colpkg support (i.e. you want collection-level
state including review history that .apkg drops), Great-Sarak/anki-manager#19
tracks the upstream AnkiConnect PR design that would be required.
COLPKG_REJECT
    exit 2
    ;;
  *)
    echo "ERROR: Unrecognized import file extension. Expected .apkg." >&2
    echo "       Path: $IMPORT_PATH" >&2
    exit 2
    ;;
esac

# --- preflight (privileged / state-dependent) ----------------------------- #

if [[ $EUID -ne 0 ]]; then
  echo "Must run as root (sudo)." >&2
  exit 1
fi

if [[ ! -d "$STATE_DIR" ]]; then
  echo "State dir $STATE_DIR missing. Run host-setup.sh first." >&2
  exit 1
fi

PROFILE_DIR="$DATA_DIR/$PROFILE"
if [[ -d "$PROFILE_DIR" ]] && [[ -n "$(ls -A "$PROFILE_DIR" 2>/dev/null)" ]]; then
  if [[ $FORCE -ne 1 ]]; then
    echo "Profile dir $PROFILE_DIR already exists and is non-empty." >&2
    echo "Re-run with --force to overwrite (destructive)." >&2
    exit 1
  fi
  echo "[!] --force: removing existing $PROFILE_DIR"
  rm -rf "$PROFILE_DIR"
fi

# Resolve AnkiWeb credentials if requested.
ANKIWEB_PASS=""
if [[ $SKIP_CREDS -ne 1 && -n "$ANKIWEB_USER" ]]; then
  if [[ -n "$ANKIWEB_PASS_FILE" ]]; then
    if [[ ! -f "$ANKIWEB_PASS_FILE" ]]; then
      echo "Pass file not found: $ANKIWEB_PASS_FILE" >&2; exit 1
    fi
    MODE=$(stat -c '%a' "$ANKIWEB_PASS_FILE")
    if [[ "$MODE" != "600" && "$MODE" != "400" ]]; then
      echo "Pass file $ANKIWEB_PASS_FILE must be chmod 600 (or 400); got $MODE." >&2
      exit 1
    fi
    ANKIWEB_PASS=$(head -n1 "$ANKIWEB_PASS_FILE")
  else
    read -r -s -p "AnkiWeb password for $ANKIWEB_USER: " ANKIWEB_PASS
    echo
  fi
  if [[ -z "$ANKIWEB_PASS" ]]; then
    echo "Empty password." >&2; exit 1
  fi
fi

echo "=== bootstrap-profile.sh ==="
echo "  profile:      $PROFILE"
echo "  import:       $IMPORT_PATH"
echo "  ankiweb user: ${ANKIWEB_USER:-<skipped>}"
echo "  pre-bootstrap default profile: $(grep -E '^KRYSHANTI_ANKI_DEFAULT_PROFILE=' "$ENV_FILE" 2>/dev/null | sed -E 's/^KRYSHANTI_ANKI_DEFAULT_PROFILE=//' || echo '<unset>')"
echo

# Trap: if the script fails mid-flight, ensure the transient container is
# stopped (it's --rm so removal is automatic on stop) and the persistent
# unit is brought back up. anki.env is never modified by this script
# regardless of exit path.
SCRIPT_SUCCESS=0
on_exit() {
  local rc=$?
  if [[ $SCRIPT_SUCCESS -eq 1 ]]; then
    return 0
  fi
  echo "" >&2
  echo "FAILURE (exit $rc): cleaning up transient bootstrap container + restoring persistent unit." >&2
  docker stop "$BOOTSTRAP_CONTAINER_NAME" 2>/dev/null || true
  systemctl start "$UNIT_NAME" 2>/dev/null || true
}
trap on_exit EXIT

# --- 1. stop the persistent kryshanti-anki unit -------------------------- #

echo "[1/9] Stopping $UNIT_NAME (releases port 8765 + data lock)..."
systemctl stop "$UNIT_NAME" 2>/dev/null || true

# --- 2. launch transient bootstrap container with env override ----------- #

echo "[2/9] Launching transient '$BOOTSTRAP_CONTAINER_NAME' with"
echo "      KRYSHANTI_ANKI_DEFAULT_PROFILE=$PROFILE (docker-run -e overrides --env-file)..."
# docker run's -e wins over --env-file for the same key. anki.env is read for
# everything else (AnkiWeb creds, etc.); KRYSHANTI_ANKI_DEFAULT_PROFILE is
# overridden just for this container's lifetime. anki.env on disk is unchanged.
docker run --rm -d \
    --name "$BOOTSTRAP_CONTAINER_NAME" \
    --env-file "$ENV_FILE" \
    -e "KRYSHANTI_ANKI_DEFAULT_PROFILE=$PROFILE" \
    -p 127.0.0.1:8765:8765 \
    -v "$DATA_DIR:/data" \
    "$IMAGE_TAG" >/dev/null

# --- 3. wait + verify the target profile is loaded ----------------------- #

echo "[3/9] Waiting up to ${ANKICONNECT_WAIT_MAX}s for AnkiConnect..."
deadline=$(( $(date +%s) + ANKICONNECT_WAIT_MAX ))
while (( $(date +%s) < deadline )); do
  if curl -fsS -o /dev/null --max-time 2 -X POST "$ANKICONNECT_URL" \
        -d '{"action":"version","version":6}' 2>/dev/null; then
    break
  fi
  sleep 1
done
if ! curl -fsS -o /dev/null --max-time 2 -X POST "$ANKICONNECT_URL" \
      -d '{"action":"version","version":6}' 2>/dev/null; then
  echo "      AnkiConnect didn't come up. docker logs $BOOTSTRAP_CONTAINER_NAME for details." >&2
  exit 1
fi

# Verify the right profile is loaded.
ACTIVE=$(curl -fsS -X POST "$ANKICONNECT_URL" \
            -d '{"action":"getActiveProfile","version":6}' \
         | python3 -c 'import json,sys; print(json.load(sys.stdin)["result"] or "")')
if [[ "$ACTIVE" != "$PROFILE" ]]; then
  echo "      Expected profile $PROFILE, got $ACTIVE. Aborting." >&2
  exit 1
fi
echo "      AnkiConnect up, profile=$ACTIVE."

# --- 4. copy apkg into bind mount + importPackageWithLog ------------------ #

echo "[4/9] Importing $IMPORT_PATH..."
mkdir -p "$IMPORT_DIR"
IMPORT_NAME="$(basename "$IMPORT_PATH")"
IMPORT_DST="$IMPORT_DIR/$IMPORT_NAME"
cp "$IMPORT_PATH" "$IMPORT_DST"
chown -R "$(stat -c '%U:%G' "$STATE_DIR")" "$IMPORT_DIR"

# AnkiConnect sees /data/_import/<name> inside the container.
CONTAINER_IMPORT_PATH="/data/_import/$IMPORT_NAME"

# importPackageWithLog (added in patches/0003-fix-importPackage-anki25.patch)
# returns a rich log summary so we can verify the import actually changed the
# collection. The upstream importPackage action returns just true/false and
# was silently no-op on Anki 25.x before the patch — see #19 for the full
# investigation.
echo "      Calling importPackageWithLog..."
IMPORT_RESULT=$(curl -fsS --max-time 600 -X POST "$ANKICONNECT_URL" \
  -d "$(python3 -c "
import json
print(json.dumps({
    'action': 'importPackageWithLog',
    'version': 6,
    'params': {'path': '$CONTAINER_IMPORT_PATH'},
}))
")")

IMPORT_ERROR=$(echo "$IMPORT_RESULT" | python3 -c 'import json,sys; print(json.load(sys.stdin).get("error") or "")')
if [[ -n "$IMPORT_ERROR" ]]; then
  echo "      importPackageWithLog failed: $IMPORT_ERROR" >&2
  exit 1
fi
echo "      Import log: $IMPORT_RESULT"

# --- 5. verify decks loaded ----------------------------------------------- #

echo "[5/9] Verifying decks loaded..."
# importPackageWithLog returns deck_count_after in its log; use it directly
# instead of a second deckNames call.
DECKS=$(echo "$IMPORT_RESULT" \
  | python3 -c 'import json,sys; print(json.load(sys.stdin)["result"]["deck_count_after"])')

if [[ "$DECKS" -lt 2 ]]; then
  # Default profile has only "Default" deck.
  echo "      Only $DECKS deck(s) found after import; expected >1." >&2
  echo "      Check Anki logs in the container." >&2
  exit 1
fi
echo "      $DECKS decks present."

# --- 6. clean up temp apkg ----------------------------------------------- #

echo "[6/9] Removing temp $IMPORT_DST..."
rm -f "$IMPORT_DST"
rmdir "$IMPORT_DIR" 2>/dev/null || true

# --- 7. write per-profile AnkiWeb credentials (optional) ----------------- #
# These are scoped vars (ANKIWEB_USERNAME_<profile> / _PASSWORD_<profile>),
# distinct from KRYSHANTI_ANKI_DEFAULT_PROFILE. Writing them does NOT change
# which profile loads by default; SeedLogin reads them when the target
# profile is subsequently loaded.

if [[ $SKIP_CREDS -ne 1 && -n "$ANKIWEB_USER" ]]; then
  echo "[7/9] Writing per-profile AnkiWeb credentials to $ENV_FILE..."
  USER_KEY="ANKIWEB_USERNAME_$PROFILE"
  PASS_KEY="ANKIWEB_PASSWORD_$PROFILE"

  # Idempotent: replace existing keys or append.
  if grep -q "^$USER_KEY=" "$ENV_FILE"; then
    sed -i -E "s|^$USER_KEY=.*|$USER_KEY=$ANKIWEB_USER|" "$ENV_FILE"
  else
    echo "$USER_KEY=$ANKIWEB_USER" >> "$ENV_FILE"
  fi
  # Write password via a python heredoc to avoid sed-metacharacter issues.
  ANKIWEB_PASS="$ANKIWEB_PASS" PASS_KEY="$PASS_KEY" ENV_FILE="$ENV_FILE" python3 <<'PY'
import os
key = os.environ["PASS_KEY"]
val = os.environ["ANKIWEB_PASS"]
path = os.environ["ENV_FILE"]
with open(path, "r", encoding="utf-8") as fh:
    lines = fh.readlines()
prefix = f"{key}="
new = f"{prefix}{val}\n"
for i, line in enumerate(lines):
    if line.startswith(prefix):
        lines[i] = new
        break
else:
    if lines and not lines[-1].endswith("\n"):
        lines[-1] += "\n"
    lines.append(new)
with open(path, "w", encoding="utf-8") as fh:
    fh.writelines(lines)
PY
  unset ANKIWEB_PASS
else
  echo "[7/9] Credentials skipped (no --ankiweb-user given or --skip-credentials set)."
fi

# --- 8. stop the transient bootstrap container --------------------------- #

echo "[8/9] Stopping transient '$BOOTSTRAP_CONTAINER_NAME'..."
docker stop "$BOOTSTRAP_CONTAINER_NAME" >/dev/null 2>&1 || true

# --- 9. start the persistent unit on its UNCHANGED default profile ------- #

echo "[9/9] Starting $UNIT_NAME (uses anki.env's pre-bootstrap default profile)..."
systemctl start "$UNIT_NAME"
deadline=$(( $(date +%s) + ANKICONNECT_WAIT_MAX ))
while (( $(date +%s) < deadline )); do
  if curl -fsS -o /dev/null --max-time 2 -X POST "$ANKICONNECT_URL" \
        -d '{"action":"version","version":6}' 2>/dev/null; then
    break
  fi
  sleep 1
done
if curl -fsS -o /dev/null --max-time 2 -X POST "$ANKICONNECT_URL" \
      -d '{"action":"version","version":6}' 2>/dev/null; then
  ACTIVE_AFTER=$(curl -fsS -X POST "$ANKICONNECT_URL" \
                    -d '{"action":"getActiveProfile","version":6}' \
                 | python3 -c 'import json,sys; print(json.load(sys.stdin)["result"] or "")')
  echo "      Persistent unit back up; active profile=${ACTIVE_AFTER:-<unknown>}."
else
  echo "      Warning: AnkiConnect didn't come back within ${ANKICONNECT_WAIT_MAX}s after persistent-unit start." >&2
  echo "      Bootstrap completed but the unit may need manual investigation." >&2
fi

SCRIPT_SUCCESS=1

# --- done ---------------------------------------------------------------- #

echo
echo "=== Bootstrap complete ==="
echo "  Profile: $PROFILE"
echo "  Decks:   $DECKS"
echo
echo "Next:"
if [[ $SKIP_CREDS -eq 1 || -z "$ANKIWEB_USER" ]]; then
  echo "  - Add AnkiWeb credentials to $ENV_FILE manually:"
  echo "      ANKIWEB_USERNAME_$PROFILE=<email>"
  echo "      ANKIWEB_PASSWORD_$PROFILE=<password>"
  echo "  - sudo systemctl restart $UNIT_NAME"
fi
echo "  - Sync will be a FULL_DOWNLOAD reconciliation against AnkiWeb on first run."
echo "  - Verify: curl -s -X POST $ANKICONNECT_URL -d '{\"action\":\"deckNames\",\"version\":6}'"
