#!/usr/bin/env bash
# Run the Social Match server in the FOREGROUND (use tmux/screen).
# Loads deploy-time config from socialmatch.conf, then execs uvicorn on loopback:3278.
# Caddy forwards devhacks.navraj.me -> 127.0.0.1:3278; Cloudflare fronts Caddy.
set -euo pipefail
cd "$(dirname "$0")/.."

if [ ! -f socialmatch.conf ]; then
  echo "missing socialmatch.conf in $(pwd)" >&2
  echo "expected KEY=VALUE lines (see .env.example)" >&2
  exit 1
fi

if [ ! -x .venv/bin/python ]; then
  echo ".venv missing; run: uv sync" >&2
  exit 1
fi

set -a
# shellcheck disable=SC1091
. ./socialmatch.conf
set +a

echo "starting on 127.0.0.1:3278  (BASE_URL=${BASE_URL:-unset})"
exec .venv/bin/python -m uvicorn server.app:app \
  --host 127.0.0.1 --port 3278 \
  --proxy-headers --forwarded-allow-ips=127.0.0.1,::1
