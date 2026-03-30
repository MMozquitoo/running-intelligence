import os
import json
import re
from flask import Flask, request, jsonify, render_template
import psycopg2
from psycopg2.extras import RealDictCursor
from datetime import datetime, date, timedelta
import anthropic

_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
app = Flask(__name__,
            template_folder=os.path.join(_root, "templates"),
            static_folder=os.path.join(_root, "static"))

DATABASE_URL = os.environ.get("DATABASE_URL")
ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY")

# ─────────────────────────────────────────
# WORLD ATHLETICS STANDARDS
# ─────────────────────────────────────────

ATHLETICS_STANDARDS = {
    "male": {
        25: {
            "100m":  {"beginner": 14, "amateur": 12, "good": 11},
            "400m":  {"beginner": 75, "amateur": 58, "good": 50},
            "800m":  {"beginner": 210, "amateur": 165, "good": 140},
            "1mile": {"beginner": 480, "amateur": 375, "good": 310},
            "5km":   {"beginner": 1800, "amateur": 1380, "good": 1140},
            "10km":  {"beginner": 3600, "amateur": 2880, "good": 2400},
        },
        35: {
            "100m":  {"beginner": 15, "amateur": 13, "good": 12},
            "400m":  {"beginner": 80, "amateur": 62, "good": 54},
            "800m":  {"beginner": 225, "amateur": 175, "good": 150},
            "1mile": {"beginner": 510, "amateur": 395, "good": 330},
            "5km":   {"beginner": 1920, "amateur": 1500, "good": 1200},
            "10km":  {"beginner": 3840, "amateur": 3000, "good": 2520},
        },
        50: {
            "100m":  {"beginner": 17, "amateur": 14, "good": 13},
            "400m":  {"beginner": 90, "amateur": 70, "good": 60},
            "800m":  {"beginner": 255, "amateur": 195, "good": 165},
            "1mile": {"beginner": 570, "amateur": 435, "good": 365},
            "5km":   {"beginner": 2100, "amateur": 1620, "good": 1320},
            "10km":  {"beginner": 4200, "amateur": 3240, "good": 2760},
        },
    },
    "female": {
        25: {
            "100m":  {"beginner": 16, "amateur": 14, "good": 13},
            "400m":  {"beginner": 85, "amateur": 68, "good": 58},
            "800m":  {"beginner": 240, "amateur": 190, "good": 160},
            "1mile": {"beginner": 540, "amateur": 420, "good": 350},
            "5km":   {"beginner": 2100, "amateur": 1560, "good": 1260},
            "10km":  {"beginner": 4200, "amateur": 3120, "good": 2640},
        },
        35: {
            "100m":  {"beginner": 17, "amateur": 15, "good": 14},
            "400m":  {"beginner": 92, "amateur": 73, "good": 63},
            "800m":  {"beginner": 255, "amateur": 200, "good": 170},
            "1mile": {"beginner": 570, "amateur": 445, "good": 375},
            "5km":   {"beginner": 2220, "amateur": 1680, "good": 1320},
            "10km":  {"beginner": 4440, "amateur": 3360, "good": 2760},
        },
        50: {
            "100m":  {"beginner": 19, "amateur": 16, "good": 15},
            "400m":  {"beginner": 100, "amateur": 80, "good": 70},
            "800m":  {"beginner": 285, "amateur": 220, "good": 188},
            "1mile": {"beginner": 630, "amateur": 490, "good": 415},
            "5km":   {"beginner": 2400, "amateur": 1800, "good": 1440},
            "10km":  {"beginner": 4800, "amateur": 3600, "good": 3000},
        },
    }
}


USER_COACHING_PROFILES = {
    "Cristian": {
        "language": "Spanish",
        "coaching_style": "encouraging, educational, explain the why behind each session",
        "level_description": "beginner transitioning to intermediate, 21 years old, needs to build base first",
        "push_factor": "moderate — build habits before intensity",
        "special_notes": "Explain physiological adaptations so he understands why he's doing each session. Keep volume conservative.",
    },
    "Adrien": {
        "language": "french",
        "coaching_style": "direct, demanding, no hand-holding, always push to the limit",
        "level_description": "lifelong athlete, 43 years old, amateur competitive, high pain tolerance, always wants maximum output",
        "push_factor": "MAXIMUM — he has decades of athletic background, always demand more, never go easy",
        "special_notes": "Adrien a fait du sport toute sa vie. Ne jamais sous-estimer sa capacite. Toujours pousser au maximum. Utiliser des seances plus dures que pour les autres.",
    },
    "Laurine": {
        "language": "french",
        "coaching_style": "supportive but structured, technically precise",
        "level_description": "female amateur runner, 23 years old, good base, working on speed and consistency",
        "push_factor": "moderate-high — she can handle quality sessions but needs recovery balance",
        "special_notes": "Adapter les charges a la physiologie feminine. Attention aux cycles de recuperation.",
    },
}


def get_standard_for_user(profile_data):
    age = profile_data.get("age", 25)
    sex = profile_data.get("sex", "male")
    level = profile_data.get("level", "beginner")
    level_map = {
        "principiante": "beginner", "intermedio": "amateur", "avanzado": "good",
        "amateur": "amateur", "beginner": "beginner", "good": "good",
        "intermediate": "amateur", "advanced": "good",
    }
    level_key = level_map.get(level, "beginner")
    standards = ATHLETICS_STANDARDS.get(sex, ATHLETICS_STANDARDS["male"])
    for age_max in sorted(standards.keys()):
        if age <= age_max:
            return {k: v[level_key] for k, v in standards[age_max].items()}
    last = sorted(standards.keys())[-1]
    return {k: v[level_key] for k, v in standards[last].items()}


# ─────────────────────────────────────────
# DB CONNECTION
# ─────────────────────────────────────────

def get_conn():
    return psycopg2.connect(DATABASE_URL, cursor_factory=RealDictCursor)


def _strip_fences(text):
    """Strip markdown code fences that models sometimes add around JSON."""
    text = text.strip()
    if text.startswith("```"):
        text = re.sub(r"^```[a-z]*\n?", "", text)
        text = re.sub(r"\n?```$", "", text.rstrip())
    return text.strip()


