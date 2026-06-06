#!/usr/bin/env bash
#
# kryshanti-anki host setup — privileged steps.
#
# Run as root (sudo). Idempotent; safe to re-run.
#
# What it does:
#   1. Creates system user  liora-mosspelt  (in docker group, no login).
#   2. Creates group        kryshanti-anki-users  and adds the invoking user to it.
#   3. Creates state dir    /var/lib/kryshanti-anki/{data,}  and migrates any
#                           seed data/ + .env adjacent to this script into it.
#                           Also creates writer.lock (group-rw to
#                           kryshanti-anki-users) for the cross-process write
#                           lock.
#   4. Builds image         kryshanti-anki:25.02.7  from this directory.
#   5. Installs systemd unit /etc/systemd/system/kryshanti-anki.service
#                           with  User=liora-mosspelt.
#   6. Installs polkit rule  /etc/polkit-1/rules.d/50-kryshanti-anki.rules
#                           that grants the kryshanti-anki-users group permission
#                           to start/stop/restart only this one unit, no password.
#   7. Installs grant-deck  /usr/local/libexec/kryshanti-anki/grant-deck
#                           (root-owned helper that the anki-manager skill
#                           shells out to via pkexec for allowlist edits).
#   8. Installs polkit rule /etc/polkit-1/rules.d/51-kryshanti-anki-grant.rules
#                           letting kryshanti-anki-users invoke grant-deck
#                           via pkexec without a password (helper still
#                           validates the request server-side).
#   9. Installs starter     /var/lib/kryshanti-anki/allowlist.toml
#                           with a Myrzka section claiming the invoking user
#                           and the <new> capability — only if not present.
#  10. Symlinks /usr/local/bin/anki-manager -> the discovered anki-manager
#                           venv binary (override with ANKI_MANAGER_BIN=...).
#                           Skipped silently if no venv found yet — re-run
#                           after `pip install -e .` to wire it up.
#
# After it finishes you can do (no sudo, no password):
#     systemctl start  kryshanti-anki
#     systemctl status kryshanti-anki
#     curl 127.0.0.1:8765 -d '{"action":"version","version":6}'
#
# Note: group membership only takes effect on the next login; if `systemctl start`
# still prompts for a password, log out + back in and try again.

set -euo pipefail

# --- configuration -------------------------------------------------------- #

ANKI_USER="liora-mosspelt"
ACCESS_GROUP="kryshanti-anki-users"
STATE_DIR="/var/lib/kryshanti-anki"
DATA_DIR="$STATE_DIR/data"
ENV_FILE="$STATE_DIR/anki.env"
IMAGE_TAG="kryshanti-anki:25.02.7"
CONTAINER_NAME="kryshanti-anki"
UNIT_NAME="kryshanti-anki.service"
POLKIT_RULE="/etc/polkit-1/rules.d/50-kryshanti-anki.rules"
POLKIT_RULE_GRANT="/etc/polkit-1/rules.d/51-kryshanti-anki-grant.rules"

ALLOWLIST_FILE="$STATE_DIR/allowlist.toml"
LOCK_FILE="$STATE_DIR/writer.lock"
HELPER_DIR="/usr/local/libexec/kryshanti-anki"
HELPER_PATH="$HELPER_DIR/grant-deck"

CONTAINER_SRC_DIR="$(cd "$(dirname "$0")" && pwd)"
SEED_DATA="$CONTAINER_SRC_DIR/data"
SEED_ENV="$CONTAINER_SRC_DIR/.env"
HELPER_SRC="$CONTAINER_SRC_DIR/grant-deck.py"

# --- preflight ------------------------------------------------------------ #

if [[ $EUID -ne 0 ]]; then
  echo "Must run as root (sudo)." >&2
  exit 1
fi

ACCESS_USER="${1:-${SUDO_USER:-$(logname 2>/dev/null || true)}}"
if [[ -z "$ACCESS_USER" ]]; then
  echo "Couldn't determine the invoking user." >&2
  echo "Pass it explicitly: $0 <username>" >&2
  exit 1
