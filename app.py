from flask import Flask, request, jsonify, render_template
from database import (
    init_db, get_users,
    get_runs_by_user, add_run, delete_run, get_stats_by_user,
    add_cooper_test, get_cooper_tests, delete_cooper_test
)

app = Flask(__name__)
init_db()

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
    return jsonify(get_users())


# ─────────────────────────────────────────
# RUNS
# ─────────────────────────────────────────

@app.route("/api/runs/<int:user_id>")
def api_get_runs(user_id):
    return jsonify(get_runs_by_user(user_id))


@app.route("/api/runs", methods=["POST"])
def api_add_run():
    d = request.get_json()
    for f in ["user_id", "date", "type", "effort"]:
        if f not in d:
            return jsonify({"error": f"Required field: {f}"}), 400

    distance = float(d.get("distance", 0))
    time_sec = int(d.get("time_seconds", 0))

    pace = "—"
    if distance > 0 and time_sec > 0:
        ps   = time_sec / distance
        pace = f"{int(ps//60)}:{int(ps%60):02d}"

    run = {
        "user_id": int(d["user_id"]), "date": d["date"],
        "type": d["type"], "distance": distance,
        "time_seconds": time_sec, "pace": pace,
        "effort": int(d["effort"]), "notes": d.get("notes", ""),
        "interval_dist": d.get("interval_dist"),
        "interval_reps": d.get("interval_reps"),
        "interval_pace": d.get("interval_pace"),
        "circuit_rounds": d.get("circuit_rounds"),
        "circuit_details": d.get("circuit_details"),
        "technique_drills": d.get("technique_drills"),
    }

    new_id    = add_run(run)
    run["id"] = new_id
    return jsonify(run), 201


@app.route("/api/runs/<int:run_id>", methods=["DELETE"])
def api_delete_run(run_id):
    delete_run(run_id)
    return jsonify({"message": "Deleted"})


# ─────────────────────────────────────────
# STATS
# ─────────────────────────────────────────

@app.route("/api/stats/<int:user_id>")
def api_stats(user_id):
    return jsonify(get_stats_by_user(user_id))


# ─────────────────────────────────────────
# COOPER TEST
# ─────────────────────────────────────────

@app.route("/api/cooper/<int:user_id>")
def api_get_cooper(user_id):
    return jsonify(get_cooper_tests(user_id))


@app.route("/api/cooper", methods=["POST"])
def api_add_cooper():
    d = request.get_json()
    for f in ["user_id", "date", "distance_m"]:
        if f not in d:
            return jsonify({"error": f"Required field: {f}"}), 400
    result = add_cooper_test(d)
    return jsonify(result), 201


@app.route("/api/cooper/<int:test_id>", methods=["DELETE"])
def api_delete_cooper(test_id):
    delete_cooper_test(test_id)
    return jsonify({"message": "Deleted"})


# ─────────────────────────────────────────

if __name__ == "__main__":
    app.run(debug=True)