def init_db():
    conn = get_conn()
    c = conn.cursor()

    c.execute("""
        CREATE TABLE IF NOT EXISTS users (
            id         SERIAL PRIMARY KEY,
            name       TEXT NOT NULL UNIQUE,
            race_date  TEXT,
            profile_data JSONB
        )
    """)

    seed_users = [
        ("Cristian", {"sex": "male",   "age": 21, "weight_kg": 64, "level": "beginner"}),
        ("Adrien",   {"sex": "male",   "age": 43, "weight_kg": 77, "level": "amateur"}),
        ("Laurine",  {"sex": "female", "age": 23, "weight_kg": 53, "level": "amateur"}),
    ]
    for name, profile in seed_users:
        c.execute("""
            INSERT INTO users (name, profile_data)
            VALUES (%s, %s)
            ON CONFLICT (name) DO UPDATE SET profile_data = EXCLUDED.profile_data
        """, (name, json.dumps(profile)))

    c.execute("""
        CREATE TABLE IF NOT EXISTS runs (
            id               SERIAL PRIMARY KEY,
            user_id          INTEGER NOT NULL REFERENCES users(id),
            date             TEXT NOT NULL,
            type             TEXT NOT NULL DEFAULT 'run',
            distance         REAL NOT NULL DEFAULT 0,
            time_seconds     INTEGER NOT NULL DEFAULT 0,
            pace             TEXT NOT NULL DEFAULT '-',
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

    c.execute("""
        CREATE TABLE IF NOT EXISTS training_sessions (
            id           SERIAL PRIMARY KEY,
            user_id      INTEGER REFERENCES users(id),
            created_at   TIMESTAMP DEFAULT NOW(),
            plan         JSONB,
            template_key TEXT,
            status       TEXT DEFAULT 'pending'
        )
    """)

    c.execute("""
        CREATE TABLE IF NOT EXISTS session_results (
            id             SERIAL PRIMARY KEY,
            session_id     INTEGER REFERENCES training_sessions(id),
            exercise_index INTEGER,
            label          TEXT,
            time_seconds   INTEGER,
            notes          TEXT
        )
    """)

    c.execute("""
        CREATE TABLE IF NOT EXISTS chat_history (
            id         SERIAL PRIMARY KEY,
            user_id    INTEGER REFERENCES users(id),
            role       TEXT NOT NULL,
            content    TEXT NOT NULL,
            created_at TIMESTAMP DEFAULT NOW()
        )
    """)

    c.execute("""
        CREATE TABLE IF NOT EXISTS run_reps (
            id           SERIAL PRIMARY KEY,
            run_id       INTEGER REFERENCES runs(id) ON DELETE CASCADE,
            rep_number   INTEGER NOT NULL,
            distance_m   INTEGER,
            time_seconds INTEGER,
            notes        TEXT
        )
    """)

    c.execute("""
        CREATE TABLE IF NOT EXISTS user_goals (
            id             SERIAL PRIMARY KEY,
            user_id        INTEGER REFERENCES users(id) ON DELETE CASCADE,
            distance_key   TEXT NOT NULL,
            target_seconds INTEGER NOT NULL,
            basis          TEXT,
            calculated_at  TIMESTAMP DEFAULT NOW(),
            UNIQUE(user_id, distance_key)
        )
    """)

    # Migrations for existing deployments
    c.execute("ALTER TABLE users ADD COLUMN IF NOT EXISTS race_date TEXT")
    c.execute("ALTER TABLE users ADD COLUMN IF NOT EXISTS profile_data JSONB")
    c.execute("ALTER TABLE run_reps ALTER COLUMN time_seconds TYPE REAL USING time_seconds::REAL")

    conn.commit()
    c.close()
    conn.close()


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
    c.execute("SELECT id, name, race_date, profile_data FROM users ORDER BY id")
    rows = c.fetchall()
    c.close(); conn.close()
    return jsonify([dict(r) for r in rows])


@app.route("/api/user/<int:user_id>", methods=["DELETE"])
def api_delete_user(user_id):
    conn = get_conn()
    c = conn.cursor()
    c.execute("DELETE FROM session_results WHERE session_id IN (SELECT id FROM training_sessions WHERE user_id = %s)", (user_id,))
    c.execute("DELETE FROM training_sessions WHERE user_id = %s", (user_id,))
    c.execute("DELETE FROM runs WHERE user_id = %s", (user_id,))
    c.execute("DELETE FROM cooper_tests WHERE user_id = %s", (user_id,))
    c.execute("DELETE FROM users WHERE id = %s", (user_id,))
    conn.commit(); c.close(); conn.close()
    return jsonify({"message": "Deleted"})


# ─────────────────────────────────────────
# RUNS
# ─────────────────────────────────────────

def _pace_to_seconds(pace_str):
    if not pace_str or pace_str == "-":
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

    interval_rows = [(i, r) for i, r in enumerate(rows)
                     if r["type"] == "intervals" and r.get("interval_pace")]
    for j, (i, r) in enumerate(interval_rows):
        if j + 1 < len(interval_rows):
            _, prev = interval_rows[j + 1]
            curr_s = _pace_to_seconds(r["interval_pace"])
            prev_s = _pace_to_seconds(prev["interval_pace"])
            if curr_s and prev_s:
                if curr_s < prev_s:
                    rows[i]["interval_trend"] = "up"
                elif curr_s > prev_s:
                    rows[i]["interval_trend"] = "down"
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
    pace = "-"
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
        int(d["effort"]), d.get("notes", ""),
        d.get("interval_dist"), d.get("interval_reps"), d.get("interval_pace"),
        d.get("circuit_rounds"), d.get("circuit_details"), d.get("technique_drills")
    ))
    new_id = c.fetchone()["id"]
    conn.commit(); c.close(); conn.close()

    try:
        import threading
        uid = int(d["user_id"])
        def bg_recalc():
            with app.app_context():
                recalculate_goals(uid)
        threading.Thread(target=bg_recalc, daemon=True).start()
    except Exception:
        pass

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

def _build_stats(user_id):
    conn = get_conn()
    c = conn.cursor()

    c.execute("SELECT * FROM runs WHERE user_id = %s ORDER BY date DESC", (user_id,))
    runs = [dict(r) for r in c.fetchall()]

    c.execute("SELECT race_date, profile_data FROM users WHERE id = %s", (user_id,))
    user_row = c.fetchone()

    c.execute("SELECT vo2max FROM cooper_tests WHERE user_id = %s ORDER BY date DESC LIMIT 1", (user_id,))
    cooper_row = c.fetchone()

    c.execute("""
        SELECT ts.id, ts.created_at, ts.status, ts.plan
        FROM training_sessions ts
        WHERE ts.user_id = %s
        ORDER BY ts.created_at DESC
        LIMIT 3
    """, (user_id,))
    recent_sessions = [dict(r) for r in c.fetchall()]

    c.close(); conn.close()

    today = date.today()

    date_set = set(datetime.strptime(r["date"], "%Y-%m-%d").date() for r in runs)
    check = today if today in date_set else today - timedelta(days=1)
    streak = 0
    while check in date_set:
        streak += 1
        check -= timedelta(days=1)

    last_session_days = None
    last_session_alert = False
    if runs:
        last_date = datetime.strptime(runs[0]["date"], "%Y-%m-%d").date()
        last_session_days = (today - last_date).days
        last_session_alert = last_session_days > 5

    week_start = today - timedelta(days=today.weekday())
    prev_week_start = week_start - timedelta(days=7)

    week_sessions = 0; week_km = 0.0
    prev_week_sessions = 0; prev_week_km = 0.0
    total_load_sum = 0.0; week_load_sum = 0.0
    for r in runs:
        rd = datetime.strptime(r["date"], "%Y-%m-%d").date()
        if rd >= week_start:
            week_sessions += 1
            week_km += r["distance"]
        elif rd >= prev_week_start:
            prev_week_sessions += 1
            prev_week_km += r["distance"]
        effort = r.get("effort") or 0
        t_sec = r.get("time_seconds") or 0
        if effort and t_sec:
            load_pts = effort * t_sec / 60.0
            total_load_sum += load_pts
            if rd >= week_start:
                week_load_sum += load_pts
    week_km = round(week_km, 2)
    prev_week_km = round(prev_week_km, 2)
    total_load = round(total_load_sum)
    week_load = round(week_load_sum)

    running = [r for r in runs if r["distance"] > 0]
    total_km = round(sum(r["distance"] for r in running), 2)
    total_runs = len(runs)
    avg_pace = best_pace = "-"
    proj_10k = None

    timed = [r for r in running if r["time_seconds"] > 0]
    if timed:
        total_sec = sum(r["time_seconds"] for r in timed)
        total_dist = sum(r["distance"] for r in timed)
        avg_s = total_sec / total_dist if total_dist > 0 else 0
        avg_pace = f"{int(avg_s//60)}:{int(avg_s%60):02d}"
        best = min(timed, key=lambda r: r["time_seconds"] / r["distance"])
        best_pace = best["pace"]
        parts = best_pace.split(":")
        pace_s = int(parts[0]) * 60 + int(parts[1])
        total_p = pace_s * 10
        proj_10k = f"{total_p//60}:{total_p%60:02d}"

    # Last 5 sessions avg pace
    last5 = [r for r in timed[:5]]
    avg_pace_last5 = "-"
    if last5:
        s5 = sum(r["time_seconds"] for r in last5)
        d5 = sum(r["distance"] for r in last5)
        if d5 > 0:
            a5 = s5 / d5
            avg_pace_last5 = f"{int(a5//60)}:{int(a5%60):02d}"

    race_date_str = user_row["race_date"] if user_row else None
    days_to_race = None
    total_planned = None
    completed_pct = None
    if race_date_str:
        try:
            race_dt = datetime.strptime(race_date_str, "%Y-%m-%d").date()
            days_to_race = (race_dt - today).days
        except Exception:
            pass

    race_progress = None
    vo2max = None
    if cooper_row:
        vo2max = float(cooper_row["vo2max"])
        race_progress = min(100, max(0, int((vo2max / 52) * 100)))
    if not vo2max and best_pace and best_pace != "-":
        try:
            parts = best_pace.split(":")
            pace_secs = int(parts[0]) * 60 + int(parts[1])
            speed_ms = 1000 / pace_secs
            vo2max = round(speed_ms * 210.938 - 48.673, 1)
            if vo2max < 10 or vo2max > 85:
                vo2max = None
        except Exception:
            pass

    # Completed sessions pct: completed / total training_sessions
    if days_to_race is not None and days_to_race > 0:
        conn2 = get_conn()
        c2 = conn2.cursor()
        c2.execute("SELECT COUNT(*) as total FROM training_sessions WHERE user_id = %s", (user_id,))
        total_row = c2.fetchone()
        c2.execute("SELECT COUNT(*) as done FROM training_sessions WHERE user_id = %s AND status = 'completed'", (user_id,))
        done_row = c2.fetchone()
        c2.close(); conn2.close()
        total_planned = total_row["total"] if total_row else 0
        done = done_row["done"] if done_row else 0
        completed_pct = int((done / total_planned * 100) if total_planned > 0 else 0)

    # Format recent sessions for display
    recent_formatted = []
    for s in recent_sessions:
        recent_formatted.append({
            "id": s["id"],
            "created_at": s["created_at"].strftime("%Y-%m-%d") if s["created_at"] else None,
            "status": s["status"],
            "exercise_count": len(s["plan"]) if s["plan"] else 0,
        })

    # Derived metrics
    total_laps = round(total_km * 1000 / 400, 1) if total_km else 0

    recovery_hours = None
    if runs:
        last = runs[0]
        effort = last.get("effort") or 0
        t_sec = last.get("time_seconds") or 0
        if effort and t_sec:
            recovery_hours = round((effort / 10) * (t_sec / 3600) * 48)
            recovery_hours = max(12, min(72, recovery_hours))

    # Advanced stats
    total_time_seconds = sum(r["time_seconds"] for r in runs)
    profile = (user_row["profile_data"] or {}) if user_row else {}
    weight_kg = profile.get("weight_kg", 70)
    weekly_goal = profile.get("weekly_goal") or None
    total_hours = total_time_seconds / 3600
    calories_burned = round(8.0 * weight_kg * total_hours)
    estimates = {
        "100m":  profile.get("est_100m_sec"),
        "400m":  profile.get("est_400m_sec"),
        "1km":   profile.get("est_1km_sec"),
        "5km":   profile.get("est_5km_sec"),
        "10km":  profile.get("est_10km_sec"),
    }

    # Best rep across all sessions
    conn3 = get_conn()
    c3 = conn3.cursor()
    c3.execute("""
        SELECT rr.distance_m, MIN(rr.time_seconds) as best_time
        FROM run_reps rr
        JOIN runs r ON r.id = rr.run_id
        WHERE r.user_id = %s AND rr.time_seconds > 0
        GROUP BY rr.distance_m
        ORDER BY MIN(rr.time_seconds) ASC
        LIMIT 1
    """, (user_id,))
    best_rep_row = c3.fetchone()
    best_rep = dict(best_rep_row) if best_rep_row else None
    c3.close(); conn3.close()

    return {
        "total_km": total_km,
        "total_runs": total_runs,
        "avg_pace": avg_pace,
        "best_pace": best_pace,
        "avg_pace_last5": avg_pace_last5,
        "weekly_km": week_km,
        "proj_10k": proj_10k,
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
        "total_planned": total_planned,
        "completed_pct": completed_pct,
        "recent_sessions": recent_formatted,
        "profile_data": user_row["profile_data"] if user_row else None,
        "total_time_seconds": total_time_seconds,
        "calories_burned": calories_burned,
        "estimates": estimates,
        "best_rep": best_rep,
        "total_laps": total_laps,
        "total_load": total_load,
        "week_load": week_load,
        "recovery_hours": recovery_hours,
        "vo2max": vo2max,
        "weekly_goal": weekly_goal,
    }


@app.route("/api/stats/<int:user_id>")
def api_stats(user_id):
    return jsonify(_build_stats(user_id))


@app.route("/api/user/<int:user_id>/stats")
def api_user_stats(user_id):
    return jsonify(_build_stats(user_id))


@app.route("/api/user/<int:user_id>/activity-stats")
def activity_stats(user_id):
    try:
        conn = get_conn()
        c = conn.cursor()

        c.execute("""
            SELECT date, type, distance, time_seconds, effort, pace
            FROM runs WHERE user_id = %s
            AND date::date >= CURRENT_DATE - INTERVAL '90 days'
            ORDER BY date ASC
        """, (user_id,))
        runs = [dict(r) for r in c.fetchall()]

        c.execute("SELECT profile_data FROM users WHERE id = %s", (user_id,))
        row = c.fetchone()
        profile_data = (row["profile_data"] or {}) if row else {}
        weight_kg = float(profile_data.get("weight_kg", 70))

        c.execute("""
            SELECT
                date_trunc('week', date::date) as week,
                COALESCE(SUM(distance), 0) as km,
                COUNT(*) as sessions,
                COALESCE(AVG(effort), 0) as avg_effort,
                COALESCE(SUM(time_seconds), 0) as total_time
            FROM runs WHERE user_id = %s
            AND date::date >= CURRENT_DATE - INTERVAL '84 days'
            GROUP BY date_trunc('week', date::date)
            ORDER BY date_trunc('week', date::date) ASC
        """, (user_id,))
        weekly = [dict(r) for r in c.fetchall()]

        for w in weekly:
            hours = float(w["total_time"]) / 3600
            w["calories"] = round(8 * weight_kg * hours)
            w["week"] = str(w["week"])[:10]

        c.execute("""
            SELECT rr.distance_m,
                   MIN(rr.time_seconds) as best,
                   AVG(rr.time_seconds) as avg,
                   COUNT(*) as count
            FROM run_reps rr
            JOIN runs r ON r.id = rr.run_id
            WHERE r.user_id = %s AND rr.time_seconds > 0
            GROUP BY rr.distance_m
            ORDER BY rr.distance_m
        """, (user_id,))
        rep_stats = [dict(r) for r in c.fetchall()]

        c.execute("""
            SELECT
                date_trunc('week', date::date) as week,
                COALESCE(SUM(effort * time_seconds / 60.0), 0) as load
            FROM runs WHERE user_id = %s
            AND date::date >= CURRENT_DATE - INTERVAL '84 days'
            GROUP BY date_trunc('week', date::date)
            ORDER BY date_trunc('week', date::date) ASC
        """, (user_id,))
        load_by_week = [{"week": str(r["week"])[:10], "load": round(float(r["load"]))} for r in c.fetchall()]

        c.close(); conn.close()
        return jsonify({
            "runs": runs,
            "weekly": weekly,
            "rep_stats": rep_stats,
            "load_by_week": load_by_week,
        })
    except Exception as e:
        import traceback
        return jsonify({"error": str(e), "traceback": traceback.format_exc()}), 500


# ─────────────────────────────────────────
# RUNS HISTORY / EDIT / DELETE
# ─────────────────────────────────────────

@app.route("/api/user/<int:user_id>/runs")
def get_user_runs(user_id):
    conn = get_conn()
    c = conn.cursor()
    c.execute("""
        SELECT id, date, type, distance, time_seconds, pace, effort, notes
        FROM runs WHERE user_id = %s
        ORDER BY date DESC, id DESC
        LIMIT 100
    """, (user_id,))
    runs = [dict(r) for r in c.fetchall()]

    if runs:
        run_ids = [r["id"] for r in runs]
        c.execute("""
            SELECT run_id, rep_number, distance_m, time_seconds, notes
            FROM run_reps WHERE run_id = ANY(%s)
            ORDER BY run_id, rep_number
        """, (run_ids,))
        reps_by_run = {}
        for rep in c.fetchall():
            reps_by_run.setdefault(rep["run_id"], []).append(dict(rep))
        for run in runs:
            run["reps"] = reps_by_run.get(run["id"], [])

    c.close(); conn.close()
    return jsonify(runs)


@app.route("/api/runs/<int:run_id>", methods=["PUT"])
def update_run(run_id):
    data = request.get_json(force=True, silent=True) or {}
    conn = get_conn()
    c = conn.cursor()

    distance = data.get("distance")
    time_seconds = data.get("time_seconds")
    pace = None
    if distance and time_seconds and float(distance) > 0:
        pace_secs = float(time_seconds) / float(distance)
        pace = f"{int(pace_secs//60)}:{int(pace_secs%60):02d}"

    c.execute("""
        UPDATE runs SET
            date = %s, type = %s, distance = %s,
            time_seconds = %s, pace = %s, effort = %s, notes = %s
        WHERE id = %s
    """, (
        data.get("date"), data.get("type"), distance,
        time_seconds, pace, data.get("effort"), data.get("notes"),
        run_id
    ))
    conn.commit()
    c.close(); conn.close()
    return jsonify({"ok": True})


@app.route("/api/runs/<int:run_id>", methods=["DELETE"])
def delete_run(run_id):
    conn = get_conn()
    c = conn.cursor()
    c.execute("DELETE FROM run_reps WHERE run_id = %s", (run_id,))
    c.execute("DELETE FROM runs WHERE id = %s", (run_id,))
    conn.commit()
    c.close(); conn.close()
    return jsonify({"ok": True})


# ─────────────────────────────────────────
# COOPER
# ─────────────────────────────────────────

def _cooper_calc(distance_m):
    vo2max = round((distance_m - 504.9) / 44.73, 1)

    if vo2max < 28:   level = "Very Poor"
    elif vo2max < 34: level = "Poor"
    elif vo2max < 42: level = "Average"
    elif vo2max < 52: level = "Good"
    elif vo2max < 60: level = "Excellent"
    else:             level = "Superior"

    try:
        pace_min_km = 29.54 / (vo2max ** 0.5765)
        total_min = pace_min_km * 10
        mins = int(total_min)
        secs = int(round((total_min - mins) * 60))
        if secs == 60:
            mins += 1
            secs = 0
        proj_10k = f"{mins}:{secs:02d}"
    except Exception:
        proj_10k = "-"

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
    """, (int(d["user_id"]), d["date"], float(d["distance_m"]), vo2max, level, proj_10k, d.get("notes", "")))
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


