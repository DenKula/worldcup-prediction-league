import os
import json
import re
import uuid
import threading
from datetime import datetime, timezone
from functools import wraps
from flask import (
    Flask, render_template, request, redirect, url_for,
    session, flash,
)

app = Flask(__name__)
app.secret_key = os.environ.get("SECRET_KEY", "wc2026-change-me-in-prod")
ADMIN_PASSWORD = os.environ.get("ADMIN_PASSWORD", "admin")
DATA_FILE = os.environ.get(
    "DATA_FILE",
    os.path.join(os.path.dirname(__file__), "worldcup_data.json"),
)

_lock = threading.Lock()


# ── Data helpers ──────────────────────────────────────────────────────────────

def load_data():
    if os.path.exists(DATA_FILE):
        with open(DATA_FILE) as f:
            d = json.load(f)
    else:
        d = {}
    d.setdefault("players", {})
    d.setdefault("settings", {"outcome_points": 1, "score_points": 3})
    d.setdefault("matches", [])
    return d


def save_data(data):
    with _lock:
        with open(DATA_FILE, "w") as f:
            json.dump(data, f, indent=2)


def parse_score(s):
    parts = s.strip().split("-")
    if len(parts) != 2:
        raise ValueError
    return int(parts[0]), int(parts[1])


def get_outcome(s):
    a, b = parse_score(s)
    if a > b: return "home"
    if a < b: return "away"
    return "draw"


def ranked_players(data):
    op = data["settings"]["outcome_points"]
    sp = data["settings"]["score_points"]
    return sorted(
        data["players"].items(),
        key=lambda x: x[1]["outcome_correct"] * op + x[1]["score_correct"] * sp,
        reverse=True,
    ), op, sp


def apply_result(data, match, actual):
    """Score a match result against stored predictions. Undoes previous result first."""
    op = data["settings"]["outcome_points"]
    sp = data["settings"]["score_points"]

    # Undo previous
    if match.get("result") and match.get("predictions"):
        old_out = get_outcome(match["result"])
        for player, pred in match["predictions"].items():
            if player not in data["players"]: continue
            try: parse_score(pred)
            except: continue
            s = data["players"][player]
            if pred == match["result"]:
                s["outcome_correct"] = max(0, s["outcome_correct"] - 1)
                s["score_correct"]   = max(0, s["score_correct"]   - 1)
            elif get_outcome(pred) == old_out:
                s["outcome_correct"] = max(0, s["outcome_correct"] - 1)

    # Apply new
    new_out = get_outcome(actual)
    for player, pred in match.get("predictions", {}).items():
        if player not in data["players"]: continue
        try: parse_score(pred)
        except: continue
        s = data["players"][player]
        if pred == actual:
            s["outcome_correct"] += 1
            s["score_correct"]   += 1
        elif get_outcome(pred) == new_out:
            s["outcome_correct"] += 1

    match["result"] = actual


def fmt_dt(dt_str):
    """Format an ISO datetime string for display."""
    try:
        dt = datetime.fromisoformat(dt_str)
        return dt.strftime("%a %b %d, %H:%M")
    except Exception:
        return dt_str[:10]


def prediction_class(pred, result):
    try:
        if pred == result: return "exact"
        if get_outcome(pred) == get_outcome(result): return "outcome"
        return "wrong"
    except Exception:
        return ""


# ── Auth decorator ────────────────────────────────────────────────────────────

def admin_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        if not session.get("admin"):
            return redirect(url_for("admin_login"))
        return f(*args, **kwargs)
    return decorated


# ── Template filters / globals ────────────────────────────────────────────────

app.jinja_env.globals.update(
    fmt_dt=fmt_dt,
    prediction_class=prediction_class,
)


# ── Routes ────────────────────────────────────────────────────────────────────

@app.route("/")
def index():
    data = load_data()
    ranked, op, sp = ranked_players(data)
    completed = [m for m in data["matches"] if m.get("result")]
    upcoming  = [m for m in data["matches"] if not m.get("result")]
    return render_template("index.html",
                           ranked=ranked, op=op, sp=sp,
                           completed=completed, upcoming=upcoming)


