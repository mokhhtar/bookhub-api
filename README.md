# BookHub API

FastAPI backend powering BookHub's AI tools, using **Gemini 1.5 Flash**
(free tier: 1,500 requests/day). Deployed on **Render** (free plan).

## Endpoints

| Method | Path          | Purpose                                  |
|--------|---------------|-------------------------------------------|
| POST   | `/summary`    | Book summary — quick / medium / deep      |
| POST   | `/questions`  | 10 book-club discussion questions         |
| POST   | `/recommend`  | 5 recommendations from favorite books     |
| POST   | `/similar`    | 4 books similar to a given title          |
| POST   | `/compare`    | Side-by-side comparison of two books      |
| GET    | `/health`     | Uptime check (used by UptimeRobot ping)   |

Interactive docs at `/docs` once deployed.

## Local development

```bash
python -m venv venv
source venv/bin/activate          # Windows: venv\Scripts\activate
pip install -r requirements.txt

cp .env.example .env
# edit .env and add your real GEMINI_API_KEY

# load .env then run:
export $(cat .env | xargs)        # macOS/Linux
uvicorn main:app --reload --port 8000
```

Test it:
```bash
curl http://localhost:8000/health
curl -X POST http://localhost:8000/summary \
  -H "Content-Type: application/json" \
  -d '{"title":"Atomic Habits","author":"James Clear","depth":"quick"}'
```

## Get a free Gemini API key

1. Go to https://aistudio.google.com/apikey
2. Sign in → "Create API key"
3. Copy the key — free tier gives 1,500 requests/day on `gemini-1.5-flash`

## Deploy to Render

1. Push this folder to its own GitHub repo (e.g. `bookhub-api`)
2. Go to https://render.com → New → Web Service → connect the repo
3. Render auto-detects `render.yaml` — confirm settings
4. In the Render dashboard → Environment, set:
   - `GEMINI_API_KEY` = your key from AI Studio
   - `AMAZON_TAG` = your Amazon Associates tracking ID
   - `ALLOWED_ORIGINS` = your GitHub Pages URL (e.g. `https://yourusername.github.io`)
5. Deploy — first build takes ~2 minutes

## Keep it awake (Render free tier sleeps after 15 min idle)

Use **UptimeRobot** (free, no card required):
1. https://uptimerobot.com → sign up
2. Add New Monitor → HTTP(s)
3. URL: `https://your-app.onrender.com/health`
4. Interval: **5 minutes**

This pings `/health` (not an AI endpoint) so it never wastes a Gemini quota call.

## Caching

Every AI response is cached for 30 days (in-memory + `/tmp` disk) keyed by the
exact input. The same book summary requested 1,000 times only calls Gemini once.
See `cache.py` — there's a commented-out Upstash Redis option ready to enable
if you outgrow the per-instance cache (e.g. multiple Render instances).

## Connecting the frontend

In your Jekyll site's `_config.yml`, set:

```yaml
api_url: "https://your-app.onrender.com"
```