# ─────────────────────────────────────────
# AI CHAT
# ─────────────────────────────────────────

def _extract_plan(text):
    match = re.search(r'\{"plan"\s*:\s*\[.*?\]\}', text, re.DOTALL)
    if match:
        try:
            return json.loads(match.group(0))
        except Exception:
            pass
    return None


def _extract_profile(text):
    match = re.search(r'\{"profile"\s*:\s*\{.*?\}\}', text, re.DOTALL)
    if match:
        try:
            return json.loads(match.group(0))
        except Exception:
            pass
    return None


def _extract_run(text):
    match = re.search(r'\{"run"\s*:\s*\{.*?\}\}', text, re.DOTALL)
    if match:
        try:
            return json.loads(match.group(0))
        except Exception:
            pass
    return None


def _template_key(plan):
    """Generate a deterministic key for a plan structure."""
    if not plan:
        return None
    labels = [item.get("type", "") for item in plan]
    return "_".join(labels)


@app.route("/api/chat", methods=["POST"])
def api_chat():
    d = request.get_json()
    user_id = d.get("user_id")
    message = d.get("message", "").strip()

    if not user_id or not message:
        return jsonify({"error": "user_id and message required"}), 400

    conn = get_conn()
    c = conn.cursor()

    c.execute("SELECT name, profile_data FROM users WHERE id = %s", (user_id,))
    user_row = c.fetchone()
    if not user_row:
        c.close(); conn.close()
        return jsonify({"error": "User not found"}), 404

    profile_data = user_row["profile_data"] or {}
    user_name = user_row["name"]

    c.execute("""
        SELECT date, type, distance, pace, effort, notes, interval_dist, interval_reps, interval_pace
        FROM runs WHERE user_id = %s ORDER BY date DESC LIMIT 10
    """, (user_id,))
    recent_runs = [dict(r) for r in c.fetchall()]
    c.close(); conn.close()

    history_lines = []
    for r in recent_runs:
        line = f"{r['date']} | {r['type']}"
        if r["distance"]:
            line += f" | {r['distance']}km"
        if r["pace"] and r["pace"] != "-":
            line += f" | pace {r['pace']}/km"
        if r["effort"]:
            line += f" | RPE {r['effort']}"
        history_lines.append(line)
    history_str = "\n".join(history_lines) if history_lines else "No sessions recorded yet."

    profile_str = json.dumps(profile_data) if profile_data else "null"

    system_prompt = f"""You are a personalized running coach. No emojis. Concise responses. Always reply in the same language the athlete writes in.
Athlete: {user_name}

If the athlete has no saved profile (profile_data is null or empty), your first task is to collect these fields one by one in the conversation:
- Age
- Weight in kg
- Current level (beginner / intermediate / advanced)
- Main goal (complete 10k / improve time / lose weight / general endurance)
- Days available per week to train

Once you have all that data, calculate estimated times based on standard performance tables and return at the end of your message this exact JSON:
{{"profile": {{"age": 28, "weight_kg": 70, "level": "intermediate", "goal": "complete 10k", "days_per_week": 4, "est_100m_sec": 18, "est_400m_sec": 95, "est_1km_sec": 280, "est_5km_sec": 1500, "est_10km_sec": 3200}}}}

After onboarding, use the profile and history to generate personalized plans.
When generating a training plan return at the end of your message this exact JSON:
{{"plan": [{{"label": "Warm-up", "type": "warmup"}}, {{"label": "Series 1 - 400m", "type": "interval"}}]}}

Valid types for the plan are: warmup, lap, interval, rest, cooldown, series, drill.

When the athlete tells you they completed a run (mentions distance, time, or how a run went), extract the key data and return at the end of your message this exact JSON:
{{"run": {{"date": "2025-01-15", "type": "run", "distance": 5.0, "time_seconds": 1800, "effort": 7, "notes": "felt good"}}}}
Valid types: run, intervals, circuit, technique, race. Use today's date if no date is mentioned. time_seconds is total run time as integer (0 if unknown). distance is in km (0 if unknown). effort is 1-10 RPE.

Never put JSON blocks in the middle of text, always at the end.
Do not use emojis anywhere in your response.

Athlete history:
{history_str}

Current profile: {profile_str}"""

    # Build full conversation history for context
    conn2 = get_conn()
    c2 = conn2.cursor()
    c2.execute("""
        SELECT role, content FROM chat_history
        WHERE user_id = %s
        ORDER BY created_at ASC
        LIMIT 20
    """, (user_id,))
    history_rows = c2.fetchall()

    messages = [{"role": r["role"], "content": r["content"]} for r in history_rows]
    messages.append({"role": "user", "content": message})

    try:
        client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)
        response = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=1024,
            system=system_prompt,
            messages=messages
        )
        ai_text = response.content[0].text
    except Exception as e:
        c2.close(); conn2.close()
        return jsonify({"error": f"AI error: {str(e)}"}), 500

    plan_json = _extract_plan(ai_text)
    profile_json = _extract_profile(ai_text)
    run_json = _extract_run(ai_text)
    clean_text = ai_text
    session_id = None

    # Strip JSON blocks from display text (always at end, strip greedily)
    clean_text = re.sub(r'\{"plan"\s*:\s*\[.*?\]\}', '', clean_text, flags=re.DOTALL).strip()
    clean_text = re.sub(r'\{"profile"\s*:\s*\{.*?\}\}', '', clean_text, flags=re.DOTALL).strip()
    clean_text = re.sub(r'\{"run"\s*:\s*\{.*?\}\}', '', clean_text, flags=re.DOTALL).strip()

    if plan_json and plan_json.get("plan"):
        plan = plan_json["plan"]
        t_key = _template_key(plan)
        c2.execute("""
            INSERT INTO training_sessions (user_id, plan, template_key, status)
            VALUES (%s, %s, %s, 'pending') RETURNING id
        """, (user_id, json.dumps(plan), t_key))
        session_id = c2.fetchone()["id"]

    if profile_json and profile_json.get("profile"):
        c2.execute(
            "UPDATE users SET profile_data = %s WHERE id = %s",
            (json.dumps(profile_json["profile"]), user_id)
        )

    if run_json and run_json.get("run"):
        r = run_json["run"]
        distance = float(r.get("distance", 0))
        time_sec = int(r.get("time_seconds", 0))
        pace = "-"
        if distance > 0 and time_sec > 0:
            ps = time_sec / distance
            pace = f"{int(ps//60)}:{int(ps%60):02d}"
        run_date = r.get("date") or date.today().strftime("%Y-%m-%d")
        c2.execute("""
            INSERT INTO runs (user_id, date, type, distance, time_seconds, pace, effort, notes)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
        """, (
            user_id, run_date, r.get("type", "run"), distance, time_sec, pace,
            int(r.get("effort", 5)), r.get("notes", "")
        ))

    c2.execute(
        "INSERT INTO chat_history (user_id, role, content) VALUES (%s, %s, %s)",
        (user_id, 'user', message)
    )
    c2.execute(
        "INSERT INTO chat_history (user_id, role, content) VALUES (%s, %s, %s)",
        (user_id, 'assistant', clean_text)
    )
    conn2.commit(); c2.close(); conn2.close()

    return jsonify({
        "text": clean_text,
        "plan": plan_json["plan"] if plan_json else None,
        "session_id": session_id,
        "profile_saved": profile_json is not None,
        "run_saved": run_json is not None,
    })