@app.route("/predict", methods=["GET", "POST"])
def predict():
    data = load_data()
    players = sorted(data["players"].keys())

    if not players:
        flash("No players have been added yet. Ask the admin to add you.", "info")
        return redirect(url_for("index"))

    if request.method == "POST":
        player = request.form.get("player", "").strip()
        if not player or player not in players:
            flash("Select a valid player name.", "error")
            return redirect(url_for("predict"))

        errors = []
        for match in data["matches"]:
            if match.get("result"):
                continue  # match already decided — lock predictions
            pred = request.form.get(f"pred_{match['id']}", "").strip()
            if pred:
                try:
                    parse_score(pred)
                    match.setdefault("predictions", {})[player] = pred
                except ValueError:
                    errors.append(f"'{pred}' is not a valid score (use 2-1 format)")
            else:
                match.setdefault("predictions", {}).pop(player, None)

        if errors:
            for e in errors:
                flash(e, "error")
            return redirect(url_for("predict"))

        save_data(data)
        session["player"] = player
        flash(f"Predictions saved for {player}!", "success")
        return redirect(url_for("predict") + f"?player={player}")

    player = request.args.get("player") or session.get("player", "")
    upcoming = [m for m in data["matches"] if not m.get("result")]
    return render_template("predict.html",
                           players=players, player=player,
                           upcoming=upcoming)


# ── Admin ─────────────────────────────────────────────────────────────────────

@app.route("/admin", methods=["GET", "POST"])
def admin_login():
    if session.get("admin"):
        return redirect(url_for("admin_dashboard"))
    if request.method == "POST":
        if request.form.get("password") == ADMIN_PASSWORD:
            session["admin"] = True
            return redirect(url_for("admin_dashboard"))
        flash("Wrong password.", "error")
    return render_template("admin_login.html")


@app.route("/admin/dashboard")
@admin_required
def admin_dashboard():
    data = load_data()
    return render_template("admin_dashboard.html", data=data, fmt_dt=fmt_dt)


@app.route("/admin/result", methods=["POST"])
@admin_required
def admin_result():
    data = load_data()
    match_id = request.form.get("match_id")
    actual   = request.form.get("actual", "").strip()

    match = next((m for m in data["matches"] if m["id"] == match_id), None)
    if not match:
        flash("Match not found.", "error")
        return redirect(url_for("admin_dashboard"))

    if actual:
        try:
            parse_score(actual)
        except ValueError:
            flash("Invalid score — use home-away format, e.g. 2-1", "error")
            return redirect(url_for("admin_dashboard"))
        apply_result(data, match, actual)
    else:
        flash("Enter the actual score.", "error")
        return redirect(url_for("admin_dashboard"))

    save_data(data)
    flash(f"Result recorded: {match['name']} → {actual}", "success")
    return redirect(url_for("admin_dashboard"))


@app.route("/admin/add_player", methods=["POST"])
@admin_required
def admin_add_player():
    data = load_data()
    name = request.form.get("name", "").strip()
    if not name:
        flash("Enter a player name.", "error")
    elif name in data["players"]:
        flash(f"'{name}' already exists.", "error")
    else:
        data["players"][name] = {"outcome_correct": 0, "score_correct": 0}
        save_data(data)
        flash(f"Added player '{name}'.", "success")
    return redirect(url_for("admin_dashboard"))


@app.route("/admin/remove_player", methods=["POST"])
@admin_required
def admin_remove_player():
    data = load_data()
    name = request.form.get("name", "").strip()
    if name in data["players"]:
        del data["players"][name]
        save_data(data)
        flash(f"Removed '{name}'.", "success")
    return redirect(url_for("admin_dashboard"))


def _parse_ics_bytes(raw: bytes) -> list:
    from icalendar import Calendar as ICal
    cal = ICal.from_ical(raw)
    matches = []
    for comp in cal.walk():
        if comp.name != "VEVENT": continue
        summary = str(comp.get("SUMMARY", "")).strip()
        if not summary: continue
        dtstart = comp.get("DTSTART")
        if dtstart is None: continue
        dt_str = dtstart.dt.isoformat()
        location = str(comp.get("LOCATION", "")).strip()
        score_m = re.search(r'\((\d+)[-–](\d+)\)\s*$', summary)
        inline_result = None
        if score_m:
            inline_result = f"{score_m.group(1)}-{score_m.group(2)}"
            summary = summary[:score_m.start()].strip()
        matches.append({
            "id": str(uuid.uuid4()),
            "name": summary,
            "datetime": dt_str,
            "venue": location,
            "round": "",
            "result": inline_result,
            "predictions": {},
        })
    matches.sort(key=lambda m: m["datetime"])
    return matches


