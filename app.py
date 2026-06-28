import os
import json
import re
import uuid
import string
import random
import threading
from datetime import datetime, timezone
from functools import wraps
from werkzeug.security import generate_password_hash, check_password_hash
from flask import (
    Flask, render_template, request, redirect, url_for,
    session, flash, abort,
)

app = Flask(__name__)
app.secret_key = os.environ.get("SECRET_KEY", "wc2026-change-me-in-prod")

DATA_DIR        = os.environ.get("DATA_DIR", os.path.join(os.path.dirname(__file__), "data"))
LEAGUES_DIR     = os.path.join(DATA_DIR, "leagues")
INDEX_FILE      = os.path.join(DATA_DIR, "leagues_index.json")
MASTER_FILE     = os.path.join(DATA_DIR, "worldcup_master.json")
ICS_TEMPLATE    = os.path.join(os.path.dirname(__file__), "worldcup_2026.ics")
MASTER_PASSWORD = os.environ.get("MASTER_PASSWORD", "master-admin")

_lock = threading.Lock()


# ── Directory setup ───────────────────────────────────────────────────────────

def ensure_dirs():
    os.makedirs(LEAGUES_DIR, exist_ok=True)


# ── Index helpers ─────────────────────────────────────────────────────────────

def load_index():
    ensure_dirs()
    if os.path.exists(INDEX_FILE):
        with open(INDEX_FILE) as f:
            return json.load(f)
    return {}


def save_index(index):
    with _lock:
        with open(INDEX_FILE, "w") as f:
            json.dump(index, f, indent=2)


def update_index_entry(code, data):
    index = load_index()
    meta = data.get("meta", {})
    index[code] = {
        "name": meta.get("name", code),
        "is_public": meta.get("is_public", True),
        "has_join_password": bool(meta.get("join_password_hash")),
        "created_at": meta.get("created_at", ""),
        "player_count": len(data.get("players", {})),
        "match_count": len(data.get("matches", [])),
    }
    save_index(index)


# ── League data helpers ───────────────────────────────────────────────────────

def league_file(code):
    return os.path.join(LEAGUES_DIR, f"{code}.json")


def load_league(code):
    path = league_file(code)
    if not os.path.exists(path):
        return None
    with open(path) as f:
        d = json.load(f)
    d.setdefault("meta", {})
    d.setdefault("players", {})
    d.setdefault("settings", {"outcome_points": 1, "diff_points": 1, "score_points": 3})
    d.setdefault("matches", [])
    return d


def save_league(code, data):
    ensure_dirs()
    with _lock:
        with open(league_file(code), "w") as f:
            json.dump(data, f, indent=2)
    update_index_entry(code, data)


# ── Code generation ───────────────────────────────────────────────────────────

def gen_code():
    chars = string.ascii_lowercase + string.digits
    while True:
        code = "".join(random.choices(chars, k=6))
        if not os.path.exists(league_file(code)):
            return code


# ── Score helpers ─────────────────────────────────────────────────────────────

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


def goal_diff(s):
    a, b = parse_score(s)
    return a - b  # signed: positive=home win, negative=away win, 0=draw


def ranked_players(data):
    op = data["settings"]["outcome_points"]
    dp = data["settings"].get("diff_points", 1)
    sp = data["settings"]["score_points"]
    return sorted(
        data["players"].items(),
        key=lambda x: (
            x[1]["outcome_correct"] * op +
            x[1].get("diff_correct", 0) * dp +
            x[1]["score_correct"] * sp
        ),
        reverse=True,
    ), op, dp, sp


def apply_result(data, match, actual):
    dp = data["settings"].get("diff_points", 1)

    # Undo previous result
    if match.get("result") and match.get("predictions"):
        old_diff = goal_diff(match["result"])
        old_out  = get_outcome(match["result"])
        for player, pred in match["predictions"].items():
            if player not in data["players"]: continue
            try: parse_score(pred)
            except: continue
            s = data["players"][player]
            if pred == match["result"]:
                s["outcome_correct"]          = max(0, s["outcome_correct"] - 1)
                s["diff_correct"]             = max(0, s.get("diff_correct", 0) - 1)
                s["score_correct"]            = max(0, s["score_correct"] - 1)
            elif goal_diff(pred) == old_diff:
                s["outcome_correct"]          = max(0, s["outcome_correct"] - 1)
                s["diff_correct"]             = max(0, s.get("diff_correct", 0) - 1)
            elif get_outcome(pred) == old_out:
                s["outcome_correct"]          = max(0, s["outcome_correct"] - 1)

    # Apply new result
    new_diff = goal_diff(actual)
    new_out  = get_outcome(actual)
    for player, pred in match.get("predictions", {}).items():
        if player not in data["players"]: continue
        try: parse_score(pred)
        except: continue
        s = data["players"][player]
        s.setdefault("diff_correct", 0)
        if pred == actual:
            s["outcome_correct"] += 1
            s["diff_correct"]    += 1
            s["score_correct"]   += 1
        elif goal_diff(pred) == new_diff:
            s["outcome_correct"] += 1
            s["diff_correct"]    += 1
        elif get_outcome(pred) == new_out:
            s["outcome_correct"] += 1

    match["result"] = actual


