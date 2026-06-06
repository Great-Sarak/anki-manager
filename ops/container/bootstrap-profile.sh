#!/usr/bin/env bash
#
# bootstrap-profile.sh — create a new Anki profile inside the kryshanti-anki
# container and seed it from a .colpkg backup.
#
# Run as root (sudo). Run once per user-collection profile being onboarded.
#
# What it does:
#   1. Stops the kryshanti-anki systemd unit.
#   2. Flips KRYSHANTI_ANKI_DEFAULT_PROFILE in /var/lib/kryshanti-anki/anki.env
#      to the new profile name so the next container start creates and loads it.
#   3. Starts the unit; waits for AnkiConnect to come up.
#   4. Copies the .colpkg into a bind-mount-accessible temp dir
#      (/var/lib/kryshanti-anki/data/_import/) and calls AnkiConnect's
#      importPackage action.
#   5. Verifies the import landed (deckNames returns a non-trivial list).
#   6. Removes the temp .colpkg.
#   7. Optionally writes ANKIWEB_USERNAME_<profile> / ANKIWEB_PASSWORD_<profile>
#      into anki.env (prompts for password or reads from --ankiweb-pass-file).
#   8. Restarts the unit so SeedLogin picks up the new credentials on next
#      profile load.
#
# The .colpkg is NEVER stored in the repo. Always supplied at runtime.

set -euo pipefail

# --- configuration -------------------------------------------------------- #

STATE_DIR="/var/lib/kryshanti-anki"
DATA_DIR="$STATE_DIR/data"
ENV_FILE="$STATE_DIR/anki.env"
IMPORT_DIR="$DATA_DIR/_import"
UNIT_NAME="kryshanti-anki.service"
ANKICONNECT_URL="http://127.0.0.1:8765"
ANKICONNECT_WAIT_MAX=60   # seconds

# --- arg parsing ---------------------------------------------------------- #

usage() {
  cat <<'USAGE' >&2
Usage:
  bootstrap-profile.sh <profile-name>
    --import <colpkg-path>
    [--ankiweb-user <email>]
    [--ankiweb-pass-file <path>]
    [--force]
    [--skip-credentials]

Required:
  <profile-name>             Name of the Anki profile to create (e.g. sorotassu).
  --import <path>            Path to the .colpkg backup to import.

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
      --import ~/assets/all_decks.colpkg \
      --ankiweb-user sorotassu@example.com

  sudo ./bootstrap-profile.sh sorotassu \
      --import ~/assets/all_decks.colpkg --skip-credentials
USAGE
  exit 1
}

PROFILE=""
COLPKG_PATH=""
ANKIWEB_USER=""
ANKIWEB_PASS_FILE=""
FORCE=0
SKIP_CREDS=0

while [[ $# -gt 0 ]]; do
  case "$1" in
    --import)              COLPKG_PATH="${2:-}"; shift 2 ;;
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
[[ -z "$COLPKG_PATH" ]]  && { echo "Missing --import <colpkg-path>." >&2; usage; }

# --- preflight ------------------------------------------------------------ #

if [[ $EUID -ne 0 ]]; then
  echo "Must run as root (sudo)." >&2
  exit 1
fi

if [[ ! -f "$COLPKG_PATH" ]]; then
  echo "Colpkg not found: $COLPKG_PATH" >&2
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

# Reject reserved profile prefixes that might collide with system dirs.
case "$PROFILE" in
  addons21|.*|_import) echo "Reserved profile name: $PROFILE" >&2; exit 1 ;;
esac

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
echo "  colpkg:       $COLPKG_PATH"
echo "  ankiweb user: ${ANKIWEB_USER:-<skipped>}"
echo

# --- 1. stop unit --------------------------------------------------------- #

echo "[1/7] Stopping $UNIT_NAME (if running)..."
systemctl stop "$UNIT_NAME" 2>/dev/null || true

# --- 2. flip default profile in anki.env --------------------------------- #

echo "[2/7] Setting KRYSHANTI_ANKI_DEFAULT_PROFILE=$PROFILE in $ENV_FILE..."
if [[ ! -f "$ENV_FILE" ]]; then
  install -m 600 /dev/null "$ENV_FILE"
