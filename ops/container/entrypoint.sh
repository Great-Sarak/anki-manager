#!/usr/bin/env bash
set -e

ANKI_BASE="${ANKI_BASE:-/data}"
ANKI_PROFILE="${KRYSHANTI_ANKI_DEFAULT_PROFILE:-_anki_skill_testrun}"

# Stage AnkiConnect addon into ANKI_BASE if missing (so a bind-mounted /data
# still gets the addon on first run, without shadowing user state on later
# runs).
ADDON_DST="${ANKI_BASE}/addons21/AnkiConnect"
ADDON_SRC="/opt/anki-addon-src/AnkiConnect"
mkdir -p "${ANKI_BASE}/addons21"
# Always refresh the addon code + config from the image-baked source on every
# start (idempotent; image rebuild = addon upgrade). User collection data
# under /data/${ANKI_PROFILE}/ is untouched.
rm -rf "${ADDON_DST}"
mkdir -p "${ADDON_DST}"
cp -r "${ADDON_SRC}/." "${ADDON_DST}/"
# Refresh runtime AnkiConnect config from the build-context copy (which sits
# next to the Dockerfile) — overrides any defaults from the addon repo
if [ -f /opt/anki-addon-runtime-config.json ]; then
  cp /opt/anki-addon-runtime-config.json "${ADDON_DST}/config.json"
fi
rm -rf "${ADDON_DST}/__pycache__"

# Stage SeedLogin (one-shot AnkiWeb credential bootstrap). Same refresh
# semantics — image-baked source wins on every start. Idempotency lives
# inside the addon itself (no-op if syncKey is already set on the profile).
SEED_DST="${ANKI_BASE}/addons21/SeedLogin"
SEED_SRC="/opt/seedlogin-src"
if [ -d "${SEED_SRC}" ]; then
  rm -rf "${SEED_DST}"
  mkdir -p "${SEED_DST}"
  cp -r "${SEED_SRC}/." "${SEED_DST}/"
  rm -rf "${SEED_DST}/__pycache__"
fi

# Clean any stale Xvfb lock from a previous (ungraceful) container exit
rm -f /tmp/.X99-lock /tmp/.X11-unix/X99 2>/dev/null || true

# Start Xvfb on display :99 (headless framebuffer for Anki Qt6 GUI)
Xvfb -ac -screen 0 1280x1024x24 :99 &
XVFB_PID=$!
export DISPLAY=:99

# Wait briefly for Xvfb to be ready
for i in $(seq 1 30); do
  if xdpyinfo -display :99 >/dev/null 2>&1; then
    break
  fi
  sleep 0.1
done

# Trap signals so we can exit cleanly when the container is stopped
trap 'kill -TERM "$XVFB_PID" 2>/dev/null; exit 0' TERM INT

# Pre-create the resolved profile directory. Anki Desktop's `-p <name>`
# does NOT create a missing profile: when prefs21.db has other profiles
# registered, Anki silently falls back to the first existing one
# (typically "User 1") instead of creating the requested one. Creating
# the dir is necessary for Anki to write collection state into it, but
# not sufficient on its own (see profile registration below).
mkdir -p "${ANKI_BASE}/${ANKI_PROFILE}"

# Register the profile in prefs21.db if it isn't already.
#
# Anki's ProfileManager tracks profiles as rows in prefs21.db.profiles
# (one row per profile, columns: name, data) where `data` is a pickled
# Python dict of profile settings. If the target profile isn't in this
# table when Anki starts with `-p <name>`, Anki silently loads whichever
# profile IS registered first instead — without warning, without log.
#
# We register the target profile by cloning any existing user profile's
# dict as a template and stripping out sync state (so SeedLogin can
# populate fresh credentials). Edge cases handled inline:
#   - prefs21.db missing → skip (first launch; Anki creates DB + profile
#     itself when there are zero registered profiles to fall back to)
#   - profile already registered → skip
#   - no template profile to clone → skip (no fallback exists; Anki
#     creates the requested profile)
python3 - "${ANKI_BASE}" "${ANKI_PROFILE}" <<'PYEOF'
import os
import pickle
import sqlite3
import sys

base, profile = sys.argv[1], sys.argv[2]
db = os.path.join(base, "prefs21.db")

if not os.path.exists(db):
    sys.exit(0)

con = sqlite3.connect(db, isolation_level=None)
try:
    cur = con.cursor()
    tables = {r[0] for r in cur.execute(
        "SELECT name FROM sqlite_master WHERE type='table'"
    )}
    if "profiles" not in tables:
        sys.exit(0)
    if cur.execute(
        "SELECT 1 FROM profiles WHERE name=?", (profile,)
    ).fetchone():
        sys.exit(0)
    templ_row = cur.execute(
        "SELECT data FROM profiles WHERE name != '_global' LIMIT 1"
    ).fetchone()
    if not templ_row:
        sys.exit(0)
    d = pickle.loads(templ_row[0])
    for key in ("syncKey", "syncUser", "currentSyncUrl", "hostNum"):
        d.pop(key, None)
    cur.execute(
        "INSERT INTO profiles (name, data) VALUES (?, ?)",
        (profile, pickle.dumps(d, protocol=4)),
    )
    print(f"entrypoint: registered profile {profile!r} in prefs21.db")
finally:
    con.close()
PYEOF

# Resolve command: if the caller passed a CMD override, honor it verbatim.
# Otherwise launch Anki against the resolved profile.
if [ "$#" -gt 0 ]; then
  exec "$@"
else
  echo "entrypoint: launching Anki with profile '${ANKI_PROFILE}'"
  exec anki -b "${ANKI_BASE}" -l en -p "${ANKI_PROFILE}"
fi