def fmt_dt(dt_str):
    try:
        dt = datetime.fromisoformat(dt_str)
        return dt.strftime("%a %b %d, %H:%M")
    except Exception:
        return dt_str[:10]


def prediction_class(pred, result):
    try:
        if pred == result: return "exact"
        if goal_diff(pred) == goal_diff(result): return "diff"
        if get_outcome(pred) == get_outcome(result): return "outcome"
        return "wrong"
    except Exception:
        return ""


# ── ICS helpers ───────────────────────────────────────────────────────────────

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


def _merge_ics_matches(data, new_matches):
    existing_keys = {(m["name"], m["datetime"]) for m in data["matches"]}
    added = [m for m in new_matches if (m["name"], m["datetime"]) not in existing_keys]
    data["matches"].extend(added)
    return len(added)


# ── Auth decorators ───────────────────────────────────────────────────────────

def league_admin_required(f):
    @wraps(f)
    def decorated(code, *args, **kwargs):
        if not session.get(f"admin_{code}") and not session.get("master_admin"):
            return redirect(url_for("league_admin_login", code=code))
        return f(code, *args, **kwargs)
    return decorated


def league_member_required(f):
    @wraps(f)
    def decorated(code, *args, **kwargs):
        data = load_league(code)
        if data is None:
            abort(404)
        if data["meta"].get("join_password_hash") and not session.get(f"member_{code}"):
            return redirect(url_for("league_join", code=code))
        return f(code, *args, **kwargs)
    return decorated


# ── Template globals ──────────────────────────────────────────────────────────

app.jinja_env.globals.update(fmt_dt=fmt_dt, prediction_class=prediction_class)


# ── Lobby ─────────────────────────────────────────────────────────────────────

@app.route("/")
def lobby():
    index = load_index()
    public = {k: v for k, v in sorted(
        index.items(),
        key=lambda x: x[1].get("created_at", ""),
        reverse=True,
    ) if v.get("is_public")}
    return render_template("lobby.html", leagues=public)


@app.route("/create", methods=["POST"])
def create_league():
    name           = request.form.get("name", "").strip()
    template       = request.form.get("template", "blank")
    is_public      = request.form.get("visibility") == "public"
    join_password  = request.form.get("join_password", "").strip()
    admin_password = request.form.get("admin_password", "").strip()

    if not name:
        flash("Enter a league name.", "error")
        return redirect(url_for("lobby"))
    if not admin_password:
        flash("Enter an admin password.", "error")
        return redirect(url_for("lobby"))

    code = gen_code()
    matches = []

    if template == "worldcup":
        master = load_master()
        for m in master["matches"]:
            matches.append({
                "id": str(uuid.uuid4()),
                "name": m["name"],
                "datetime": m["datetime"],
                "venue": m.get("venue", ""),
                "round": m.get("round", ""),
                "result": m.get("result"),
                "predictions": {},
            })

    data = {
        "meta": {
            "name": name,
            "is_public": is_public,
            "join_password_hash": generate_password_hash(join_password) if join_password else None,
            "admin_password_hash": generate_password_hash(admin_password),
            "created_at": datetime.now(timezone.utc).isoformat(),
            "template": template,
        },
        "players": {},
        "settings": {"outcome_points": 1, "diff_points": 1, "score_points": 3},
        "matches": matches,
    }

    save_league(code, data)
    session[f"admin_{code}"] = True
    if join_password:
        session[f"member_{code}"] = True

    flash(f"League created! Your code is {code} — share this link with friends.", "success")
    return redirect(url_for("league_admin_dashboard", code=code))