fi

# Idempotent set: replace existing line or append.
if grep -q "^KRYSHANTI_ANKI_DEFAULT_PROFILE=" "$ENV_FILE"; then
  sed -i -E "s|^KRYSHANTI_ANKI_DEFAULT_PROFILE=.*|KRYSHANTI_ANKI_DEFAULT_PROFILE=$PROFILE|" "$ENV_FILE"
else
  echo "KRYSHANTI_ANKI_DEFAULT_PROFILE=$PROFILE" >> "$ENV_FILE"
fi

# --- 3. start unit + wait for AnkiConnect --------------------------------- #

echo "[3/7] Starting $UNIT_NAME..."
systemctl start "$UNIT_NAME"

echo "      Waiting up to ${ANKICONNECT_WAIT_MAX}s for AnkiConnect on $ANKICONNECT_URL..."
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
  echo "      AnkiConnect didn't come up. Check 'journalctl -u $UNIT_NAME'." >&2
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

# --- 4. copy colpkg into bind mount + importPackage ----------------------- #

echo "[4/7] Importing $COLPKG_PATH..."
mkdir -p "$IMPORT_DIR"
COLPKG_NAME="$(basename "$COLPKG_PATH")"
COLPKG_DST="$IMPORT_DIR/$COLPKG_NAME"
cp "$COLPKG_PATH" "$COLPKG_DST"
chown -R "$(stat -c '%U:%G' "$STATE_DIR")" "$IMPORT_DIR"

# AnkiConnect sees /data/_import/<name> inside the container.
CONTAINER_COLPKG_PATH="/data/_import/$COLPKG_NAME"

# importPackage in AnkiConnect 25.x can take minutes for a large collection.
echo "      Calling importPackage (may take several minutes for large collections)..."
IMPORT_RESULT=$(curl -fsS --max-time 600 -X POST "$ANKICONNECT_URL" \
  -d "$(python3 -c "
import json
print(json.dumps({
    'action': 'importPackage',
    'version': 6,
    'params': {'path': '$CONTAINER_COLPKG_PATH'},
}))
")")

IMPORT_ERROR=$(echo "$IMPORT_RESULT" | python3 -c 'import json,sys; print(json.load(sys.stdin).get("error") or "")')
if [[ -n "$IMPORT_ERROR" ]]; then
  echo "      importPackage failed: $IMPORT_ERROR" >&2
  exit 1
fi
echo "      Import returned: $IMPORT_RESULT"

# --- 5. verify decks loaded ----------------------------------------------- #

echo "[5/7] Verifying decks loaded..."
DECKS=$(curl -fsS -X POST "$ANKICONNECT_URL" \
  -d '{"action":"deckNames","version":6}' \
  | python3 -c 'import json,sys; print(len(json.load(sys.stdin)["result"]))')

if [[ "$DECKS" -lt 2 ]]; then
  # Default profile has only "Default" deck.
  echo "      Only $DECKS deck(s) found after import; expected >1." >&2
  echo "      Check /data/seedlogin.log and Anki logs in the container." >&2
  exit 1
fi
echo "      $DECKS decks present."

# --- 6. clean up temp colpkg --------------------------------------------- #

echo "[6/7] Removing temp $COLPKG_DST..."
rm -f "$COLPKG_DST"
rmdir "$IMPORT_DIR" 2>/dev/null || true

# --- 7. credentials + restart -------------------------------------------- #

if [[ $SKIP_CREDS -ne 1 && -n "$ANKIWEB_USER" ]]; then
  echo "[7/7] Writing per-profile AnkiWeb credentials to $ENV_FILE..."
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

  echo "      Restarting $UNIT_NAME so SeedLogin can register the new account..."
  systemctl restart "$UNIT_NAME"
  # Wait for AnkiConnect to come back.
  deadline=$(( $(date +%s) + ANKICONNECT_WAIT_MAX ))
  while (( $(date +%s) < deadline )); do
    if curl -fsS -o /dev/null --max-time 2 -X POST "$ANKICONNECT_URL" \
          -d '{"action":"version","version":6}' 2>/dev/null; then
      break
    fi
    sleep 1
  done
fi

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