@app.route("/api/chat-history/<int:user_id>")
def api_chat_history(user_id):
    conn = get_conn()
    c = conn.cursor()
    c.execute("""
        SELECT role, content, created_at
        FROM chat_history
        WHERE user_id = %s
        ORDER BY created_at ASC
        LIMIT 50
    """, (user_id,))
    rows = c.fetchall()
    c.close(); conn.close()
    return jsonify([
        {"role": r["role"], "content": r["content"], "created_at": str(r["created_at"])}
        for r in rows
    ])


@app.route("/api/training-session/<int:session_id>/results")
def api_get_session_results(session_id):
    conn = get_conn()
    c = conn.cursor()
    c.execute("SELECT plan, status FROM training_sessions WHERE id = %s", (session_id,))
    session_row = c.fetchone()
    if not session_row:
        c.close(); conn.close()
        return jsonify({"error": "Session not found"}), 404
    c.execute("""
        SELECT exercise_index, label, time_seconds
        FROM session_results
        WHERE session_id = %s
        ORDER BY exercise_index
    """, (session_id,))
    results = [dict(r) for r in c.fetchall()]
    c.close(); conn.close()
    return jsonify({
        "plan": session_row["plan"] or [],
        "status": session_row["status"],
        "results": results,
    })


# ─────────────────────────────────────────
# SESSION RESULTS
# ─────────────────────────────────────────