@app.route("/find", methods=["POST"])
def find_league():
    code = request.form.get("code", "").strip().lower()
    if not code:
        flash("Enter a league code.", "error")
        return redirect(url_for("lobby"))
    if load_league(code) is None:
        flash(f"No league found with code '{code}'.", "error")
        return redirect(url_for("lobby"))
    return redirect(url_for("league_welcome", code=code))


# ── Join (password gate) ──────────────────────────────────────────────────────

@app.route("/league/<code>/join", methods=["GET", "POST"])
def league_join(code):
    data = load_league(code)
    if data is None:
        abort(404)
    if not data["meta"].get("join_password_hash"):
        return redirect(url_for("league_welcome", code=code))
    if session.get(f"member_{code}"):
        return redirect(url_for("league_welcome", code=code))
    if request.method == "POST":
        pw = request.form.get("password", "")
        if check_password_hash(data["meta"]["join_password_hash"], pw):
            session[f"member_{code}"] = True
            return redirect(url_for("league_welcome", code=code))
        flash("Wrong password.", "error")
    return render_template("join.html", league_name=data["meta"]["name"], code=code)


# ── Welcome / name entry ──────────────────────────────────────────────────────

@app.route("/league/<code>/welcome", methods=["GET", "POST"])
def league_welcome(code):
    data = load_league(code)
    if data is None:
        abort(404)
    if data["meta"].get("join_password_hash") and not session.get(f"member_{code}"):
        return redirect(url_for("league_join", code=code))

    if request.method == "POST":
        name = request.form.get("name", "").strip()
        if not name:
            flash("Enter your name to continue.", "error")
            return redirect(url_for("league_welcome", code=code))
        if name not in data["players"]:
            data["players"][name] = {"outcome_correct": 0, "diff_correct": 0, "score_correct": 0}
            save_league(code, data)
        session[f"player_{code}"] = name
        flash(f"Welcome, {name}! 🎉", "success")
        return redirect(url_for("league_index", code=code))

    return render_template("welcome.html", code=code, league_name=data["meta"]["name"])


# ── Scoreboard ────────────────────────────────────────────────────────────────

@app.route("/league/<code>/")
@league_member_required
def league_index(code):
    data = load_league(code)
    ranked, op, dp, sp = ranked_players(data)
    completed = [m for m in data["matches"] if m.get("result")]
    upcoming  = [m for m in data["matches"] if not m.get("result")]
    return render_template("index.html",
                           code=code, league_name=data["meta"]["name"],
                           ranked=ranked, op=op, dp=dp, sp=sp,
                           completed=completed, upcoming=upcoming)


# ── Predictions ───────────────────────────────────────────────────────────────

@app.route("/league/<code>/predict", methods=["GET", "POST"])
@league_member_required
def league_predict(code):
    data = load_league(code)
    players = sorted(data["players"].keys())

    if not players:
        flash("No players yet. Ask the admin to add you.", "info")
        return redirect(url_for("league_index", code=code))

    if request.method == "POST":
        player = request.form.get("player", "").strip()
        if not player or player not in players:
            flash("Select a valid player name.", "error")
            return redirect(url_for("league_predict", code=code))

        errors = []
        for match in data["matches"]:
            if match.get("result"): continue
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
            for e in errors: flash(e, "error")
            return redirect(url_for("league_predict", code=code))

        save_league(code, data)
        session[f"player_{code}"] = player
        flash(f"Predictions saved for {player}!", "success")
        return redirect(url_for("league_predict", code=code) + f"?player={player}")

    player   = request.args.get("player") or session.get(f"player_{code}", "")
    upcoming = [m for m in data["matches"] if not m.get("result")]
    return render_template("predict.html",
                           code=code, league_name=data["meta"]["name"],
                           players=players, player=player, upcoming=upcoming)


# ── Admin login ───────────────────────────────────────────────────────────────

@app.route("/league/<code>/admin", methods=["GET", "POST"])
def league_admin_login(code):
    data = load_league(code)
    if data is None:
        abort(404)
    if session.get(f"admin_{code}"):
        return redirect(url_for("league_admin_dashboard", code=code))
    if request.method == "POST":
        pw = request.form.get("password", "")
        if check_password_hash(data["meta"]["admin_password_hash"], pw):
            session[f"admin_{code}"] = True
            session[f"member_{code}"] = True
            return redirect(url_for("league_admin_dashboard", code=code))
        flash("Wrong password.", "error")
    return render_template("admin_login.html",
                           code=code, league_name=data["meta"]["name"])


