-- Fitness Tracker Schema
-- SQLite database for tracking gym, cardio, and recovery metrics

PRAGMA journal_mode = WAL;
PRAGMA foreign_keys = ON;

-- ============================================================
-- GYM SESSIONS
-- ============================================================

CREATE TABLE IF NOT EXISTS gym_sessions (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    date            TEXT    NOT NULL,   -- ISO 8601 date (YYYY-MM-DD)
    time            TEXT,               -- HH:MM
    name            TEXT    NOT NULL,
    duration_min    INTEGER,
    volume_lb       REAL,
    calories        INTEGER,
    source_url      TEXT,
    notes           TEXT,
    created_at      TEXT    NOT NULL DEFAULT (datetime('now')),
    UNIQUE(date, name)
);

CREATE TABLE IF NOT EXISTS gym_exercises (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id      INTEGER NOT NULL REFERENCES gym_sessions(id) ON DELETE CASCADE,
    exercise_order  INTEGER NOT NULL DEFAULT 0,
    name            TEXT    NOT NULL,
    muscle_groups   TEXT,               -- comma-separated
    equipment       TEXT,
    superset_group  TEXT,               -- exercises with same group are supersetted
    notes           TEXT
);

CREATE INDEX IF NOT EXISTS idx_gym_exercises_session ON gym_exercises(session_id);
CREATE INDEX IF NOT EXISTS idx_gym_exercises_name    ON gym_exercises(name);

CREATE TABLE IF NOT EXISTS gym_sets (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    exercise_id     INTEGER NOT NULL REFERENCES gym_exercises(id) ON DELETE CASCADE,
    set_number      INTEGER NOT NULL,
    set_type        TEXT    NOT NULL DEFAULT 'working'
                        CHECK(set_type IN ('warmup','working','dropset','failure')),
    reps            INTEGER,
    weight_lb       REAL,
    assist_lb       REAL,               -- for assisted machines (pull-up/dip assist)
    notes           TEXT
);

CREATE INDEX IF NOT EXISTS idx_gym_sets_exercise ON gym_sets(exercise_id);

-- ============================================================
-- CARDIO SESSIONS (Garmin-aware)
-- ============================================================

CREATE TABLE IF NOT EXISTS cardio_sessions (
    id                          INTEGER PRIMARY KEY AUTOINCREMENT,
    date                        TEXT    NOT NULL,
    time                        TEXT,
    name                        TEXT,
    activity_type               TEXT    NOT NULL
        CHECK(activity_type IN (
            'running','cycling','swimming','rowing',
            'stair_climbing','walking','hiking','elliptical',
            'strength','other'
        )),
    duration_seconds            INTEGER,
    distance_meters             REAL,
    calories                    INTEGER,
    avg_hr_bpm                  INTEGER,
    max_hr_bpm                  INTEGER,
    avg_speed_mps               REAL,
    max_speed_mps               REAL,
    elevation_gain_m            REAL,
    elevation_loss_m            REAL,
    avg_cadence                 REAL,
    avg_power_watts             REAL,
    normalized_power_watts      REAL,
    training_effect_aerobic     REAL,
    training_effect_anaerobic   REAL,
    training_effect_label       TEXT,
    training_load               REAL,
    avg_stride_length_cm        REAL,
    avg_ground_contact_time_ms  REAL,
    avg_vertical_oscillation_cm REAL,
    garmin_activity_id          TEXT    UNIQUE,
    source_url                  TEXT,
    notes                       TEXT,
    created_at                  TEXT    NOT NULL DEFAULT (datetime('now')),
    UNIQUE(date, name)
);

CREATE INDEX IF NOT EXISTS idx_cardio_sessions_date ON cardio_sessions(date);
CREATE INDEX IF NOT EXISTS idx_cardio_sessions_type ON cardio_sessions(activity_type);

CREATE TABLE IF NOT EXISTS cardio_laps (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id      INTEGER NOT NULL REFERENCES cardio_sessions(id) ON DELETE CASCADE,
    lap_number      INTEGER NOT NULL,
    distance_m      REAL,
    duration_s      REAL,
    avg_speed_mps   REAL,
    avg_hr_bpm      INTEGER,
    avg_power_watts REAL,
    avg_cadence     REAL,
    elevation_gain_m REAL
);