@app.route("/api/session-results", methods=["POST"])
def api_session_results():
    d = request.get_json()
    session_id = d.get("session_id")
    results = d.get("results", [])

    if not session_id:
        return jsonify({"error": "session_id required"}), 400

    conn = get_conn()
    c = conn.cursor()

    for i, r in enumerate(results):
        time_seconds = r.get("time_seconds") or 0

        c.execute("""
            INSERT INTO session_results (session_id, exercise_index, label, time_seconds, notes)
            VALUES (%s, %s, %s, %s, %s)
        """, (session_id, i, r.get("label", ""), time_seconds, r.get("notes", "")))

    c.execute("UPDATE training_sessions SET status = 'completed' WHERE id = %s", (session_id,))
    conn.commit(); c.close(); conn.close()

    return jsonify({"message": "Results saved", "session_id": session_id}), 201


# ─────────────────────────────────────────
# REPS
# ─────────────────────────────────────────

@app.route("/api/runs/<int:run_id>/reps", methods=["POST"])
def save_reps(run_id):
    data = request.get_json()
    reps = data.get("reps", [])
    conn = get_conn()
    c = conn.cursor()
    c.execute("DELETE FROM run_reps WHERE run_id = %s", (run_id,))
    for rep in reps:
        c.execute("""
            INSERT INTO run_reps (run_id, rep_number, distance_m, time_seconds, notes)
            VALUES (%s, %s, %s, %s, %s)
        """, (run_id, rep["rep_number"], rep.get("distance_m"), rep.get("time_seconds"), rep.get("notes", "")))
    conn.commit()
    c.close(); conn.close()
    return jsonify({"ok": True})


