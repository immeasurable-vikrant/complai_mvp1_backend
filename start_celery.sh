#!/bin/bash
# Start Celery worker — run from anywhere: ./start_celery.sh
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
BACKEND_DIR="$SCRIPT_DIR/backend"

cd "$BACKEND_DIR" || exit 1
source venv/bin/activate

# Export PYTHONPATH so spawned worker processes (macOS spawn start-method)
# can find models/, agents/, utils/ without needing to cd into backend/ first.
export PYTHONPATH="$BACKEND_DIR:$PYTHONPATH"

exec celery -A workers.celery_app worker --loglevel=info --concurrency=2