fi

if ! id "$ACCESS_USER" >/dev/null 2>&1; then
  echo "User '$ACCESS_USER' doesn't exist." >&2
  exit 1
fi

echo "=== kryshanti-anki host setup ==="
echo "  anki user:         $ANKI_USER"
echo "  access group:      $ACCESS_GROUP (adding: $ACCESS_USER)"
echo "  state dir:         $STATE_DIR"
echo "  image tag:         $IMAGE_TAG"
echo "  seed data source:  $SEED_DATA"
echo

# --- 1. anki user --------------------------------------------------------- #

if id "$ANKI_USER" >/dev/null 2>&1; then
  echo "[1/10] User $ANKI_USER exists; skipping."
else
  echo "[1/10] Creating system user $ANKI_USER..."
  useradd --system --shell /usr/sbin/nologin --no-create-home \
          --groups docker "$ANKI_USER"
fi

# --- 2. access group ------------------------------------------------------ #

if getent group "$ACCESS_GROUP" >/dev/null; then
  echo "[2/10] Group $ACCESS_GROUP exists."
else
  echo "[2/10] Creating group $ACCESS_GROUP..."
  groupadd "$ACCESS_GROUP"
fi

if id -nG "$ACCESS_USER" | tr ' ' '\n' | grep -qx "$ACCESS_GROUP"; then
  echo "      $ACCESS_USER is already in $ACCESS_GROUP."
else
  echo "      Adding $ACCESS_USER to $ACCESS_GROUP (effective next login)..."
  usermod -a -G "$ACCESS_GROUP" "$ACCESS_USER"
fi

# --- 3. state dir + data + env migration ---------------------------------- #

echo "[3/10] Setting up state dir $STATE_DIR..."
mkdir -p "$DATA_DIR"

if [[ -z "$(ls -A "$DATA_DIR" 2>/dev/null)" && -d "$SEED_DATA" ]]; then
  echo "      Migrating $SEED_DATA -> $DATA_DIR..."
  cp -a "$SEED_DATA/." "$DATA_DIR/"
elif [[ -n "$(ls -A "$DATA_DIR" 2>/dev/null)" ]]; then
  echo "      $DATA_DIR is non-empty; not overwriting."
fi

# Legacy profile rename: "User 1" -> "_anki_skill_testrun".
# Only renames if (a) the legacy dir exists, (b) the new dir does NOT exist.
# Skipping cases preserve any in-progress manual migration.
if [[ -d "$DATA_DIR/User 1" && ! -d "$DATA_DIR/_anki_skill_testrun" ]]; then
  echo "      Renaming legacy profile 'User 1' -> '_anki_skill_testrun'..."
  mv "$DATA_DIR/User 1" "$DATA_DIR/_anki_skill_testrun"
fi

if [[ -f "$ENV_FILE" ]]; then
  echo "      $ENV_FILE already exists; not overwriting."
elif [[ -f "$SEED_ENV" ]]; then
  echo "      Migrating $SEED_ENV -> $ENV_FILE..."
  install -m 600 "$SEED_ENV" "$ENV_FILE"
else
  echo "      WARNING: no seed .env to migrate. Create $ENV_FILE manually:"
  echo "        # Default profile (omit to use _anki_skill_testrun):"
  echo "        echo 'KRYSHANTI_ANKI_DEFAULT_PROFILE=_anki_skill_testrun' | sudo tee $ENV_FILE"
  echo "        # Per-profile credentials (preferred):"
  echo "        echo 'ANKIWEB_USERNAME__anki_skill_testrun=...' | sudo tee -a $ENV_FILE"
  echo "        echo 'ANKIWEB_PASSWORD__anki_skill_testrun=...' | sudo tee -a $ENV_FILE"
  echo "        # Legacy unscoped ANKIWEB_USERNAME/PASSWORD form is still honored"
  echo "        # for one release cycle as a fallback."
  echo "        sudo chmod 600 $ENV_FILE; sudo chown $ANKI_USER:$ANKI_USER $ENV_FILE"
