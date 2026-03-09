import sqlite3
from datetime import datetime

DB_PATH = "running.db"

# ─────────────────────────────────────────
# INIT
# ─────────────────────────────────────────

def init_db():
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()

    c.execute("""
        CREATE TABLE IF NOT EXISTS users (
            id    INTEGER PRIMARY KEY AUTOINCREMENT,
            name  TEXT NOT NULL UNIQUE,
            emoji TEXT NOT NULL
        )
    """)

    for name, emoji in [("Cristian", "🏃"), ("Adrien", "⚡"), ("Laurine", "🌟")]:
        c.execute("INSERT OR IGNORE INTO users (name, emoji) VALUES (?, ?)", (name, emoji))

    c.execute("""
        CREATE TABLE IF NOT EXISTS runs (
            id               INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id          INTEGER NOT NULL,
            date             TEXT NOT NULL,
            type             TEXT NOT NULL DEFAULT 'run',
            distance         REAL NOT NULL DEFAULT 0,
            time_seconds     INTEGER NOT NULL DEFAULT 0,
            pace             TEXT NOT NULL DEFAULT '—',
            effort           INTEGER NOT NULL DEFAULT 5,
            notes            TEXT,
            interval_dist    REAL,
            interval_reps    INTEGER,
            interval_pace    TEXT,
            circuit_rounds   INTEGER,
            circuit_details  TEXT,
            technique_drills TEXT,
            FOREIGN KEY (user_id) REFERENCES users(id)
        )
    """)

    c.execute("""
        CREATE TABLE IF NOT EXISTS cooper_tests (
            id           INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id      INTEGER NOT NULL,
            date         TEXT NOT NULL,
            distance_m   REAL NOT NULL,
            vo2max       REAL NOT NULL,
            fitness_level TEXT NOT NULL,
            proj_10k     TEXT NOT NULL,
            notes        TEXT,
            FOREIGN KEY (user_id) REFERENCES users(id)
        )
    """)

    conn.commit()
    conn.close()


# ─────────────────────────────────────────
# USERS
# ─────────────────────────────────────────

def get_users():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    c = conn.cursor()
    c.execute("SELECT * FROM users ORDER BY id")
    rows = c.fetchall()
    conn.close()
    return [dict(r) for r in rows]


# ─────────────────────────────────────────
# RUNS
# ─────────────────────────────────────────

def get_runs_by_user(user_id):
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    c = conn.cursor()
    c.execute("SELECT * FROM runs WHERE user_id = ? ORDER BY date DESC", (user_id,))
    rows = c.fetchall()
    conn.close()
    return [dict(r) for r in rows]


def add_run(run):
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("""
        INSERT INTO runs (
            user_id, date, type, distance, time_seconds, pace, effort, notes,
            interval_dist, interval_reps, interval_pace,
            circuit_rounds, circuit_details, technique_drills
        ) VALUES (
            :user_id, :date, :type, :distance, :time_seconds, :pace, :effort, :notes,
            :interval_dist, :interval_reps, :interval_pace,
            :circuit_rounds, :circuit_details, :technique_drills
        )
    """, run)
    new_id = c.lastrowid
    conn.commit()
    conn.close()
    return new_id


def delete_run(run_id):
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("DELETE FROM runs WHERE id = ?", (run_id,))
    conn.commit()
    conn.close()


# ─────────────────────────────────────────
# STATS
# ─────────────────────────────────────────

def get_stats_by_user(user_id):
    runs = get_runs_by_user(user_id)
    running = [r for r in runs if r["distance"] > 0]

    total_km   = round(sum(r["distance"] for r in running), 2)
    total_runs = len(runs)

    avg_pace = best_pace = "—"
    timed = [r for r in running if r["time_seconds"] > 0]
    if timed:
        total_sec  = sum(r["time_seconds"] for r in timed)
        total_dist = sum(r["distance"]     for r in timed)
        avg_s  = total_sec / total_dist if total_dist > 0 else 0
        avg_pace = f"{int(avg_s//60)}:{int(avg_s%60):02d}"
        best = min(timed, key=lambda r: r["time_seconds"] / r["distance"])
        best_pace = best["pace"]

    week_start = datetime.today().strftime("%Y-%W")
    weekly_km = round(sum(
        r["distance"] for r in running
        if datetime.strptime(r["date"], "%Y-%m-%d").strftime("%Y-%W") == week_start
    ), 2)

    # 10k projection from best pace
    proj_10k = None
    if best_pace != "—":
        parts   = best_pace.split(":")
        pace_s  = int(parts[0]) * 60 + int(parts[1])
        total_p = pace_s * 10
        proj_10k = f"{total_p//60}:{total_p%60:02d}"

    return {
        "total_km":    total_km,
        "total_runs":  total_runs,
        "avg_pace":    avg_pace,
        "best_pace":   best_pace,
        "weekly_km":   weekly_km,
        "proj_10k":    proj_10k
    }


# ─────────────────────────────────────────
# COOPER TESTS
# ─────────────────────────────────────────

def _cooper_calc(distance_m):
    """Returns vo2max, fitness_level, proj_10k given 12-min distance in meters."""
    vo2max = round((distance_m - 504.9) / 44.73, 1)

    # Fitness level (general adult scale)
    if vo2max < 28:
        level = "Very Poor"
    elif vo2max < 34:
        level = "Poor"
    elif vo2max < 42:
        level = "Average"
    elif vo2max < 52:
        level = "Good"
    elif vo2max < 60:
        level = "Excellent"
    else:
        level = "Superior"

    # 10k projection using Jack Daniels VDOT approximation
    # pace_per_km (s) ≈ (29.54 + 5.000663 * vo2max) ... simplified linear for clarity
    # Better: use fraction of VO2max at 10k pace (~95%) then back-calculate
    # pace_per_km(s) = 60 / (0.000104 * vo2max^2 + 0.1981 * vo2max - 4.6) — simplified
    try:
        vdot_pace = 60 / (0.000104 * vo2max**2 + 0.1981 * vo2max - 4.6)
        total_s   = int(vdot_pace * 10 * 60)
        proj_10k  = f"{total_s//60}:{total_s%60:02d}"
    except Exception:
        proj_10k = "—"

    return vo2max, level, proj_10k


def add_cooper_test(data):
    vo2max, level, proj_10k = _cooper_calc(data["distance_m"])
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("""
        INSERT INTO cooper_tests (user_id, date, distance_m, vo2max, fitness_level, proj_10k, notes)
        VALUES (?, ?, ?, ?, ?, ?, ?)
    """, (data["user_id"], data["date"], data["distance_m"], vo2max, level, proj_10k, data.get("notes", "")))
    new_id = c.lastrowid
    conn.commit()
    conn.close()
    return {"id": new_id, "vo2max": vo2max, "fitness_level": level, "proj_10k": proj_10k}


def get_cooper_tests(user_id):
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    c = conn.cursor()
    c.execute("SELECT * FROM cooper_tests WHERE user_id = ? ORDER BY date DESC", (user_id,))
    rows = c.fetchall()
    conn.close()
    return [dict(r) for r in rows]


def delete_cooper_test(test_id):
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("DELETE FROM cooper_tests WHERE id = ?", (test_id,))
    conn.commit()
    conn.close()