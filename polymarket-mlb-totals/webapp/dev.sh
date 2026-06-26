#!/usr/bin/env bash
# Start the Polymarket Sports dashboard backend (:8000) + frontend (:5173) for dev.
set -euo pipefail
cd "$(dirname "$0")"

# --- Backend -------------------------------------------------------------
cd backend
if [ ! -d .venv ]; then
  python3 -m venv .venv
  ./.venv/bin/pip install -q -r requirements.txt
fi
./.venv/bin/uvicorn app:app --port 8000 --reload &
BACK_PID=$!
cd ..

# --- Frontend ------------------------------------------------------------
cd frontend
[ -d node_modules ] || npm install
npm run dev -- --port 5173 &
FRONT_PID=$!
cd ..

trap 'kill $BACK_PID $FRONT_PID 2>/dev/null' EXIT INT TERM
echo "Backend  -> http://localhost:8000"
echo "Frontend -> http://localhost:5173"
wait
