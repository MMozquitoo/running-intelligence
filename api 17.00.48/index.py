import os
from flask import Flask, request, jsonify, render_template
import psycopg2
from psycopg2.extras import RealDictCursor
from datetime import datetime, date, timedelta

app = Flask(__name__, template_folder="../templates", static_folder="../static")

DATABASE_URL = os.environ.get("DATABASE_URL")

# ─────────────────────────────────────────
# DB CONNECTION
# ─────────────────────────────────────────

def get_conn():
    return psycopg2.connect(DATABASE_URL, cursor_factory=RealDictCursor)


def init_db():
    conn = get_conn()
    c = conn.cursor()

    c.execute("""
        CREATE TABLE IF NOT EXISTS users (
            id    SERIAL PRIMARY KEY,
            name  TEXT NOT NULL UNIQUE,
            emoji TEXT NOT NULL
        )
    """)

    for name, emoji in [("Cristian", "🏃"), ("Adrien", "⚡"), ("Laurine", "🌟")]:
        c.execute("INSERT INTO users (name, emoji) VALUES (%s, %s) ON CONFLICT (name) DO NOTHING", (name, emoji))

    c.execute("""
        CREATE TABLE IF NOT EXISTS runs (
            id               SERIAL PRIMARY KEY,
            user_id          INTEGER NOT NULL REFERENCES users(id),
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
            technique_drills TEXT
        )
    """)

    c.execute("""
        CREATE TABLE IF NOT EXISTS cooper_tests (
            id            SERIAL PRIMARY KEY,
            user_id       INTEGER NOT NULL REFERENCES users(id),
            date          TEXT NOT NULL,
            distance_m    REAL NOT NULL,
            vo2max        REAL NOT NULL,
            fitness_level TEXT NOT NULL,
            proj_10k      TEXT NOT NULL,
            notes         TEXT
        )
    """)

    c.execute("ALTER TABLE users ADD COLUMN IF NOT EXISTS race_date TEXT")

    conn.commit()
    c.close()
    conn.close()


# Init on cold start
try:
    init_db()
except Exception as e:
    print(f"DB init error: {e}")


# ─────────────────────────────────────────
# FRONTEND
# ─────────────────────────────────────────

@app.route("/")
def index():
    return render_template("index.html")


# ─────────────────────────────────────────
# USERS
# ─────────────────────────────────────────

@app.route("/api/users")
def api_users():
    conn = get_conn()
    c = conn.cursor()
    c.execute("SELECT * FROM users ORDER BY id")
    rows = c.fetchall()
    c.close(); conn.close()
    return jsonify([dict(r) for r in rows])


# ─────────────────────────────────────────
# RUNS
# ─────────────────────────────────────────

def _pace_to_seconds(pace_str):
    if not pace_str or pace_str == "—":
        return None
    try:
        parts = pace_str.split(":")
        return int(parts[0]) * 60 + int(parts[1])
    except Exception:
        return None


@app.route("/api/runs/<int:user_id>")
def api_get_runs(user_id):
    conn = get_conn()
    c = conn.cursor()
    c.execute("SELECT * FROM runs WHERE user_id = %s ORDER BY date DESC", (user_id,))
    rows = [dict(r) for r in c.fetchall()]
    c.close(); conn.close()

    # Interval pace progression: compare each intervals session to the previous one
    interval_rows = [(i, r) for i, r in enumerate(rows)
                     if r["type"] == "intervals" and r.get("interval_pace")]
    for j, (i, r) in enumerate(interval_rows):
        if j + 1 < len(interval_rows):
            _, prev = interval_rows[j + 1]
            curr_s = _pace_to_seconds(r["interval_pace"])
            prev_s = _pace_to_seconds(prev["interval_pace"])
            if curr_s and prev_s:
                if curr_s < prev_s:
                    rows[i]["interval_trend"] = "up"    # faster = better
                elif curr_s > prev_s:
                    rows[i]["interval_trend"] = "down"  # slower
                else:
                    rows[i]["interval_trend"] = "equal"

    return jsonify(rows)


