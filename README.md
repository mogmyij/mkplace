# mkplace

Self-hosted Facebook Marketplace watcher. You describe what you want in plain English. A background
worker crawls Marketplace on a schedule, and TypeSafe's **Jev** classification model scores every
listing on whether it's worth your time. Results appear in a Marketplace-style UI with filters.

```
┌──────────┐   every N min    ┌──────────────┐   new listings   ┌───────────┐
│  worker  │ ───────────────▶ │  Playwright  │ ───────────────▶ │    Jev    │
│ (APSched)│                  │  + FB cookies│                  │ classifier│
└────┬─────┘                  └──────────────┘                  └─────┬─────┘
     └───────────────────────────── SQLite (data/) ◀──────────────────┘
                                         ▲
                              web (FastAPI + UI) :8000
```

## Setup

1. **Configure**
   ```sh
   cp .env.example .env
   ```
   Set `TYPESAFE_API_KEY` (from the TypeSafe console). Without it a simple keyword/price heuristic
   is used instead, so you can try everything else first.

2. **Export Facebook cookies.** Log in to facebook.com in a browser, preferably on a secondary
   account. Export the cookies with an extension such as *Cookie-Editor* (Export → JSON), and save
   them as `data/cookies.json`. Re-export whenever the UI reports that cookies expired.

3. **Run**
   ```sh
   docker compose up -d --build
   ```
   Open http://localhost:8000 (or your server's IP). Set `APP_PASSWORD` in `.env` if the server
   can be reached from other machines; the username is `admin`.

## Using it

- **+ New** creates a saved search. Write what you want, for example *"a PS5 disc edition under
  $350, ideally with 2 controllers, nothing modded"*.
  - Search keywords (`PS5 disc edition`) and price limits (`≤ 350`) are picked out of the text
    automatically. You can override either one in the form.
  - The full sentence goes to Jev with every listing, so details like "2 controllers" or "nothing
    modded" affect the score even though they aren't part of the FB search.
- **Location** is whatever follows `/marketplace/` in FB's URL after you choose a location there,
  for example `sydney`, `nyc`, or a numeric ID.
- Each search runs on its own interval. ↻ queues an immediate run, which the worker picks up
  within about a minute.
- Filters in the sidebar: unseen/starred/hidden, sort order, price range, minimum score, date
  found, Jev's price rating, and hiding likely scams. Hover a card to star ★ or hide ✕ it. Clicking
  a card opens it on Facebook and marks it as seen.

## How the score works

For each new listing (title, price, location, description), Jev answers four questions:

| Question | Type | Feeds into |
|---|---|---|
| How well does it match the request? | Score, 4 levels | 35% |
| Price vs. typical used price | Choice: great / fair / overpriced / unclear | 20% |
| Worth the buyer's time? | Noul (probability) | 45% |
| Scam or bait signals? | Noul | multiplies the score by `(1 - 0.8 × p)` |

Listings with scam probability ≥ 0.5 are hidden by default. The weights live in `app/jev.py`
(`combine`). Editing a search's wording or budget re-scores its listings.

## Tuning (`.env`)

| Variable | Default | |
|---|---|---|
| `DETAIL_FETCH_LIMIT` | 12 | New listings per run whose page is opened to read the description |
| `SCROLL_ROUNDS` | 6 | How far down the results the crawler scrolls (~20 listings per round) |
| `JEV_MODEL` | `jev-latest` | Pin a version such as `jev-1.13.0` to keep scores stable |
| `HEADLESS` | 1 | Set to 0 to watch the browser while debugging (needs a display) |

## Caveats

- Automated access breaks Facebook's terms. The account whose cookies you use could be
  rate-limited or checkpointed. Keep intervals reasonable (≥ 15 min) and the number of searches
  small.
- FB changes its markup from time to time. Parsing depends only on `/marketplace/item/` links and
  their card text (`app/crawler.py`), which keeps it fairly sturdy, but it can still break.
- FB image URLs are signed and expire after a while, so thumbnails on old listings may stop
  loading.

## Development

```sh
pip install -r requirements.txt pytest
pytest
DATA_DIR=data uvicorn app.web:app --reload     # UI
DATA_DIR=data python -m app.worker             # scheduler
```
