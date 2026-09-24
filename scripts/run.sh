#!/usr/bin/env bash
# Start the API on :8010 and the built wizard on :5180.
set -euo pipefail
cd "$(dirname "$0")/.."

if [[ -f .env ]]; then
  set -a
  # shellcheck disable=SC1091
  source .env
  set +a
fi

if [[ -f .venv/bin/activate ]]; then
  # shellcheck disable=SC1091
  source .venv/bin/activate
fi

uvicorn omarchy_imagine.app:app --host 127.0.0.1 --port 8010 &
api_pid=$!
cleanup() {
  kill "$api_pid" >/dev/null 2>&1 || true
}
trap cleanup EXIT

cd web
npm run preview