# ── Admin dashboard ───────────────────────────────────────────────────────────

@app.route("/league/<code>/admin/dashboard")
@league_admin_required
def league_admin_dashboard(code):
    data = load_league(code)
    return render_template("admin_dashboard.html",
                           code=code, league_name=data["meta"]["name"],
                           data=data, fmt_dt=fmt_dt)


@app.route("/league/<code>/admin/result", methods=["POST"])
@league_admin_required
def league_admin_result(code):
    data     = load_league(code)
    match_id = request.form.get("match_id")
    actual   = request.form.get("actual", "").strip()
    match    = next((m for m in data["matches"] if m["id"] == match_id), None)
    if not match:
        flash("Match not found.", "error")
        return redirect(url_for("league_admin_dashboard", code=code))
    if not actual:
        flash("Enter the actual score.", "error")
        return redirect(url_for("league_admin_dashboard", code=code))
    try:
        parse_score(actual)
    except ValueError:
        flash("Invalid score — use 2-1 format.", "error")
        return redirect(url_for("league_admin_dashboard", code=code))
    apply_result(data, match, actual)
    save_league(code, data)
    flash(f"Result recorded: {match['name']} → {actual}", "success")
    return redirect(url_for("league_admin_dashboard", code=code))


@app.route("/league/<code>/admin/add_player", methods=["POST"])
@league_admin_required
def league_admin_add_player(code):
    data = load_league(code)
    name = request.form.get("name", "").strip()
    if not name:
        flash("Enter a player name.", "error")
    elif name in data["players"]:
        flash(f"'{name}' already exists.", "error")
    else:
        data["players"][name] = {"outcome_correct": 0, "diff_correct": 0, "score_correct": 0}
        save_league(code, data)
        flash(f"Added '{name}'.", "success")
    return redirect(url_for("league_admin_dashboard", code=code))


@app.route("/league/<code>/admin/remove_player", methods=["POST"])
@league_admin_required
def league_admin_remove_player(code):
    data = load_league(code)
    name = request.form.get("name", "").strip()
    if name in data["players"]:
        del data["players"][name]
        save_league(code, data)
        flash(f"Removed '{name}'.", "success")
    return redirect(url_for("league_admin_dashboard", code=code))


@app.route("/league/<code>/admin/import_ics", methods=["POST"])
@league_admin_required
def league_admin_import_ics(code):
    data = load_league(code)
    url  = request.form.get("ics_url", "").strip()
    if not url:
        flash("Enter an ICS URL.", "error")
        return redirect(url_for("league_admin_dashboard", code=code))
    try:
        import requests as req
        resp = req.get(url, timeout=20, headers={"User-Agent": "WC2026Scoreboard/1.0"})
        resp.raise_for_status()
        added = _merge_ics_matches(data, _parse_ics_bytes(resp.content))
        save_league(code, data)
        flash(f"Imported {added} new matches.", "success")
    except Exception as e:
        flash(f"Import failed: {e}", "error")
    return redirect(url_for("league_admin_dashboard", code=code))


@app.route("/league/<code>/admin/import_ics_file", methods=["POST"])
@league_admin_required
def league_admin_import_ics_file(code):
    data = load_league(code)
    f    = request.files.get("ics_file")
    if not f or not f.filename:
        flash("Choose an .ics file first.", "error")
        return redirect(url_for("league_admin_dashboard", code=code))
    try:
        added = _merge_ics_matches(data, _parse_ics_bytes(f.read()))
        save_league(code, data)
        flash(f"Imported {added} new matches.", "success")
    except Exception as e:
        flash(f"Import failed: {e}", "error")
    return redirect(url_for("league_admin_dashboard", code=code))


