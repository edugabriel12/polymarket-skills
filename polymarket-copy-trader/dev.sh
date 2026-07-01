#!/usr/bin/env bash
# Start the Polymarket copy-trader backend (:8002) + frontend (:5175) for dev.
set -euo pipefail
cd "$(dirname "$0")"

# --- Backend -------------------------------------------------------------
cd backend
if [ ! -d .venv ]; then
  python3 -m venv .venv
  ./.venv/bin/pip install -q -r requirements.txt
fi
./.venv/bin/uvicorn app:app --port 8002 --reload &
BACK_PID=$!
cd ..

# --- Frontend ------------------------------------------------------------
cd frontend
[ -d node_modules ] || npm install
npm run dev -- --port 5175 &
FRONT_PID=$!
cd ..

trap 'kill $BACK_PID $FRONT_PID 2>/dev/null' EXIT INT TERM
echo "Backend  -> http://localhost:8002"
echo "Frontend -> http://localhost:5175"
wait
