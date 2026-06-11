#!/usr/bin/env bash
#
# anki-tools install — agent-independent /opt install (anki-manager #32).
#
# Run as root (sudo). Idempotent; safe to re-run.
#
# Builds /opt/anki-tools/{anki-rpc,anki-manager,anki-translator}, each in its own
# venv, pins all three to a git ref (default: the v0.1 tag), installs the CLI
# entrypoints onto /usr/local/bin, and creates /var/lib/anki-translator/ state.
#
# Companion to ops/container/host-setup.sh (the kryshanti-anki container). Run
# host-setup.sh FIRST — it creates the kryshanti-anki-users group this script
# chowns the translator state dir to.
#
# What it does NOT do (deliberately — see the Migration section of #32, do these
# by hand once the v0.1 smoke passes against the new install):
#   - move existing in-hut queue/ + qa/ state into /var/lib/anki-translator/
#   - delete the old *_hut/*/anki-{manager,translator}_main/.venv installs
#
# Usage:
#   sudo ./install-tools.sh [invoking-user]
#
# Env overrides:
#   ANKI_TOOLS_REF=v0.1                  git ref checked out for all three repos
#   ANKI_TOOLS_ROOT=/opt/anki-tools
#   ANKI_TRANSLATOR_STATE_DIR=/var/lib/anki-translator
#   GIT_BASE=https://github.com/Great-Sarak

set -euo pipefail

# --- configuration -------------------------------------------------------- #

ANKI_TOOLS_ROOT="${ANKI_TOOLS_ROOT:-/opt/anki-tools}"
TRANSLATOR_STATE_DIR="${ANKI_TRANSLATOR_STATE_DIR:-/var/lib/anki-translator}"
ACCESS_GROUP="kryshanti-anki-users"
REF="${ANKI_TOOLS_REF:-v0.1}"
GIT_BASE="${GIT_BASE:-https://github.com/Great-Sarak}"
# Install order == dependency order: anki-rpc is the Layer-1 dependency,
# anki-manager builds on it, anki-translator builds on both.
REPOS=(anki-rpc anki-manager anki-translator)

# --- preflight ------------------------------------------------------------ #

if [[ $EUID -ne 0 ]]; then
  echo "Must run as root (sudo)." >&2
  exit 1
fi

ACCESS_USER="${1:-${SUDO_USER:-}}"
if [[ -z "$ACCESS_USER" ]]; then
  echo "Couldn't determine the invoking user. Pass it explicitly: $0 <username>" >&2
  exit 1
fi
if ! id "$ACCESS_USER" &>/dev/null; then
  echo "User '$ACCESS_USER' doesn't exist." >&2
  exit 1
fi
command -v git     >/dev/null || { echo "git not found." >&2; exit 1; }
command -v python3 >/dev/null || { echo "python3 not found." >&2; exit 1; }

if ! getent group "$ACCESS_GROUP" >/dev/null; then
  echo "  WARNING: group $ACCESS_GROUP is missing — run ops/container/host-setup.sh"
  echo "           first. Translator state perms will fall back to root-owned."
fi

echo "=== anki-tools install ==="
echo "  install root:  $ANKI_TOOLS_ROOT"
echo "  state dir:     $TRANSLATOR_STATE_DIR"
echo "  git ref:       $REF"
echo "  user / group:  $ACCESS_USER / $ACCESS_GROUP"
echo

# --- 1. /opt tree --------------------------------------------------------- #

echo "[1/5] Creating $ANKI_TOOLS_ROOT..."
mkdir -p "$ANKI_TOOLS_ROOT"

# --- 2. clone / update each repo at $REF ---------------------------------- #

echo "[2/5] Cloning/updating repos at $REF..."
for repo in "${REPOS[@]}"; do
  dest="$ANKI_TOOLS_ROOT/$repo"
  if [[ -d "$dest/.git" ]]; then
    echo "      $repo: fetching..."
    git -C "$dest" fetch --quiet --tags origin
  else
    echo "      $repo: cloning..."
    git clone --quiet "$GIT_BASE/$repo.git" "$dest"
  fi
  git -C "$dest" checkout --quiet "$REF"
  echo "      $repo @ $REF ($(git -C "$dest" rev-parse --short HEAD))"
done

# --- 3. build venvs in dependency order ----------------------------------- #

echo "[3/5] Building venvs (editable, so sibling packages resolve)..."
build_venv() {  # $1=repo  $2..=extra editable sibling paths to install first
  local repo="$1"; shift
  local dir="$ANKI_TOOLS_ROOT/$repo"
  echo "      $repo: python -m venv + pip install -e ..."
  python3 -m venv "$dir/.venv"
  "$dir/.venv/bin/pip" install --quiet --upgrade pip
  local args=()
  local dep
  for dep in "$@"; do args+=(-e "$ANKI_TOOLS_ROOT/$dep"); done
  args+=(-e "$dir")
  "$dir/.venv/bin/pip" install --quiet "${args[@]}"
}
build_venv anki-rpc
build_venv anki-manager     anki-rpc
build_venv anki-translator  anki-rpc anki-manager

# --- 4. CLI entrypoints onto /usr/local/bin ------------------------------- #

echo "[4/5] Installing CLI entrypoints onto /usr/local/bin..."
# anki-manager: plain symlink — its state is the container's /var/lib/kryshanti-anki.
ln -sf "$ANKI_TOOLS_ROOT/anki-manager/.venv/bin/anki-manager" /usr/local/bin/anki-manager
echo "      anki-manager    -> $ANKI_TOOLS_ROOT/anki-manager/.venv/bin/anki-manager"
# anki-translator: a tiny wrapper so the /opt install defaults its state to
# $TRANSLATOR_STATE_DIR (still overridable by an explicit env), per #32 step 5.
cat > /usr/local/bin/anki-translator <<WRAP
#!/usr/bin/env bash
export ANKI_TRANSLATOR_STATE_DIR="\${ANKI_TRANSLATOR_STATE_DIR:-$TRANSLATOR_STATE_DIR}"
exec "$ANKI_TOOLS_ROOT/anki-translator/.venv/bin/anki-translator" "\$@"
WRAP
chmod 0755 /usr/local/bin/anki-translator
echo "      anki-translator -> wrapper (ANKI_TRANSLATOR_STATE_DIR=$TRANSLATOR_STATE_DIR) -> /opt venv"

# --- 5. translator state dir ---------------------------------------------- #

echo "[5/5] Creating $TRANSLATOR_STATE_DIR..."
mkdir -p "$TRANSLATOR_STATE_DIR"/{queue,qa,trimmed,committed}
if getent group "$ACCESS_GROUP" >/dev/null; then
  chgrp -R "$ACCESS_GROUP" "$TRANSLATOR_STATE_DIR"
  # group-writable + setgid so files created by any group member stay group-owned.
  chmod -R g+rwXs "$TRANSLATOR_STATE_DIR"
  echo "      owned by group $ACCESS_GROUP (group-writable, setgid)"
else
  echo "      left root-owned (group $ACCESS_GROUP absent)"
fi

echo
echo "=== done ==="
echo "  Verify (as a $ACCESS_GROUP member, no sudo):"
echo "      anki-manager --help"
echo "      anki-translator --help        # state defaults to $TRANSLATOR_STATE_DIR"
echo
echo "  Then run the v0.1 smoke (ops/container/README #17 runbook) against this"
echo "  install before migrating state out of the huts and deleting the hut venvs."