@app.route("/league/<code>/admin/match/<match_id>", methods=["GET", "POST"])
@league_admin_required
def league_admin_edit_match(code, match_id):
    data  = load_league(code)
    match = next((m for m in data["matches"] if m["id"] == match_id), None)
    if not match:
        flash("Match not found.", "error")
        return redirect(url_for("league_admin_dashboard", code=code))

    if request.method == "POST":
        if match.get("result"):
            old_result   = match["result"]
            old_outcome  = get_outcome(old_result)
            old_diff_val = goal_diff(old_result)
            for player, pred in match.get("predictions", {}).items():
                if player not in data["players"]: continue
                try: parse_score(pred)
                except: continue
                s = data["players"][player]
                if pred == old_result:
                    s["outcome_correct"] = max(0, s["outcome_correct"] - 1)
                    s["diff_correct"]    = max(0, s.get("diff_correct", 0) - 1)
                    s["score_correct"]   = max(0, s["score_correct"]   - 1)
                elif goal_diff(pred) == old_diff_val:
                    s["outcome_correct"] = max(0, s["outcome_correct"] - 1)
                    s["diff_correct"]    = max(0, s.get("diff_correct", 0) - 1)
                elif get_outcome(pred) == old_outcome:
                    s["outcome_correct"] = max(0, s["outcome_correct"] - 1)

        new_name = request.form.get("match_name", "").strip()
        if new_name:
            match["name"] = new_name

        new_preds = {}
        for player in data["players"]:
            pred = request.form.get(f"pred_{player}", "").strip()
            if pred:
                try:
                    parse_score(pred)
                    new_preds[player] = pred
                except ValueError:
                    flash(f"Invalid score '{pred}' for {player}.", "error")
                    return redirect(url_for("league_admin_edit_match",
                                           code=code, match_id=match_id))
        match["predictions"] = new_preds

        if match.get("result"):
            result         = match["result"]
            result_outcome = get_outcome(result)
            result_diff    = goal_diff(result)
            for player, pred in new_preds.items():
                if player not in data["players"]: continue
                s = data["players"][player]
                s.setdefault("diff_correct", 0)
                if pred == result:
                    s["outcome_correct"] += 1
                    s["diff_correct"]    += 1
                    s["score_correct"]   += 1
                elif goal_diff(pred) == result_diff:
                    s["outcome_correct"] += 1
                    s["diff_correct"]    += 1
                elif get_outcome(pred) == result_outcome:
                    s["outcome_correct"] += 1

        save_league(code, data)
        flash("Predictions updated.", "success")
        return redirect(url_for("league_admin_edit_match", code=code, match_id=match_id))

    players = sorted(data["players"].keys())
    return render_template("admin_edit_match.html",
                           code=code, league_name=data["meta"]["name"],
                           match=match, players=players, fmt_dt=fmt_dt)


@app.route("/league/<code>/admin/settings", methods=["POST"])
@league_admin_required
def league_admin_settings(code):
    data = load_league(code)
    try:
        data["settings"]["outcome_points"] = int(request.form.get("outcome_points", 1))
        data["settings"]["diff_points"]    = int(request.form.get("diff_points", 1))
        data["settings"]["score_points"]   = int(request.form.get("score_points", 3))
        save_league(code, data)
        flash("Settings saved.", "success")
    except ValueError:
        flash("Invalid point values.", "error")
    return redirect(url_for("league_admin_dashboard", code=code))


@app.route("/league/<code>/admin/recalculate", methods=["POST"])
@league_admin_required
def league_admin_recalculate(code):
    data = load_league(code)
    for player in data["players"]:
        data["players"][player]["outcome_correct"] = 0
        data["players"][player]["diff_correct"]    = 0
        data["players"][player]["score_correct"]   = 0
    completed = [(m, m["result"]) for m in data["matches"] if m.get("result")]
    for match, result in completed:
        match["result"] = None  # so apply_result skips the undo block
        apply_result(data, match, result)
    save_league(code, data)
    flash(f"Scores recalculated from {len(completed)} completed match(es).", "success")
    return redirect(url_for("league_admin_dashboard", code=code))


@app.route("/league/<code>/admin/visibility", methods=["POST"])
@league_admin_required
def league_admin_visibility(code):
    data = load_league(code)
    data["meta"]["is_public"] = not data["meta"].get("is_public", False)
    save_league(code, data)
    state = "public" if data["meta"]["is_public"] else "private"
    flash(f"League is now {state}.", "success")
    return redirect(url_for("league_admin_dashboard", code=code))


@app.route("/league/<code>/admin/logout")
def league_admin_logout(code):
    session.pop(f"admin_{code}", None)
    return redirect(url_for("league_index", code=code))


# ── World Cup master admin ────────────────────────────────────────────────────

def load_master():
    ensure_dirs()
    if os.path.exists(MASTER_FILE):
        with open(MASTER_FILE) as f:
            return json.load(f)
    # Bootstrap from ICS template on first load
    matches = []
    if os.path.exists(ICS_TEMPLATE):
        try:
            with open(ICS_TEMPLATE, "rb") as f:
                matches = _parse_ics_bytes(f.read())
        except Exception:
            pass
    data = {"matches": matches}
    _save_master(data)
    return data


