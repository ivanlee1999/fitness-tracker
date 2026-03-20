# Ivan's Fitness Tracker

A self-hosted fitness tracking system with SQLite storage, Python ingestion scripts, and a lightweight web dashboard.

## Architecture

```
fitness-tracker/
├── schema.sql                  # SQLite schema (tables + views)
├── fitness.db                  # SQLite database (gitignored)
├── run.sh                      # Start the dashboard
├── requirements.txt            # Python dependencies
├── scripts/
│   ├── db.py                   # Shared database utilities
│   ├── import_gym.py           # Import gym workouts (Motra text or JSON)
│   ├── import_garmin.py        # Import Garmin activities (JSON)
│   ├── import_daily_metrics.py # Import daily recovery metrics (JSON)
│   ├── migrate.py              # One-shot migration from workouts.json
│   └── backup_to_github.py     # Backup DB to private GitHub repo
└── dashboard/
    ├── app.py                  # FastAPI application
    ├── templates/              # Jinja2 HTML templates
    │   ├── base.html
    │   ├── index.html          # Today's summary
    │   ├── gym.html            # Gym session history
    │   ├── exercise.html       # Exercise progression chart
    │   ├── cardio.html         # Cardio session history
    │   ├── cardio_trends.html  # Cardio trends by activity type
    │   ├── recovery.html       # HRV, HR, body battery, sleep charts
    │   └── advice.html         # Rule-based training suggestions
    └── static/                 # Static assets (if needed)
```

## Quick Start

```bash
# Install dependencies
pip install -r requirements.txt

# Run migration (imports existing data)
cd scripts && python migrate.py && cd ..

# Start the dashboard
./run.sh
# → http://localhost:8090
```

## Importing Data

### Gym Workouts (Motra format)

```bash
# From file
python scripts/import_gym.py workout.txt

# From stdin (paste and Ctrl+D)
python scripts/import_gym.py

# From JSON
python scripts/import_gym.py --json workout.json
```

### Garmin Activities

```bash
python scripts/import_garmin.py activity.json
```

### Daily Metrics

```bash
python scripts/import_daily_metrics.py metrics.json
```

## Dashboard Pages

| Route | Description |
|-------|-------------|
| `/` | Today's summary: last workout, metrics, weekly counts |
| `/gym` | Gym history with muscle group filters |
| `/gym/exercise/<name>` | Exercise progression chart (weight over time) |
| `/cardio` | Cardio history by activity type |
| `/cardio/<type>` | Cardio trends: pace, HR, distance charts |
| `/recovery` | HRV, resting HR, body battery, sleep charts |
| `/advice` | Rule-based training suggestions (last 30 days) |

## Database

SQLite with three main domains:

- **Gym**: `gym_sessions` → `gym_exercises` → `gym_sets`
- **Cardio**: `cardio_sessions` → `cardio_laps`
- **Recovery**: `daily_metrics`

Views: `exercise_progression`, `weekly_volume`, `cardio_trends`

## Backup

```bash
python scripts/backup_to_github.py
```

Pushes `fitness.db` to `ivanlee1999/ivan-fitness-data` (private repo).

## Tech Stack

- **Python 3.11+** with FastAPI, Jinja2, uvicorn
- **SQLite** (no ORM, direct SQL)
- **Chart.js** via CDN (no build step)
- **Bootstrap 5** dark theme
