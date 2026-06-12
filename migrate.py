"""
One-shot migration script: converts the old single-league worldcup_data.json
into the new multi-league format.

Run from the webapp/ directory on Railway:
    python migrate.py
"""
import os, json, string, random
from datetime import datetime, timezone
from werkzeug.security import generate_password_hash

# ── Config ────────────────────────────────────────────────────────────────────
LEAGUE_NAME    = "Dünya KUPASI"
IS_PUBLIC      = False
ADMIN_PASSWORD = "good33bye"

# ── Paths ─────────────────────────────────────────────────────────────────────
OLD_FILE    = os.environ.get("DATA_FILE",
              os.path.join(os.path.dirname(__file__), "worldcup_data.json"))
DATA_DIR    = os.environ.get("DATA_DIR",
              os.path.join(os.path.dirname(__file__), "data"))
LEAGUES_DIR = os.path.join(DATA_DIR, "leagues")
INDEX_FILE  = os.path.join(DATA_DIR, "leagues_index.json")
MASTER_FILE = os.path.join(DATA_DIR, "worldcup_master.json")

# ── Load old data ─────────────────────────────────────────────────────────────
if not os.path.exists(OLD_FILE):
    print(f"ERROR: old data file not found at {OLD_FILE}")
    exit(1)

with open(OLD_FILE) as f:
    old = json.load(f)

players = old.get("players", {})
matches = old.get("matches", [])
settings = old.get("settings", {"outcome_points": 1, "score_points": 3})
print(f"Loaded: {len(players)} players, {len(matches)} matches")

# ── Generate league code ──────────────────────────────────────────────────────
os.makedirs(LEAGUES_DIR, exist_ok=True)

def gen_code():
    chars = string.ascii_lowercase + string.digits
    while True:
        code = "".join(random.choices(chars, k=6))
        if not os.path.exists(os.path.join(LEAGUES_DIR, f"{code}.json")):
            return code

code = gen_code()

# ── Write new league file ─────────────────────────────────────────────────────
now = datetime.now(timezone.utc).isoformat()

league_data = {
    "meta": {
        "name": LEAGUE_NAME,
        "is_public": IS_PUBLIC,
        "join_password_hash": None,
        "admin_password_hash": generate_password_hash(ADMIN_PASSWORD),
        "created_at": now,
        "template": "worldcup",
    },
    "players":  players,
    "settings": settings,
    "matches":  matches,
}

with open(os.path.join(LEAGUES_DIR, f"{code}.json"), "w") as f:
    json.dump(league_data, f, indent=2)

# ── Write index ───────────────────────────────────────────────────────────────
index = {}
if os.path.exists(INDEX_FILE):
    with open(INDEX_FILE) as f:
        index = json.load(f)

index[code] = {
    "name": LEAGUE_NAME,
    "is_public": IS_PUBLIC,
    "has_join_password": False,
    "created_at": now,
    "player_count": len(players),
    "match_count": len(matches),
}

with open(INDEX_FILE, "w") as f:
    json.dump(index, f, indent=2)

# ── Seed master from existing match results ───────────────────────────────────
# (only if master doesn't already exist)
if not os.path.exists(MASTER_FILE):
    master_matches = []
    for m in matches:
        master_matches.append({
            "id": m["id"],
            "name": m["name"],
            "datetime": m["datetime"],
            "venue": m.get("venue", ""),
            "round": m.get("round", ""),
            "result": m.get("result"),
            "predictions": {},
        })
    with open(MASTER_FILE, "w") as f:
        json.dump({"matches": master_matches}, f, indent=2)
    print(f"Seeded worldcup_master.json with {len(master_matches)} matches")

# ── Done ──────────────────────────────────────────────────────────────────────
print(f"\n✅ Migration complete!")
print(f"   League : {LEAGUE_NAME}")
print(f"   Code   : {code}")
print(f"   URL    : /league/{code}/")
print(f"   Admin  : /league/{code}/admin  (password: {ADMIN_PASSWORD})")
print(f"\nNext steps:")
print(f"  1. Note the code above")
print(f"  2. Merge dev → main and redeploy")
print(f"  3. In Railway, rename DATA_FILE → DATA_DIR (value: /app/data)")