def _save_master(data):
    with _lock:
        with open(MASTER_FILE, "w") as f:
            json.dump(data, f, indent=2)


def propagate_result(match_name, match_datetime, actual):
    """Apply a result to every World Cup template league."""
    index = load_index()
    synced = 0
    for code in index:
        league_data = load_league(code)
        if league_data is None:
            continue
        if league_data["meta"].get("template") != "worldcup":
            continue
        match = next(
            (m for m in league_data["matches"]
             if m["name"] == match_name and m["datetime"] == match_datetime),
            None,
        )
        if match is None:
            continue
        apply_result(league_data, match, actual)
        save_league(code, league_data)
        synced += 1
    return synced


def wc_league_count():
    index = load_index()
    count = 0
    for code, info in index.items():
        data = load_league(code)
        if data and data["meta"].get("template") == "worldcup":
            count += 1
    return count


@app.route("/worldcup/admin", methods=["GET", "POST"])
def master_login():
    if session.get("master_admin"):
        return redirect(url_for("master_dashboard"))
    if request.method == "POST":
        if request.form.get("password") == MASTER_PASSWORD:
            session["master_admin"] = True
            return redirect(url_for("master_dashboard"))
        flash("Wrong password.", "error")
    return render_template("master_login.html")


@app.route("/worldcup/admin/dashboard")
def master_dashboard():
    if not session.get("master_admin"):
        return redirect(url_for("master_login"))
    data = load_master()
    completed = [m for m in data["matches"] if m.get("result")]
    upcoming  = [m for m in data["matches"] if not m.get("result")]
    index = load_index()
    leagues = {code: info for code, info in sorted(
        index.items(), key=lambda x: x[1].get("created_at", ""), reverse=True
    )}
    return render_template("master_dashboard.html",
                           completed=completed, upcoming=upcoming,
                           fmt_dt=fmt_dt, league_count=wc_league_count(),
                           leagues=leagues)


@app.route("/worldcup/admin/result", methods=["POST"])
def master_result():
    if not session.get("master_admin"):
        return redirect(url_for("master_login"))
    data     = load_master()
    match_id = request.form.get("match_id")
    actual   = request.form.get("actual", "").strip()
    match    = next((m for m in data["matches"] if m["id"] == match_id), None)
    if not match:
        flash("Match not found.", "error")
        return redirect(url_for("master_dashboard"))
    if not actual:
        flash("Enter the actual score.", "error")
        return redirect(url_for("master_dashboard"))
    try:
        parse_score(actual)
    except ValueError:
        flash("Invalid score — use 2-1 format.", "error")
        return redirect(url_for("master_dashboard"))
    match["result"] = actual
    _save_master(data)
    synced = propagate_result(match["name"], match["datetime"], actual)
    flash(f"{match['name']} → {actual} · synced to {synced} league(s).", "success")
    return redirect(url_for("master_dashboard"))


@app.route("/worldcup/admin/rename", methods=["POST"])
def master_rename_match():
    if not session.get("master_admin"):
        return redirect(url_for("master_login"))
    data     = load_master()
    match_id = request.form.get("match_id")
    new_name = request.form.get("new_name", "").strip()
    match    = next((m for m in data["matches"] if m["id"] == match_id), None)
    if not match:
        flash("Match not found.", "error")
        return redirect(url_for("master_dashboard"))
    if not new_name:
        flash("Name cannot be empty.", "error")
        return redirect(url_for("master_dashboard"))

    old_name     = match["name"]
    old_datetime = match["datetime"]
    match["name"] = new_name
    _save_master(data)

    index  = load_index()
    synced = 0
    for code in index:
        league_data = load_league(code)
        if league_data is None:
            continue
        if league_data["meta"].get("template") != "worldcup":
            continue
        m = next(
            (m for m in league_data["matches"]
             if m["name"] == old_name and m["datetime"] == old_datetime),
            None,
        )
        if m:
            m["name"] = new_name
            save_league(code, league_data)
            synced += 1

    flash(f"Renamed to '{new_name}' · updated {synced} league(s).", "success")
    return redirect(url_for("master_dashboard"))


@app.route("/worldcup/admin/logout")
def master_logout():
    session.pop("master_admin", None)
    return redirect(url_for("lobby"))


if __name__ == "__main__":
    app.run(debug=True, port=5000)