def _merge_ics_matches(data: dict, new_matches: list):
    existing_keys = {(m["name"], m["datetime"]) for m in data["matches"]}
    added = [m for m in new_matches if (m["name"], m["datetime"]) not in existing_keys]
    data["matches"].extend(added)
    return len(added)


@app.route("/admin/import_ics", methods=["POST"])
@admin_required
def admin_import_ics():
    data = load_data()
    url = request.form.get("ics_url", "").strip()
    if not url:
        flash("Enter an ICS URL.", "error")
        return redirect(url_for("admin_dashboard"))
    try:
        import requests as req
        resp = req.get(url, timeout=20, headers={"User-Agent": "WC2026Scoreboard/1.0"})
        resp.raise_for_status()
        new_matches = _parse_ics_bytes(resp.content)
        added = _merge_ics_matches(data, new_matches)
        save_data(data)
        flash(f"Imported {added} new matches.", "success")
    except Exception as e:
        flash(f"Import failed: {e}", "error")
    return redirect(url_for("admin_dashboard"))


@app.route("/admin/import_ics_file", methods=["POST"])
@admin_required
def admin_import_ics_file():
    data = load_data()
    f = request.files.get("ics_file")
    if not f or not f.filename:
        flash("Choose an .ics file first.", "error")
        return redirect(url_for("admin_dashboard"))
    try:
        new_matches = _parse_ics_bytes(f.read())
        added = _merge_ics_matches(data, new_matches)
        save_data(data)
        flash(f"Imported {added} new matches from file.", "success")
    except Exception as e:
        flash(f"Import failed: {e}", "error")
    return redirect(url_for("admin_dashboard"))


@app.route("/admin/match/<match_id>", methods=["GET", "POST"])
@admin_required
def admin_edit_match(match_id):
    data = load_data()
    match = next((m for m in data["matches"] if m["id"] == match_id), None)
    if not match:
        flash("Match not found.", "error")
        return redirect(url_for("admin_dashboard"))

    if request.method == "POST":
        players = list(data["players"].keys())

        # If match has a result, undo old prediction scores before saving new ones
        if match.get("result"):
            old_result = match["result"]
            old_outcome = get_outcome(old_result)
            for player, pred in match.get("predictions", {}).items():
                if player not in data["players"]: continue
                try: parse_score(pred)
                except: continue
                s = data["players"][player]
                if pred == old_result:
                    s["outcome_correct"] = max(0, s["outcome_correct"] - 1)
                    s["score_correct"]   = max(0, s["score_correct"]   - 1)
                elif get_outcome(pred) == old_outcome:
                    s["outcome_correct"] = max(0, s["outcome_correct"] - 1)

        # Save new predictions
        new_preds = {}
        for player in players:
            pred = request.form.get(f"pred_{player}", "").strip()
            if pred:
                try:
                    parse_score(pred)
                    new_preds[player] = pred
                except ValueError:
                    flash(f"Invalid score '{pred}' for {player} — use 2-1 format.", "error")
                    return redirect(url_for("admin_edit_match", match_id=match_id))
        match["predictions"] = new_preds

        # Re-apply scores if match already has a result
        if match.get("result"):
            result = match["result"]
            result_outcome = get_outcome(result)
            for player, pred in new_preds.items():
                if player not in data["players"]: continue
                s = data["players"][player]
                if pred == result:
                    s["outcome_correct"] += 1
                    s["score_correct"]   += 1
                elif get_outcome(pred) == result_outcome:
                    s["outcome_correct"] += 1

        save_data(data)
        flash("Predictions updated.", "success")
        return redirect(url_for("admin_edit_match", match_id=match_id))

    players = sorted(data["players"].keys())
    return render_template("admin_edit_match.html", match=match, players=players,
                           fmt_dt=fmt_dt)


@app.route("/admin/settings", methods=["POST"])
@admin_required
def admin_settings():
    data = load_data()
    try:
        data["settings"]["outcome_points"] = int(request.form.get("outcome_points", 1))
        data["settings"]["score_points"]   = int(request.form.get("score_points", 3))
        save_data(data)
        flash("Settings saved.", "success")
    except ValueError:
        flash("Invalid point values.", "error")
    return redirect(url_for("admin_dashboard"))


@app.route("/admin/logout")
def admin_logout():
    session.pop("admin", None)
    return redirect(url_for("index"))


if __name__ == "__main__":
    app.run(debug=True, port=5000)