CREATE INDEX IF NOT EXISTS idx_cardio_laps_session ON cardio_laps(session_id);

-- ============================================================
-- DAILY METRICS (Recovery / Garmin)
-- ============================================================

CREATE TABLE IF NOT EXISTS daily_metrics (
    id                          INTEGER PRIMARY KEY AUTOINCREMENT,
    date                        TEXT    NOT NULL UNIQUE,
    resting_hr                  INTEGER,
    hrv_ms                      REAL,
    hrv_baseline_ms             REAL,
    hrv_status                  TEXT,    -- e.g. 'balanced', 'low', 'high'
    body_battery_wake           INTEGER,
    body_battery_sleep          INTEGER,
    training_readiness_score    REAL,
    training_readiness_level    TEXT,
    vo2_max                     REAL,
    sleep_duration_min          REAL,
    sleep_score                 INTEGER,
    sleep_deep_min              REAL,
    sleep_rem_min               REAL,
    sleep_spo2_avg              REAL,
    steps                       INTEGER,
    active_calories             INTEGER,
    stress_avg                  INTEGER,
    training_status             TEXT,
    acwr                        REAL,    -- acute:chronic workload ratio
    acute_load                  REAL,
    chronic_load                REAL,
    created_at                  TEXT    NOT NULL DEFAULT (datetime('now'))
);

-- ============================================================
-- VIEWS
-- ============================================================

-- Top set weight per exercise per date
CREATE VIEW IF NOT EXISTS exercise_progression AS
SELECT
    gs.date,
    ge.name            AS exercise_name,
    ge.muscle_groups,
    MAX(CASE WHEN gset.assist_lb IS NOT NULL AND gset.assist_lb > 0
             THEN -gset.assist_lb   -- lower assist = stronger, show negative
             ELSE gset.weight_lb END) AS top_set_weight_lb,
    MAX(gset.reps)     AS top_set_reps,
    MIN(gset.assist_lb) AS min_assist_lb
FROM gym_sessions gs
JOIN gym_exercises ge  ON ge.session_id = gs.id
JOIN gym_sets gset     ON gset.exercise_id = ge.id
WHERE gset.set_type IN ('working','dropset','failure')
GROUP BY gs.date, ge.name;

-- Total volume by muscle group per week
CREATE VIEW IF NOT EXISTS weekly_volume AS
SELECT
    strftime('%Y-W%W', gs.date)   AS year_week,
    MIN(gs.date)                  AS week_start,
    mg.value                      AS muscle_group,
    SUM(gset.reps * COALESCE(gset.weight_lb, 0)) AS total_volume_lb,
    COUNT(DISTINCT gs.id)         AS sessions,
    COUNT(DISTINCT ge.id)         AS exercises,
    SUM(gset.reps)                AS total_reps
FROM gym_sessions gs
JOIN gym_exercises ge  ON ge.session_id = gs.id
JOIN gym_sets gset     ON gset.exercise_id = ge.id,
     json_each('["' || REPLACE(ge.muscle_groups, ',', '","') || '"]') AS mg
WHERE gset.set_type IN ('working','dropset','failure')
GROUP BY year_week, mg.value;

-- Weekly avg pace/power/HR per activity type
CREATE VIEW IF NOT EXISTS cardio_trends AS
SELECT
    strftime('%Y-W%W', date)    AS year_week,
    MIN(date)                   AS week_start,
    activity_type,
    COUNT(*)                    AS session_count,
    ROUND(AVG(avg_speed_mps), 2)     AS avg_speed_mps,
    ROUND(AVG(avg_hr_bpm), 0)        AS avg_hr_bpm,
    ROUND(AVG(avg_power_watts), 1)   AS avg_power_watts,
    ROUND(SUM(distance_meters), 0)   AS total_distance_m,
    ROUND(SUM(duration_seconds) / 60.0, 1) AS total_duration_min,
    ROUND(AVG(avg_cadence), 1)       AS avg_cadence
FROM cardio_sessions
GROUP BY year_week, activity_type;
