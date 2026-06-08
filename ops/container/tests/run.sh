#!/usr/bin/env bash
#
# Run the bootstrap-profile.sh test suite.
#
# Looks for `bats` on PATH. If not found, suggests installation.
# Otherwise invokes bats against the suite and reports pass/fail.

set -euo pipefail

HERE="$(cd "$(dirname "$0")" && pwd)"

if ! command -v bats >/dev/null 2>&1; then
  cat >&2 <<'EOF'
bats not on PATH. Install via:
  sudo apt install bats          # Debian/Ubuntu, version 1.10.0+
  brew install bats-core         # macOS
Or vendor from source:
  git clone --depth 1 https://github.com/bats-core/bats-core.git ~/bats-core
  export PATH="$HOME/bats-core/bin:$PATH"
EOF
  exit 1
fi

cd "$HERE"
exec bats bootstrap-profile.bats "$@"
