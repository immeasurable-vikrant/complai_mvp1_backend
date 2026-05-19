#!/bin/bash
# Start FastAPI — run from anywhere: ./start_api.sh
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
BACKEND_DIR="$SCRIPT_DIR/backend"

cd "$BACKEND_DIR" || exit 1
source venv/bin/activate

export PYTHONPATH="$BACKEND_DIR:$PYTHONPATH"

exec uvicorn main:app --reload --port 8000
