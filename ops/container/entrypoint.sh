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

# Resolve command: if the caller passed a CMD override, honor it verbatim.
# Otherwise launch Anki against the resolved profile. Profile is created
# implicitly by Anki on first start if the directory doesn't exist.
if [ "$#" -gt 0 ]; then
  exec "$@"
else
  echo "entrypoint: launching Anki with profile '${ANKI_PROFILE}'"
  exec anki -b "${ANKI_BASE}" -l en -p "${ANKI_PROFILE}"
fi