fi

chown -R "$ANKI_USER:$ANKI_USER" "$STATE_DIR"

# Cross-process writer lock — group-rw to kryshanti-anki-users so all
# agents that can talk to AnkiConnect can also serialise writes.
if [[ ! -f "$LOCK_FILE" ]]; then
  echo "      Creating $LOCK_FILE..."
  install -m 0664 -o root -g "$ACCESS_GROUP" /dev/null "$LOCK_FILE"
fi
chown root:"$ACCESS_GROUP" "$LOCK_FILE"
chmod 0664 "$LOCK_FILE"

# --- 4. image build ------------------------------------------------------- #

echo "[4/10] Building image $IMAGE_TAG from $CONTAINER_SRC_DIR..."
docker build -t "$IMAGE_TAG" "$CONTAINER_SRC_DIR"

# --- 5. systemd unit ------------------------------------------------------ #

echo "[5/10] Installing systemd unit /etc/systemd/system/$UNIT_NAME..."
cat >/etc/systemd/system/"$UNIT_NAME" <<EOF
[Unit]
Description=kryshanti Anki Desktop (headless + AnkiConnect)
After=docker.service
Requires=docker.service

[Service]
Type=simple
User=$ANKI_USER
Group=$ANKI_USER
EnvironmentFile=$ENV_FILE
ExecStartPre=-/usr/bin/docker rm -f $CONTAINER_NAME
ExecStart=/usr/bin/docker run --rm --name $CONTAINER_NAME \\
    --env-file $ENV_FILE \\
    -p 127.0.0.1:8765:8765 \\
    -v $DATA_DIR:/data \\
    $IMAGE_TAG
ExecStop=/usr/bin/docker stop $CONTAINER_NAME
Restart=on-failure
RestartSec=5

[Install]
WantedBy=multi-user.target
EOF

systemctl daemon-reload

# --- 6. polkit rule ------------------------------------------------------- #

echo "[6/10] Installing polkit rule $POLKIT_RULE..."
cat >"$POLKIT_RULE" <<EOF
// Grants members of $ACCESS_GROUP the right to manage the kryshanti-anki
// systemd unit (start/stop/restart/status) without a password.
polkit.addRule(function(action, subject) {
    if (action.id == "org.freedesktop.systemd1.manage-units" &&
        action.lookup("unit") == "$UNIT_NAME" &&
        subject.isInGroup("$ACCESS_GROUP")) {
        return polkit.Result.YES;
    }
});
EOF

# --- 7. grant-deck helper ------------------------------------------------- #

echo "[7/10] Installing grant-deck helper at $HELPER_PATH..."
if [[ ! -f "$HELPER_SRC" ]]; then
  echo "      WARNING: helper source $HELPER_SRC not found; skipping." >&2
else
  mkdir -p "$HELPER_DIR"
  install -m 0755 -o root -g root "$HELPER_SRC" "$HELPER_PATH"
fi

# --- 8. polkit rule for grant-deck ---------------------------------------- #

echo "[8/10] Installing polkit rule $POLKIT_RULE_GRANT..."
cat >"$POLKIT_RULE_GRANT" <<EOF
// Grants members of $ACCESS_GROUP the right to run the kryshanti-anki
// grant-deck helper via pkexec without a password. The helper validates
// the request server-side (pattern syntax, invoker identity, section
// authority); polkit only enforces that the invoker is in the group.
polkit.addRule(function(action, subject) {
    if (action.id == "org.freedesktop.policykit.exec" &&
        action.lookup("program") == "$HELPER_PATH" &&
        subject.isInGroup("$ACCESS_GROUP")) {
        return polkit.Result.YES;
    }
});
EOF

# --- 9. starter allowlist ------------------------------------------------- #

