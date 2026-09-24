#!/usr/bin/env bash
# Ensure the API (:8010) and wizard (:5180) are up, then open a chrome-free window.
# Resolves symlinks, so this file can live on PATH as `omarchy-grok-imagine`.
set -euo pipefail

SOURCE="${BASH_SOURCE[0]}"
while [[ -L "${SOURCE}" ]]; do
  DIR="$(cd -P "$(dirname "${SOURCE}")" && pwd)"
  SOURCE="$(readlink "${SOURCE}")"
  [[ "${SOURCE}" != /* ]] && SOURCE="${DIR}/${SOURCE}"
done
ROOT="$(cd -P "$(dirname "${SOURCE}")/.." && pwd)"

API_URL="http://127.0.0.1:8010"
WEB_URL="http://127.0.0.1:5180"
APP_CLASS="OmarchyGrokImagine"
API_LOG="/tmp/omarchy-grok-imagine-api.log"
WEB_LOG="/tmp/omarchy-grok-imagine-web.log"

cd "${ROOT}"

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

listening() {
  local url="$1"
  if command -v curl >/dev/null 2>&1; then
    curl -fsS --max-time 1 "${url}" >/dev/null 2>&1
    return
  fi
  local py
  for py in python python3; do
    if command -v "${py}" >/dev/null 2>&1; then
      "${py}" -c 'import sys, urllib.request
try:
    urllib.request.urlopen(sys.argv[1], timeout=1)
except Exception:
    sys.exit(1)' "${url}"
      return
    fi
  done
  return 1
}

wait_for() {
  local url="$1"
  local _try
  for _try in $(seq 1 40); do
    if listening "${url}"; then
      return 0
    fi
    sleep 0.25
  done
  return 1
}

start_api() {
  if command -v uvicorn >/dev/null 2>&1; then
    nohup uvicorn omarchy_imagine.app:app --host 127.0.0.1 --port 8010 >"${API_LOG}" 2>&1 &
    return
  fi
  local py
  for py in python python3; do
    if command -v "${py}" >/dev/null 2>&1; then
      nohup "${py}" -m uvicorn omarchy_imagine.app:app --host 127.0.0.1 --port 8010 >"${API_LOG}" 2>&1 &
      return
    fi
  done
  echo "uvicorn not found. From ${ROOT}: python -m venv .venv && pip install -e ." >&2
  exit 1
}

if ! listening "${API_URL}/api/health"; then
  start_api
  if ! wait_for "${API_URL}/api/health"; then
    echo "API did not start on ${API_URL}. See ${API_LOG}." >&2
    exit 1
  fi
fi

if ! listening "${WEB_URL}"; then
  if ! command -v npm >/dev/null 2>&1; then
    echo "npm not found. Install nodejs and npm, then re-run." >&2
    exit 1
  fi
  cd "${ROOT}/web"
  if [[ ! -d dist ]]; then
    npm install
    npm run build
  fi
  nohup npm run preview >"${WEB_LOG}" 2>&1 &
  if ! wait_for "${WEB_URL}"; then
    echo "Wizard did not start on ${WEB_URL}. See ${WEB_LOG}." >&2
    exit 1
  fi
fi

# Omarchy web app (chrome-free), then Chromium/Chrome --app=, then xdg-open.
open_app() {
  if command -v omarchy-launch-or-focus-webapp >/dev/null 2>&1; then
    omarchy-launch-or-focus-webapp "${APP_CLASS}" "${WEB_URL}" --class="${APP_CLASS}" >/dev/null 2>&1 || true
    return
  fi
  if command -v omarchy-launch-webapp >/dev/null 2>&1; then
    omarchy-launch-webapp "${WEB_URL}" --class="${APP_CLASS}" >/dev/null 2>&1 || true
    return
  fi
  local bin
  for bin in chromium google-chrome google-chrome-stable chromium-browser; do
    if command -v "${bin}" >/dev/null 2>&1; then
      nohup "${bin}" --app="${WEB_URL}" --class="${APP_CLASS}" >/dev/null 2>&1 &
      return
    fi
  done
  if command -v xdg-open >/dev/null 2>&1; then
    xdg-open "${WEB_URL}" >/dev/null 2>&1 || true
    return
  fi
  echo "Open ${WEB_URL}"
}

open_app
