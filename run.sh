#!/usr/bin/env bash
# Start the Fitness Tracker dashboard on port 8090
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

# Initialize DB if needed
python3 -c "
import sys; sys.path.insert(0, 'scripts')
from db import init_db; init_db()
print('Database ready.')
"

echo "Starting Fitness Tracker dashboard at http://localhost:8090"
exec uvicorn dashboard.app:app --host 0.0.0.0 --port 8091 --reload