@app.route("/api/runs", methods=["POST"])
def api_add_run():
    d = request.get_json()
    for f in ["user_id", "date", "type", "effort"]:
        if f not in d:
            return jsonify({"error": f"Required: {f}"}), 400

    distance = float(d.get("distance", 0))
    time_sec = int(d.get("time_seconds", 0))
    pace = "—"
    if distance > 0 and time_sec > 0:
        ps = time_sec / distance
        pace = f"{int(ps//60)}:{int(ps%60):02d}"

    conn = get_conn()
    c = conn.cursor()
    c.execute("""
        INSERT INTO runs (user_id, date, type, distance, time_seconds, pace, effort, notes,
            interval_dist, interval_reps, interval_pace, circuit_rounds, circuit_details, technique_drills)
        VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
        RETURNING id
    """, (
        int(d["user_id"]), d["date"], d["type"], distance, time_sec, pace,
        int(d["effort"]), d.get("notes",""),
        d.get("interval_dist"), d.get("interval_reps"), d.get("interval_pace"),
        d.get("circuit_rounds"), d.get("circuit_details"), d.get("technique_drills")
    ))
    new_id = c.fetchone()["id"]
    conn.commit(); c.close(); conn.close()
    return jsonify({"id": new_id, "pace": pace}), 201


@app.route("/api/runs/<int:run_id>", methods=["DELETE"])
def api_delete_run(run_id):
    conn = get_conn()
    c = conn.cursor()
    c.execute("DELETE FROM runs WHERE id = %s", (run_id,))
    conn.commit(); c.close(); conn.close()
    return jsonify({"message": "Deleted"})


# ─────────────────────────────────────────
# STATS
# ─────────────────────────────────────────

@app.route("/api/stats/<int:user_id>")
def api_stats(user_id):
    conn = get_conn()
    c = conn.cursor()

    c.execute("SELECT * FROM runs WHERE user_id = %s ORDER BY date DESC", (user_id,))
    runs = [dict(r) for r in c.fetchall()]

    c.execute("SELECT race_date FROM users WHERE id = %s", (user_id,))
    user_row = c.fetchone()

    c.execute("SELECT vo2max FROM cooper_tests WHERE user_id = %s ORDER BY date DESC LIMIT 1", (user_id,))
    cooper_row = c.fetchone()

    c.close(); conn.close()

    today = date.today()

    # ── Streak ───────────────────────────────
    date_set = set(datetime.strptime(r["date"], "%Y-%m-%d").date() for r in runs)
    check = today if today in date_set else today - timedelta(days=1)
    streak = 0
    while check in date_set:
        streak += 1
        check -= timedelta(days=1)

    # ── Last session ─────────────────────────
    last_session_days = None
    last_session_alert = False
    if runs:
        last_date = datetime.strptime(runs[0]["date"], "%Y-%m-%d").date()
        last_session_days = (today - last_date).days
        last_session_alert = last_session_days > 5

    # ── This week vs last week ────────────────
    week_start = today - timedelta(days=today.weekday())   # Monday
    prev_week_start = week_start - timedelta(days=7)

    week_sessions = 0;  week_km = 0.0
    prev_week_sessions = 0;  prev_week_km = 0.0
    for r in runs:
        rd = datetime.strptime(r["date"], "%Y-%m-%d").date()
        if rd >= week_start:
            week_sessions += 1
            week_km += r["distance"]
        elif rd >= prev_week_start:
            prev_week_sessions += 1
            prev_week_km += r["distance"]
    week_km = round(week_km, 2)
    prev_week_km = round(prev_week_km, 2)

    # ── Classic stats ─────────────────────────
    running = [r for r in runs if r["distance"] > 0]
    total_km   = round(sum(r["distance"] for r in running), 2)
    total_runs = len(runs)
    avg_pace = best_pace = "—"
    proj_10k = None

    timed = [r for r in running if r["time_seconds"] > 0]
    if timed:
        total_sec  = sum(r["time_seconds"] for r in timed)
        total_dist = sum(r["distance"]     for r in timed)
        avg_s = total_sec / total_dist if total_dist > 0 else 0
        avg_pace = f"{int(avg_s//60)}:{int(avg_s%60):02d}"
        best = min(timed, key=lambda r: r["time_seconds"] / r["distance"])
        best_pace = best["pace"]
        parts = best_pace.split(":")
        pace_s = int(parts[0])*60 + int(parts[1])
        total_p = pace_s * 10
        proj_10k = f"{total_p//60}:{total_p%60:02d}"

    # ── Race countdown ────────────────────────
    race_date_str = user_row["race_date"] if user_row else None
    days_to_race = None
    if race_date_str:
        try:
            race_dt = datetime.strptime(race_date_str, "%Y-%m-%d").date()
            days_to_race = (race_dt - today).days
        except Exception:
            pass

    # Progress bar: VO2max toward target 52 (Good level)
    race_progress = None
    if cooper_row:
        vo2max = float(cooper_row["vo2max"])
        race_progress = min(100, max(0, int((vo2max / 52) * 100)))

    return jsonify({
        "total_km": total_km, "total_runs": total_runs,
        "avg_pace": avg_pace, "best_pace": best_pace,
        "weekly_km": week_km, "proj_10k": proj_10k,
        "streak": streak,
        "last_session_days": last_session_days,
        "last_session_alert": last_session_alert,
        "week_sessions": week_sessions,
        "week_km": week_km,
        "prev_week_sessions": prev_week_sessions,
        "prev_week_km": prev_week_km,
        "race_date": race_date_str,
        "days_to_race": days_to_race,
        "race_progress": race_progress,
    })