@app.route("/api/runs/<int:run_id>/reps")
def get_reps(run_id):
    conn = get_conn()
    c = conn.cursor()
    c.execute("SELECT * FROM run_reps WHERE run_id = %s ORDER BY rep_number", (run_id,))
    rows = [dict(r) for r in c.fetchall()]
    c.close(); conn.close()
    return jsonify(rows)


# ─────────────────────────────────────────
# GOALS (dynamic, AI-calculated)
# ─────────────────────────────────────────

@app.route("/api/user/<int:user_id>/recalculate-goals", methods=["POST"])
def recalculate_goals(user_id):
    conn = get_conn()
    c = conn.cursor()

    c.execute("SELECT profile_data FROM users WHERE id = %s", (user_id,))
    row = c.fetchone()
    profile = dict(row["profile_data"]) if row and row["profile_data"] else {}

    c.execute("""
        SELECT r.id, r.type, r.distance, r.time_seconds, r.date
        FROM runs r WHERE r.user_id = %s
        ORDER BY r.date DESC LIMIT 10
    """, (user_id,))
    recent_runs = [dict(r) for r in c.fetchall()]

    run_ids = [r["id"] for r in recent_runs]
    reps_by_run = {}
    if run_ids:
        c.execute("SELECT * FROM run_reps WHERE run_id = ANY(%s)", (run_ids,))
        for rep in c.fetchall():
            reps_by_run.setdefault(rep["run_id"], []).append(dict(rep))

    standards = get_standard_for_user(profile)

    try:
        client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)
        prompt = f"""You are a running performance analyst. No emojis. Respond only with valid JSON.

Athlete profile: {json.dumps(profile)}
World Athletics standard for this athlete: {json.dumps(standards)}
Recent sessions: {json.dumps(recent_runs)}
Recent reps: {json.dumps(reps_by_run)}

Based on the athlete's recent performance, calculate realistic improvement goals.
For each distance the athlete has trained, set a target that is 3-5% better than their recent best.
If no data exists for a distance, use the World Athletics standard as the goal.

Respond with ONLY this JSON, no other text:
{{"goals": {{"100m": 13, "400m": 58, "800m": 155, "1mile": 340, "5km": 1200, "10km": 2500}}}}

Only include distances where you have data or a standard. All values in seconds."""

        response = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=300,
            messages=[{"role": "user", "content": prompt}]
        )
        goals_json = json.loads(_strip_fences(response.content[0].text))
        goals = goals_json.get("goals", {})
        for dist_key, target_secs in goals.items():
            c.execute("""
                INSERT INTO user_goals (user_id, distance_key, target_seconds, basis, calculated_at)
                VALUES (%s, %s, %s, 'ai_calculated', NOW())
                ON CONFLICT (user_id, distance_key)
                DO UPDATE SET target_seconds = EXCLUDED.target_seconds,
                              basis = 'ai_calculated',
                              calculated_at = NOW()
            """, (user_id, dist_key, int(target_secs)))
        conn.commit()
    except Exception:
        pass

    c.close(); conn.close()
    return jsonify({"ok": True})


@app.route("/api/user/<int:user_id>/goals")
def get_user_goals(user_id):
    conn = get_conn()
    c = conn.cursor()

    c.execute("SELECT profile_data FROM users WHERE id = %s", (user_id,))
    row = c.fetchone()
    profile = dict(row["profile_data"]) if row and row["profile_data"] else {}
    standards = get_standard_for_user(profile)

    c.execute("SELECT distance_key, target_seconds, basis FROM user_goals WHERE user_id = %s", (user_id,))
    goals = {r["distance_key"]: {"target": r["target_seconds"], "basis": r["basis"]} for r in c.fetchall()}

    c.execute("""
        SELECT rr.distance_m, MIN(rr.time_seconds) as best
        FROM run_reps rr
        JOIN runs r ON r.id = rr.run_id
        WHERE r.user_id = %s AND rr.time_seconds IS NOT NULL AND rr.time_seconds > 0
        GROUP BY rr.distance_m
    """, (user_id,))
    dist_map = {100: "100m", 400: "400m", 800: "800m", 1609: "1mile", 5000: "5km", 10000: "10km"}
    bests = {dist_map[r["distance_m"]]: r["best"] for r in c.fetchall() if r["distance_m"] in dist_map}

    c.close(); conn.close()
    return jsonify({"goals": goals, "standards": standards, "bests": bests})


# ─────────────────────────────────────────
# AI SESSION PARSER
# ─────────────────────────────────────────

@app.route("/api/parse-session", methods=["POST"])
def parse_session():
    data = request.get_json(force=True, silent=True) or {}
    text = data.get("text", "")
    user_id = data.get("user_id")

    if not text:
        return jsonify({"error": "No text provided"}), 400

    client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)

    prompt = f"""You are a running session parser. No emojis. Respond ONLY with valid JSON.

The athlete described their training session in natural language. Extract all structured data.

Session description: "{text}"

Respond with ONLY this JSON structure, no other text:
{{
  "type": "run|intervals|circuit|technique",
  "date": "YYYY-MM-DD or null",
  "distance_km": 8.5,
  "time_seconds": 2700,
  "effort": 7,
  "notes": "brief summary of the session",
  "reps": [
    {{"rep_number": 1, "distance_m": 150, "time_seconds": 28}},
    {{"rep_number": 2, "distance_m": 150, "time_seconds": 29}}
  ],
  "blocks": [
    {{"label": "Warmup", "duration_min": 12, "notes": "2 laps easy + mobility"}},
    {{"label": "Fartlek", "laps": 4, "notes": "100m fast / 100m easy alternating"}},
    {{"label": "Quality block", "reps": 6, "distance_m": 150, "notes": "85-90% effort, 1 min rest"}}
  ]
}}

Rules:
- type: use 'intervals' if there are series/reps, 'technique' if drills, 'circuit' if mixed, 'run' if continuous
- If a value is unknown or not mentioned, use null
- reps array: only include if there are specific timed repetitions with known times
- blocks array: always include all workout blocks even if no specific times
- date: use today {str(date.today())} if not specified
- distance_km: estimate total distance if possible, null if unknown
- time_seconds: total session time if mentioned, null if unknown
- effort: estimate 1-10 based on described intensity, default 6 if unclear
- notes: write in the same language the athlete used"""

    try:
        response = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=800,
            messages=[{"role": "user", "content": prompt}]
        )
        parsed = json.loads(_strip_fences(response.content[0].text))
        return jsonify({"ok": True, "parsed": parsed})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/user/<int:user_id>/profile", methods=["PUT"])
