# World Cup 2026 Prediction League

A web app where a group of friends each predict match scores for the 2026 World Cup. Points are awarded for correct outcomes and exact scores.

## How scoring works

| Result | Points |
|---|---|
| Correct outcome (win/draw/loss) | 1 pt (configurable) |
| Exact score | +3 pts bonus (configurable) |

Predictions appear color-coded on the scoreboard:
- 🟦 ★ **Blue with gold star** — exact score
- 🟩 **Green** — correct outcome only
- ⬜ **Gray** — wrong

## Running locally

```bash
# Install dependencies
pip install -r requirements.txt

# Start the dev server
python app.py
```

Open [http://localhost:5000](http://localhost:5000).

## Deploying to Railway

1. Fork/push this repo to GitHub.
2. Create a new project on [Railway](https://railway.app) from your GitHub repo, pointing to the `webapp/` directory.
3. Add a persistent volume mounted at `/app/data`.
4. Set these environment variables in Railway:

| Variable | Description |
|---|---|
| `ADMIN_PASSWORD` | Password for the `/admin` panel |
| `SECRET_KEY` | Random secret string for Flask sessions |
| `DATA_FILE` | `/app/data/worldcup_data.json` |

5. Deploy — Railway uses the `Procfile` (`gunicorn app:app`) automatically.

## First-time setup (after deploy)

1. Go to `/admin` and log in with your `ADMIN_PASSWORD`.
2. **Add players** — one name per person in your group.
3. **Import matches** — upload the World Cup 2026 `.ics` calendar file (from fotmob or similar) or paste a URL.
4. Share the site URL with your friends — they pick their name and enter predictions.

## Admin panel

- **Record results** — enter the final score for a completed match; points are calculated automatically.
- **Edit predictions** — admin can correct any player's prediction before or after a match.
- **Point settings** — change how many points each result type is worth.

## Data

All data is stored in a single JSON file (`worldcup_data.json`). On Railway this lives on the persistent volume so it survives redeploys.
