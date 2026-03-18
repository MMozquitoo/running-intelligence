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
# DB CONNECTION
# ─────────────────────────────────────────

def get_conn():
    return psycopg2.connect(DATABASE_URL, cursor_factory=RealDictCursor)


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

    for name in ["Cristian", "Adrien"]:
        c.execute("INSERT INTO users (name) VALUES (%s) ON CONFLICT (name) DO NOTHING", (name,))

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

    # Migrations for existing deployments
    c.execute("ALTER TABLE users ADD COLUMN IF NOT EXISTS race_date TEXT")
    c.execute("ALTER TABLE users ADD COLUMN IF NOT EXISTS profile_data JSONB")

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
    if cooper_row:
        vo2max = float(cooper_row["vo2max"])
        race_progress = min(100, max(0, int((vo2max / 52) * 100)))

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

    # Advanced stats
    total_time_seconds = sum(r["time_seconds"] for r in runs)
    profile = (user_row["profile_data"] or {}) if user_row else {}
    weight_kg = profile.get("weight_kg", 70)
    total_hours = total_time_seconds / 3600
    calories_burned = round(8.0 * weight_kg * total_hours)
    estimates = {
        "100m":  profile.get("est_100m_sec"),
        "400m":  profile.get("est_400m_sec"),
        "1km":   profile.get("est_1km_sec"),
        "5km":   profile.get("est_5km_sec"),
        "10km":  profile.get("est_10km_sec"),
    }

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
    }


@app.route("/api/stats/<int:user_id>")
def api_stats(user_id):
    return jsonify(_build_stats(user_id))


@app.route("/api/user/<int:user_id>/stats")
def api_user_stats(user_id):
    return jsonify(_build_stats(user_id))


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