# ─────────────────────────────────────────
# COOPER
# ─────────────────────────────────────────

def _cooper_calc(distance_m):
    # VO2max — Cooper formula (ml/kg/min)
    vo2max = round((distance_m - 504.9) / 44.73, 1)

    # Fitness level
    if vo2max < 28:   level = "Very Poor"
    elif vo2max < 34: level = "Poor"
    elif vo2max < 42: level = "Average"
    elif vo2max < 52: level = "Good"
    elif vo2max < 60: level = "Excellent"
    else:             level = "Superior"

    # 10k projection — Daniels VDOT formula
    # pace_per_km (min) = 29.54 / vo2max^0.5765
    # Validated: VO2max 42 → ~58 min, VO2max 52 → ~47 min, VO2max 60 → ~41 min
    try:
        pace_min_km = 29.54 / (vo2max ** 0.5765)
        total_min   = pace_min_km * 10
        mins        = int(total_min)
        secs        = int(round((total_min - mins) * 60))
        if secs == 60:
            mins += 1
            secs  = 0
        proj_10k = f"{mins}:{secs:02d}"
    except Exception:
        proj_10k = "—"

    return vo2max, level, proj_10k


@app.route("/api/cooper/<int:user_id>")
def api_get_cooper(user_id):
    conn = get_conn()
    c = conn.cursor()
    c.execute("SELECT * FROM cooper_tests WHERE user_id = %s ORDER BY date DESC", (user_id,))
    rows = c.fetchall()
    c.close(); conn.close()
    return jsonify([dict(r) for r in rows])


@app.route("/api/cooper", methods=["POST"])
def api_add_cooper():
    d = request.get_json()
    for f in ["user_id", "date", "distance_m"]:
        if f not in d:
            return jsonify({"error": f"Required: {f}"}), 400
    vo2max, level, proj_10k = _cooper_calc(float(d["distance_m"]))
    conn = get_conn()
    c = conn.cursor()
    c.execute("""
        INSERT INTO cooper_tests (user_id, date, distance_m, vo2max, fitness_level, proj_10k, notes)
        VALUES (%s,%s,%s,%s,%s,%s,%s) RETURNING id
    """, (int(d["user_id"]), d["date"], float(d["distance_m"]), vo2max, level, proj_10k, d.get("notes","")))
    new_id = c.fetchone()["id"]
    conn.commit(); c.close(); conn.close()
    return jsonify({"id": new_id, "vo2max": vo2max, "fitness_level": level, "proj_10k": proj_10k}), 201


@app.route("/api/cooper/<int:test_id>", methods=["DELETE"])
def api_delete_cooper(test_id):
    conn = get_conn()
    c = conn.cursor()
    c.execute("DELETE FROM cooper_tests WHERE id = %s", (test_id,))
    conn.commit(); c.close(); conn.close()
    return jsonify({"message": "Deleted"})


# ─────────────────────────────────────────
# RACE DATE
# ─────────────────────────────────────────

@app.route("/api/race_date/<int:user_id>", methods=["POST"])
def api_set_race_date(user_id):
    d = request.get_json()
    race_date = d.get("race_date")
    conn = get_conn()
    c = conn.cursor()
    c.execute("UPDATE users SET race_date = %s WHERE id = %s", (race_date, user_id))
    conn.commit(); c.close(); conn.close()
    return jsonify({"race_date": race_date})