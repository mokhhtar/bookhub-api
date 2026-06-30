# BookHub API

FastAPI backend powering BookHub's AI tools. Every tool is built on a
**grounding-first pipeline**: real book data is fetched from Google Books /
Open Library BEFORE any AI call, so summaries are anchored to verified
facts instead of the model's possibly-wrong memory of a book.

## Architecture

```
bookhub-api/
├── main.py            # thin entrypoint — mounts each tool's router
├── book_data.py        # SHARED grounding layer: Google Books → Open Library fallback
├── gemini_client.py     # SHARED Gemini 3.1 Flash-Lite client (low thinking, temp 0.3)
├── cache.py             # SHARED 30-day cache (in-memory + disk)
├── tools/
│   └── summary.py       # Book Summarizer — fully self-contained tool module
└── render.yaml
```

**Each tool in `tools/` is independent** — its own request model, its own
prompt, its own route, in its own file. Tools import the shared
`book_data` / `gemini_client` / `cache` modules but never import each
other. This means you can rebuild, A/B test, or delete one tool without
touching any other.

## Why grounding matters

Asking Gemini to summarize a book "from memory" produces plausible-sounding
but often **wrong** content — the model blends general knowledge of the
genre with the title and invents specifics. `book_data.py` fixes this by
verifying the book exists and injecting its REAL publisher description
into the prompt as mandatory context, with explicit rules forbidding the
model from adding anything not present in that context.

If a book can't be verified in either source, the API returns
`{"found": false, "message": "..."}` instead of asking Gemini to guess.

## Active endpoints

| Method | Path       | Status | Notes                                          |
|--------|------------|--------|--------------------------------------------------|
| POST   | `/summary` | ✅ Live | Grounded summary + real similar books            |
| GET    | `/health`  | ✅ Live | Uptime check (used by UptimeRobot ping)           |

Other tools (`recommend`, `questions`, `compare`) are intentionally **not
mounted yet** — see `main.py` comments. They'll be rebuilt with the same
grounding pipeline as `/summary` before being re-enabled, one at a time.

### POST /summary

Request:
```json
{ "title": "Atomic Habits", "author": "James Clear", "depth": "quick" }
```
`depth` is one of `quick` / `medium` / `deep`. `author` is optional.

Response (found):
```json
{
  "found": true,
  "source": "google_books",
  "title": "Atomic Habits",
  "author": "James Clear",
  "depth": "quick",
  "summary": "...",
  "category": "Self-Help",
  "page_count": 320,
  "published_year": "2018",
  "cover_url": "https://...",
  "average_rating": 4.5,
  "isbn_13": "9780735211292",
  "similar_books": [
    { "title": "...", "author": "...", "cover_url": "..." }
  ]
}
```

Response (not found):
```json
{ "found": false, "title": "...", "author": "...", "message": "We couldn't verify ..." }
```

## Local development

```bash
python -m venv venv
source venv/bin/activate          # Windows: venv\Scripts\activate
pip install -r requirements.txt

cp .env.example .env
# edit .env and add your real GEMINI_API_KEY

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
3. Free tier on `gemini-3.1-flash-lite` is generous for this use case

Google Books works without a key at low volume; set `GOOGLE_BOOKS_API_KEY`
if you outgrow the unauthenticated rate limit (get one in Google Cloud
Console → Enable "Books API" → Credentials).

## Deploy to Render

1. Push this folder to its own GitHub repo (e.g. `bookhub-api`)
2. https://render.com → New → Web Service → connect the repo
3. Render auto-detects `render.yaml`
4. In Render dashboard → Environment, set:
   - `GEMINI_API_KEY`
   - `GOOGLE_BOOKS_API_KEY` (optional)
   - `AMAZON_TAG`
   - `ALLOWED_ORIGINS` = your GitHub Pages URL
5. Deploy — first build ~2 minutes

## Keep it awake (free tier sleeps after 15 min idle)

UptimeRobot (free) → New Monitor → HTTP(s) →
`https://your-app.onrender.com/health` → every 5 minutes.
This hits `/health` only — never an AI endpoint — so it costs zero Gemini quota.

## Before generating any SEO pages

Per the agreed plan: **do not** start programmatic page generation until
`/summary` has been manually tested against 15-20 real books spanning
bestsellers, mid-tier titles, and obscure/older books, with a measured
real success rate. Grounding reduces hallucination but Google Books still
won't have every title — the `found: false` path needs to be seen working
correctly in practice first.

## Adding the next tool

Copy the shape of `tools/summary.py`:
1. New file `tools/<name>.py` with its own `APIRouter()`, Pydantic model, and grounded prompt.
2. Call `book_data.resolve_book(...)` before any Gemini call — never skip grounding.
3. Import `gemini_client.generate(...)` for the actual call.
4. Cache the result via `cache.get()` / `cache.set()`.
5. In `main.py`, uncomment/add the import and `app.include_router(...)` line.