def api_update_profile(user_id):
    d = request.get_json()
    conn = get_conn()
    c = conn.cursor()
    c.execute("SELECT profile_data FROM users WHERE id = %s", (user_id,))
    row = c.fetchone()
    if not row:
        c.close(); conn.close()
        return jsonify({"error": "User not found"}), 404
    existing = dict(row["profile_data"]) if row["profile_data"] else {}
    existing.update(d)
    c.execute("UPDATE users SET profile_data = %s WHERE id = %s", (json.dumps(existing), user_id))
    conn.commit(); c.close(); conn.close()
    return jsonify({"profile_data": existing})


@app.route("/api/training-sessions/<int:user_id>")
def api_get_training_sessions(user_id):
    conn = get_conn()
    c = conn.cursor()
    c.execute("""
        SELECT ts.id, ts.created_at, ts.plan, ts.template_key, ts.status
        FROM training_sessions ts
        WHERE ts.user_id = %s
        ORDER BY ts.created_at DESC
        LIMIT 20
    """, (user_id,))
    rows = []
    for r in c.fetchall():
        row = dict(r)
        row["created_at"] = row["created_at"].strftime("%Y-%m-%d %H:%M") if row["created_at"] else None
        rows.append(row)
    c.close(); conn.close()
    return jsonify(rows)


# ─────────────────────────────────────────
# WEEKLY PLAN
# ─────────────────────────────────────────

@app.route("/api/weekly-plan/<int:user_id>")
def api_get_weekly_plan(user_id):
    conn = get_conn()
    c = conn.cursor()
    c.execute("""
        SELECT id, plan, created_at FROM training_sessions
        WHERE user_id = %s AND template_key = 'weekly_plan' AND status = 'active'
        ORDER BY created_at DESC LIMIT 1
    """, (user_id,))
    row = c.fetchone()
    c.close(); conn.close()
    if not row:
        return jsonify({"ok": True, "plan": None})
    return jsonify({
        "ok": True,
        "plan": row["plan"],
        "plan_id": row["id"],
        "created_at": str(row["created_at"])[:10]
    })


def _build_partners_section(partners_profiles):
    """Build the TRAINING PARTNERS system prompt section from a list of partner profile dicts."""
    if not partners_profiles:
        return ""
    lines = []
    names = [p.get("name", "?") for p in partners_profiles]
    lines.append(f"\nTRAINING PARTNERS THIS WEEK: {', '.join(names)}")
    for p in partners_profiles:
        p_name = p.get("name", "Unknown")
        p_data = p.get("profile_data") or {}
        p_coaching = USER_COACHING_PROFILES.get(p_name, {
            "level_description": p_data.get("level", "intermediate"),
            "coaching_style": "balanced and supportive",
            "push_factor": "moderate",
            "special_notes": "",
        })
        lines.append(
            f"  - {p_name}: age {p_data.get('age','?')}, {p_data.get('weight_kg','?')} kg | "
            f"level: {p_coaching['level_description']} | "
            f"push: {p_coaching['push_factor']} | "
            f"notes: {p_coaching.get('special_notes','')}"
        )
    lines.append("""
RELATIVE ADAPTATION RULES (shared-training week):
- Athletes train the same days and same general objective.
- BUT each plan MUST respect the individual level, age, and coaching style.
- For sessions done together on the track:
  * Give each athlete their OWN target pace in sec/lap.
  * The faster athlete leads, the slower has their own zone.
  * Example in main_block: "Adrien: 1:45/lap — Cristian: 2:10/lap, même circuit."
- For total weekly volume: do NOT equalize — respect each athlete's capacity.
  * Beginner (21 yo): reduce 20-30 % vs advanced athletes.
  * Female athlete: adjust for recovery cycle.
- RPE coherence: same perceived effort produces different paces per athlete.
  Keep RPE consistent across partners so they feel the same relative effort.
- In the tactical_note of EVERY shared session add exactly:
  "Session partagée avec [partner name(s)]: ton allure cible est X sec/lap, la leur est Y sec/lap. Même effort, pas le même temps."
""")
    return "\n".join(lines)