echo "[9/10] Installing starter allowlist $ALLOWLIST_FILE..."
if [[ -f "$ALLOWLIST_FILE" ]]; then
  echo "      Already exists; not overwriting."
else
  cat >"$ALLOWLIST_FILE" <<EOF
# anki-manager allowlist
#
# Schema:
#   universal      patterns applied to every agent
#   [<AgentName>]  per-agent rules
#     allowed      patterns (fnmatch-style; * matches any sequence incl ::)
#                  the literal "<new>" entry grants this agent the right to
#                  auto-extend its own list when add-deck is called with a
#                  deck name not yet matched by any pattern (the grant-deck
#                  helper appends the new deck name as a fresh pattern).
#     aliases      Linux usernames that resolve to this agent.
#
# To inspect:   anki-manager permissions show
# To mutate:    anki-manager permissions add | remove | grant-new | revoke-new
# Direct edits also work; subsequent helper calls will round-trip the file.

universal = []

[Myrzka]
allowed = [
    "Myrzka::*",
    "Myrzka",
    "<new>",
]
aliases = [
    "$ACCESS_USER",
]
EOF
  chown root:"$ACCESS_GROUP" "$ALLOWLIST_FILE"
  chmod 0640 "$ALLOWLIST_FILE"   # root-writable, group-readable, others-no-access
fi

# --- 10. anki-manager CLI symlink ----------------------------------------- #

echo "[10/10] Linking anki-manager CLI onto /usr/local/bin..."
ANKI_MANAGER_BIN="${ANKI_MANAGER_BIN:-}"
if [[ -z "$ANKI_MANAGER_BIN" ]]; then
  # Auto-detect: look in any *_hut sibling for an anki-manager venv.
  shopt -s nullglob
  for candidate in \
      /media/sorotassu/great_sarak/*/anki-manager_main/.venv/bin/anki-manager \
      /opt/anki-manager/.venv/bin/anki-manager; do
    if [[ -x "$candidate" ]]; then
      ANKI_MANAGER_BIN="$candidate"
      break
    fi
  done
  shopt -u nullglob
fi

if [[ -z "$ANKI_MANAGER_BIN" || ! -x "$ANKI_MANAGER_BIN" ]]; then
  echo "      Skipping: no anki-manager venv found."
  echo "      Override with:  ANKI_MANAGER_BIN=/path/to/.venv/bin/anki-manager sudo $0"
else
  ln -sf "$ANKI_MANAGER_BIN" /usr/local/bin/anki-manager
  echo "      Linked /usr/local/bin/anki-manager -> $ANKI_MANAGER_BIN"
  echo "      (If the source venv is deleted, re-run this script with"
  echo "       ANKI_MANAGER_BIN pointing at another agent's venv.)"
fi

# polkit picks up new rule files automatically on most distros, but a reload
# is safe and explicit.
systemctl reload polkit 2>/dev/null || \
    systemctl restart polkit 2>/dev/null || \
    echo "      (couldn't reload polkit; new rule takes effect on next polkit start)"

# --- done ----------------------------------------------------------------- #

echo
echo "=== Setup complete ==="
echo
echo "Verify (as $ACCESS_USER; may need fresh login for group membership):"
echo
echo "  # Stop any prior dev-run container if it's still around:"
echo "  docker stop anki-spike 2>/dev/null || true"
echo
echo "  # Start the managed unit (should NOT prompt for password):"
echo "  systemctl start  $UNIT_NAME"
echo "  systemctl status $UNIT_NAME --no-pager"
echo
echo "  # Wait a few seconds for collection load, then probe:"
echo "  sleep 10 && curl -s 127.0.0.1:8765 \\"
echo "    -d '{\"action\":\"version\",\"version\":6}'"
echo
echo "If 'systemctl start' prompts for a password, the polkit rule isn't"
echo "applying. Check:"
echo "  - id $ACCESS_USER  # should show $ACCESS_GROUP in the group list"
echo "  - You may need to log out + back in for group membership."
