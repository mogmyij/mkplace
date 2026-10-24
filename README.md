# mkplace

Self-hosted Facebook Marketplace watcher. You describe what you want in plain English, and a
scheduler crawls Marketplace on your schedule. TypeSafe's **Jev** classification model (via
OpenRouter) then
separates **genuine listings** from the noise: wanted/ISO posts, dealer and reseller ads,
services and rentals, bait prices, and scams. Only genuine listings rank, so the top of the page
is what's worth messaging about.

Everything runs in **one container**: the web UI and the crawl scheduler share a process, and data
lives in `./data`. It runs on a Raspberry Pi 4/5.

```
mkplace container ─────────────────────────────────────────────────────┐
│ scheduler (every tick) ─▶ Playwright + FB cookies ─▶ Jev classifier  │
│        │                                                   │         │
│        └──────────────── SQLite (/data) ◀──────────────────┘         │
│                               ▲                                      │
│                 web UI + API on :8000                                │
└──────────────────────────────────────────────────────────────────────┘
```

## Deploy on a Raspberry Pi

**You need:** a Pi 4 or 5 with at least 2 GB of RAM (4 GB+ is more comfortable, since Chromium is
the heavy part). It must run a **64-bit** OS such as Raspberry Pi OS (64-bit) Bookworm or Ubuntu
Server arm64, because the Playwright/Chromium image only exists for arm64, not 32-bit armhf. Check
with `uname -m`, which should print `aarch64`.

1. **Install Docker** (one time):
   ```sh
   curl -fsSL https://get.docker.com | sh
   sudo usermod -aG docker $USER
   # log out and back in (or reboot) so the group change applies
   docker compose version   # should print v2.x
   ```

2. **Get the code and configure it:**
   ```sh
   git clone https://github.com/mogmyij/mkplace.git
   cd mkplace
   git checkout claude/fb-marketplace-crawler   # until this branch is merged
   cp .env.example .env
   nano .env
   ```
   Set at least:
   - `OPENROUTER_API_KEY`: create one at https://openrouter.ai/keys and add credit. Without it, a
     pattern-based fallback is used.
   - `TZ`: your timezone, e.g. `Australia/Sydney`. Active hours use it.
   - `APP_PASSWORD`: protects the UI with username `admin`. Recommended if anything other than
     your own LAN can reach the Pi.

3. **Build and start it.** The first build downloads about 2 GB and takes 5–15 minutes on a Pi:
   ```sh
   docker compose up -d --build
   docker compose logs -f    # Ctrl-C to stop following; the container keeps running
   ```
   `restart: unless-stopped` brings it back after reboots and crashes.

4. **Open the UI** at `http://<pi-ip>:8000`, or `http://raspberrypi.local:8000`. Find the IP with
   `hostname -I`.

5. **Add your Facebook cookies.** On your normal computer:
   - Log in to facebook.com, preferably with a secondary account.
   - Install the *Cookie-Editor* browser extension, open it on facebook.com, and choose
     **Export → JSON**.
   - In mkplace, click **⚙ Settings** and paste the export under *Facebook cookies*, then Save.

   It checks that the export contains a logged-in session (`c_user` and `xs`). When a search later
   shows "cookies are missing or expired", repeat this step.

6. **Create a search** with **+ New**. The first results arrive within a few minutes.

**Updating:**
```sh
cd mkplace && git pull && docker compose up -d --build
```

**Backups:** everything (database and cookies) is in `./data`. Copy that folder.