@app.route("/api/generate-weekly-plan", methods=["POST"])
def generate_weekly_plan():
    d = request.get_json()
    user_id           = d.get("user_id")
    available_days    = d.get("available_days", [])
    objetivo          = d.get("objetivo", "mejorar marca")
    intensidad        = d.get("intensidad", "normal")
    partners_profiles = d.get("partners_profiles", [])

    conn = get_conn(); c = conn.cursor()
    c.execute("SELECT name, profile_data FROM users WHERE id = %s", (user_id,))
    user_row = c.fetchone()
    if not user_row:
        c.close(); conn.close()
        return jsonify({"error": "User not found"}), 404

    user_name    = user_row["name"]
    profile_data = user_row["profile_data"] or {}

    c.execute("""
        SELECT date, type, distance, time_seconds, pace, effort, notes
        FROM runs WHERE user_id = %s ORDER BY date DESC LIMIT 15
    """, (user_id,))
    recent_runs = [dict(r) for r in c.fetchall()]

    c.execute("""
        SELECT rr.distance_m, MIN(rr.time_seconds) as best
        FROM run_reps rr JOIN runs r ON r.id = rr.run_id
        WHERE r.user_id = %s AND rr.time_seconds > 0
        GROUP BY rr.distance_m ORDER BY rr.distance_m
    """, (user_id,))
    best_reps = [dict(r) for r in c.fetchall()]

    c.execute("SELECT distance_key, target_seconds FROM user_goals WHERE user_id = %s", (user_id,))
    goals = {r["distance_key"]: r["target_seconds"] for r in c.fetchall()}

    c.close(); conn.close()

    coaching = USER_COACHING_PROFILES.get(user_name, {
        "language": "english",
        "coaching_style": "balanced and supportive",
        "level_description": profile_data.get("level", "intermediate"),
        "push_factor": "moderate",
        "special_notes": "",
    })
    lang = coaching["language"]

    system_prompt = f"""You are an elite athletics coach specialized in middle and long distance runners.

CRITICAL LANGUAGE RULE: Your ENTIRE response must be in {lang.upper()}. Every single field value in the JSON must be written in {lang.upper()}. Writing in any other language is a critical failure.

No emojis. Respond ONLY with valid JSON.

ATHLETE PROFILE:
- Name: {user_name}
- Age: {profile_data.get('age', '?')}
- Weight: {profile_data.get('weight_kg', '?')} kg
- Level: {coaching['level_description']}
- Coaching style: {coaching['coaching_style']}
- Push factor: {coaching['push_factor']}
- Special notes: {coaching['special_notes']}

PERFORMANCE DATA:
- Recent sessions: {json.dumps(recent_runs)}
- Best reps: {json.dumps(best_reps)}
- Current goals: {json.dumps(goals)}

AVAILABLE DAYS THIS WEEK: {json.dumps(available_days)}
WEEKLY OBJECTIVE: {objetivo}
WEEK INTENSITY: {intensidad}

UNIT AND SPECIFICITY RULES — MANDATORY:
- Express ALL distances in 400m track laps AND meters (never km only)
- Express ALL paces in sec/lap AND sec/100m AND min/km
- Express ALL heart rate zones as % max HR (never vague descriptions)
- Every warmup must include progressive lap times (lap 1: 2:30, lap 2: 2:20, etc.)
- Every cooldown is MANDATORY, minimum 2 easy laps + stretching cues
- Every interval series must include: reps × distance_m, target time/rep, recovery time, recovery pace

ONE SESSION = ONE OBJECTIVE:
- Never mix tempo with base endurance in the same main block
- Never mix speed work with aerobic base
- If multiple objectives exist, pick the priority one and state why

MANDATORY TRAINING STRUCTURE (adapt to available days):
1. LONG EASY RUN (1x/week): 18-22 laps, Zone 1-2 (65-72% MHR), progressive last 20-25%
2. VO2max INTERVALS (1x/week): 800m-1200m series at 90-95% MHR, 1:1 active recovery, full rep times
3. TEMPO/THRESHOLD (1x/week): 20-30 min continuous at 83-88% MHR, pace in sec/lap + sec/100m
4. FARTLEK (1x/week): 40-50 min total, Zone 2 base with 1-3 min accelerations, lap-by-lap structure
5. RECOVERY RUN (1-2x/week): 15-20 laps easy, Zone 1
6. STRENGTH (1x/week): eccentric, hip/glutes, running core, light plyometrics

INTENSITY DISTRIBUTION: 80% Zone 1-2, 20% Zone 3-4
WEEKLY VOLUME TARGET: 40-50 km
CRITICAL RULE: NEVER place two quality sessions on consecutive days.

INTENSIVE PLAN RULES — apply ONLY when WEEK INTENSITY contains "intense" / "intensive" / "fuerte":
- Use exactly 4 sessions ordered as: Fartlek → VO2max Intervals → Tempo → Long Progressive
- Minimum 48h rest between each quality session (VO2max and Tempo must not be adjacent)
- FARTLEK: structure the main_block as timed cycles, e.g. "5 cycles: 3 min base (2:30/lap, 37 sec/100m) + 2 min acceleration (2:08/lap, 32 sec/100m). Smooth transitions, controlled cadence."
- VO2max: use 4 × 1000m (2.5 laps). Recovery = 2 min active jog with explicit pace (e.g. "2:35/lap"). State total series count clearly.
- TEMPO: include breathing rhythm pattern (e.g. "3-3 or 4-4 breathing cadence"). Stress that pace must be constant — no negative split, no surge.
- LONG: add progressive split structure (first 60% at Z1 pace, final 40% at Z2 pace, explicit sec/lap for each phase). Add hydration reminder every 5-6 laps.
- warmup for ALL sessions in intensive week: 3 progressive laps with explicit times (e.g. lap 1: 2:50, lap 2: 2:35, lap 3: 2:25) + dynamic mobility drills.
- week_summary must open with the intensity level and week focus, e.g. "INTENSIVE — 80% intensity. Week focused on 5k/10k time improvement. Two quality sessions spaced 48h, complemented with aerobic work and a progressive long run. Total: X km in 4 sessions."
- Populate week_notes with one per-session tactical rationale explaining the physiological goal and key execution cue.

IMPORTANT FOR {user_name.upper()}: {coaching['special_notes']}
{_build_partners_section(partners_profiles)}
Respond ONLY with this JSON structure, no other text:
{{
  "week_summary": "2-3 sentence overview of the week focus and goals",
  "total_km": 45,
  "quality_sessions": 2,
  "week_notes": [
    {{"day": "Monday", "note": "Tactical rationale and key execution cue for this session."}}
  ],
  "days": [
    {{
      "day": "Monday",
      "day_short": "Mon",
      "type": "run",
      "session_label": "Easy Recovery Run",
      "distance_km": 8,
      "distance_laps": 20,
      "duration_min": 50,
      "pace_target": "6:00-6:30/km",
      "pace_sec_lap": "144-156 sec/lap",
      "pace_sec_100m": "36-39 sec/100m",
      "zones": "Zone 1-2 (65-72% MHR)",
      "effort_rpe": 4,
      "warmup": "2 laps progressive: lap 1 in 2:40, lap 2 in 2:20 + dynamic mobility: leg swings, hip circles, high knees",
      "main_block": "16 laps at 2:24/lap (36 sec/100m). Conversational pace, nasal breathing.",
      "cooldown": "2 laps walk/jog + 5 min static stretching: calves, hip flexors, hamstrings",
      "tactical_note": "Physiological goal: active recovery, increase blood flow, flush lactate.",
      "coach_note": "Specific motivational note tailored to this athlete personality",
      "series_detail": []
    }}
  ]
}}

Valid types: run, recovery, intervals, tempo, fartlek, strength, rest.
Only include days from AVAILABLE DAYS.
series_detail only for interval/tempo sessions:
[{{"rep": 1, "distance_m": 800, "target_sec": 200, "recovery_sec": 120, "recovery_pace": "jog 2:30/lap"}}]
week_notes: one entry per training day (skip rest days). For normal/easy intensity, week_notes may be an empty array."""

    try:
        client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)
        response = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=6000,
            system=system_prompt,
            messages=[{"role": "user", "content": (
                f"Génère un plan d'entraînement hebdomadaire pour {user_name}. Jours disponibles: {available_days}. Objectif: {objetivo}. Intensité: {intensidad}. RÉPONDS UNIQUEMENT EN FRANÇAIS."
                if lang == "french" else
                f"Generate a weekly training plan for {user_name}. Available days: {available_days}. Objective: {objetivo}. Intensity: {intensidad}. RESPOND ONLY IN ENGLISH."
                if lang == "english" else
                f"Generate a weekly training plan for {user_name}. Available days: {available_days}. Objective: {objetivo}. Intensity: {intensidad}."
            )}]
        )
        plan_data = json.loads(_strip_fences(response.content[0].text))

        conn2 = get_conn(); c2 = conn2.cursor()
        c2.execute("""
            INSERT INTO training_sessions (user_id, plan, template_key, status)
            VALUES (%s, %s, 'weekly_plan', 'active') RETURNING id
        """, (user_id, json.dumps(plan_data)))
        plan_id = c2.fetchone()["id"]

        c2.execute("""
            UPDATE training_sessions SET status = 'archived'
            WHERE user_id = %s AND template_key = 'weekly_plan' AND id != %s
        """, (user_id, plan_id))

        conn2.commit(); c2.close(); conn2.close()

        return jsonify({"ok": True, "plan": plan_data, "plan_id": plan_id})

    except Exception as e:
        import traceback
        return jsonify({"error": str(e), "traceback": traceback.format_exc()}), 500