**Viewing it away from home:** don't port-forward it to the internet. Install
[Tailscale](https://tailscale.com/download/linux) on the Pi and your phone
(`curl -fsSL https://tailscale.com/install.sh | sh && sudo tailscale up`), then open
`http://<pi-tailscale-name>:8000`.

**Troubleshooting:**
- `docker compose logs --tail 100` shows each crawl and its result.
- The status pill at the top right says whether the scheduler is running, paused, or outside
  active hours, whether scoring uses Jev or the fallback, and how much Jev has cost so far.
- If the Pi runs short of memory, lower `DETAIL_FETCH_LIMIT` and `SCROLL_ROUNDS` in `.env`, then
  run `docker compose up -d`.

## Using it

- **+ New** creates a saved search. Write what you want, for example *"a PS5 disc edition under
  $350, ideally with 2 controllers, nothing modded"*.
  - Search keywords and price limits are picked out of the text automatically. You can override
    either one in the form.
  - The full sentence goes to Jev with every listing, so the extra details affect the score too.
- **Location** is whatever follows `/marketplace/` in FB's URL after you choose a location there,
  for example `sydney` or a numeric ID.
- Clicking a card opens it on Facebook and marks it as seen. Hover a card to star ★ or hide ✕ it.

### Crawl frequency

| Where | What |
|---|---|
| Each search → *Check every* | 10 min to daily, or any custom number of minutes (min 5) |
| ⚙ Settings → *Pause all* | Stops scheduled crawls. ↻ *Run now* still works |
| ⚙ Settings → *Only crawl between* | Active hours, e.g. 07:00–23:00, or overnight windows like 22:00–06:00 |
| ⚙ Settings → *Default interval* | Preselected interval for new searches |
| `.env` `WORKER_TICK_SECONDS` | How often the scheduler checks for due searches (default 60) |

Each search crawls when its interval has passed. Runs get a little random jitter so the requests
don't hit Facebook on an exact rhythm.

### Filters

- Listing type: genuine (default), wanted posts, business ads, services/rentals, bait prices,
  scams. Tick the junk types to see what was filtered out.
- Unseen/starred/hidden, sort by Jev score, newest, or price.
- Price range, minimum Jev score, date found, and price rating.

## How Jev scores a listing

Jev gets the buyer's request, budget, and the listing (title, price, location, description) and
answers four typed questions:

| Question | Jev type | Used for |
|---|---|---|
| What kind of post is this? genuine / wanted / business_ad / service / bait_price / scam | Choice | Listing-type filter; the score is multiplied by P(genuine) |
| How well does it match the request? | Score, 4 levels | 35% |
| Price vs. typical used price | Choice: great / fair / overpriced / unclear | 20% |
| Is it genuine, in budget, and worth messaging about? | Noul (probability) | 45% |

A dealer ad that is a perfect keyword match therefore still scores near zero.

All four questions go in **one request per new listing**, and only new listings are scored.
OpenRouter charges for input tokens only (outputs are free), and each listing is roughly a
thousand tokens. The running total from OpenRouter's `usage.cost` is shown in the status pill. The descriptions
of each post type are in `KIND_OPTIONS` in `app/jev.py`; edit them to teach Jev about patterns you
see in your area. Editing a search's wording or budget re-scores its listings.

## Configuration (`.env`)

| Variable | Default | |
|---|---|---|
| `OPENROUTER_API_KEY` | – | Calls Jev through OpenRouter. Without a Jev key, a regex/keyword fallback is used |
| `JEV_MODEL` | `~typesafe/jev-latest` | Pin a version such as `typesafe/jev-1.14` to keep scores stable |
| `TYPESAFE_API_KEY` | – | Alternative to OpenRouter: call TypeSafe directly (model default `jev-latest`) |
| `TZ` | `UTC` | Timezone for active hours |
| `DEFAULT_INTERVAL_MINUTES` | 30 | Initial default (then editable in Settings) |
| `ACTIVE_HOURS` | – | Initial active hours, e.g. `7-23` (then editable in Settings) |
| `APP_PASSWORD` | – | Basic-auth password for the UI (user `admin`) |
| `DETAIL_FETCH_LIMIT` | 12 | New listings per run whose page is opened to read the description |
| `SCROLL_ROUNDS` | 6 | How far down results the crawler scrolls (~20 listings per round) |
| `WORKER_TICK_SECONDS` | 60 | Scheduler check frequency |
| `RUN_WORKER` | 1 | Set to 0 to run the scheduler separately with `python -m app.worker` |

## Caveats

- Automated access breaks Facebook's terms. The account whose cookies you use could be
  rate-limited or checkpointed. Keep intervals reasonable (≥ 15 min) and the number of searches
  small. A Pi on your home connection is less likely to be blocked than a cloud server.
- FB changes its markup from time to time. Parsing depends only on `/marketplace/item/` links and
  their card text (`app/crawler.py`), which keeps it fairly sturdy, but it can still break.
- FB image URLs are signed and expire, so thumbnails on old listings may stop loading.

## Development

```sh
pip install -r requirements.txt pytest
pytest
DATA_DIR=data uvicorn app.web:app --reload
```
