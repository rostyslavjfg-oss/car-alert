# car-alert

Free listing watcher for **mobile.de** and **autoscout24.com** with a Telegram
bot front end. Runs on GitHub Actions every 30 minutes, keeps state in a
committed SQLite file, pushes matches to your chat. No VPS, no paid APIs.

## Setup

1. Create a bot with [@BotFather](https://t.me/BotFather) and copy the token.
2. Repo → Settings → Secrets and variables → Actions → **Secrets**:
   `TELEGRAM_BOT_TOKEN`. Optionally `TELEGRAM_CHAT_ID` — only needed if you want
   `config/searches.yml` imported automatically on the first run.
3. Send `/start` to your bot, then `/add` to create a search.
4. Seed the database once so the first real run does not fire hundreds of
   messages — Actions → *scrape* → *Run workflow* with **seed = true**, or
   locally `python main.py --seed`.

From then on the cron takes over.

## Telegram bot

| command | what it does |
|---|---|
| `/start` | registers your chat |
| `/add` | 8-step dialog: brand → model → year → price → mileage → fuel → gearbox → countries (`-` skips a step) |
| `/list` | your searches, each with ⏸ Pause / 🗑 Delete buttons |
| `/del <id>` `/pause <id>` `/resume <id>` | same, by id |
| `/fav` | saved listings |
| `/dealers` | hidden sellers; `/dealers <key>` unhides one |
| `/run` | scrape immediately (only when `bot.py` is running) |
| `/status` | counters and last run time |
| `/cancel` | abort the `/add` dialog |

Every alert carries three buttons: **❤️ Save**, **🚫 Hide seller**,
**🔕 Mute** (silences that search without deleting it).

### Two ways to run the bot

**Zero hosting (default).** The Actions run calls `main.py --drain`, which
processes everything you sent since the last cycle. Commands work, but replies
land up to 30 minutes later.

**Instant replies.** Run the long-polling process wherever you like — your Mac,
a free tier VM:

```bash
export TELEGRAM_BOT_TOKEN=...
python bot.py
```

Then set the repo variable `SELF_HOSTED_BOT=true` so the workflow stops draining.
Telegram allows exactly one `getUpdates` consumer; running both gives HTTP 409.

## Local usage

```bash
python main.py --dry-run      # scrape + match, print messages instead of sending
python main.py                # real run (needs TELEGRAM_BOT_TOKEN)
python main.py --drain        # also process pending bot commands
python main.py --max-pages 1  # shallower scrape
python bot.py                 # long-polling bot
python fetch_mobile_makes.py  # refresh mobile.de make ids in config/brands.json
```

## Search profiles

Searches live in the `searches` table, written by the bot. `config/searches.yml`
is imported once, only while that table is empty and `TELEGRAM_CHAT_ID` is set:

```yaml
searches:
  - name: "bmw-3-diesel"
    brand: "BMW"          # must match a key in config/brands.json
    model: "320"          # optional, resolved to each site's model id
    year_from: 2018
    mileage_max: 150000
    price_max: 20000
    price_min: 5000       # optional
    fuel: "diesel"        # diesel | petrol | hybrid | electric
    gearbox: "automatic"  # automatic | manual
    countries: ["D", "A"] # autoscout24 codes, mapped to ISO for mobile.de
```

Filters are applied server-side on both sites and re-checked locally in
`core/matcher.py`, so a loosened server filter can't leak junk into Telegram.

## How each source is read

| | autoscout24 | mobile.de |
|---|---|---|
| Endpoint | `/lst/{brand}/{model}` HTML | `www.mobile.de/svc/s/` JSON |
| Data | `__NEXT_DATA__` JSON blob in the page | native JSON |
| Sort | `sort=age&desc=1` | `sb=ct&od=down` |
| Paging | `page=1..3` | none — `psz` up to 200 in one request |
| Auth | none | `X-Mobile-Client: de.mobile.android.app` |

`suchen.mobile.de/fahrzeuge/search.html` answers **403** to non-browser clients,
which is why the app-facing service is used instead. It returns the same ads
with no HTML parsing and no Playwright.

Because that service has no page parameter, `--max-pages N` becomes a single
request with `psz = min(50·N, 200)`.

## Behaviour

- **Dedupe** — primary key is `{source}:{listing_id}`. "New" = not in the db.
- **Stop early** — results are newest-first; autoscout24 stops paginating as
  soon as a page contains an already-known ad (hard cap 3 pages).
- **Anti-block** — rotating desktop User-Agents, 2–4 s random gap between
  requests, `Accept-Language: de-DE`. A bot-wall or HTTP error logs a warning
  and skips that source for that profile; the run continues.
- **Rate limit** — max 20 Telegram messages per run. The rest go to
  `notification_queue` and are sent first on the next run.
- **Price drops** — known ads that reappear with a price ≥5 % lower trigger a
  `PRICE DROP -x%: old → new` message (once per listing per search).
- **Hidden sellers** — blocked seller ids are filtered before matching, per chat.
- **Logging** — one line per source per search:
  `fetched / new / matched / to notify`.

## Layout

```
.github/workflows/scrape.yml   cron, run, commit listings.db back
config/brands.json             314 brands: autoscout24 slug + mobile.de makeId
config/searches.yml            optional starting profiles (imported once)
scrapers/base.py               HTTP, UA rotation, throttling, Listing schema
scrapers/autoscout24.py
scrapers/mobilede.py
core/db.py                     SQLite: listings, searches, favorites, queue, ...
core/matcher.py                profile matching
core/notifier.py               Telegram sending + caption + inline buttons
core/telegram_app.py           bot commands and callbacks
bot.py                         long-polling bot process
fetch_mobile_makes.py          resolves missing mobile.de make ids
main.py                        orchestrator
```

## Normalized schema

`{id, source, brand, model, year, mileage_km, price_eur, fuel, gearbox, url, image_url, dealer_id, dealer_name, first_seen}`

`dealer_id`/`dealer_name` back the "Hide seller" button. On mobile.de **every
private seller shares one bucket `sellerId`** (7723851 today), so only real
dealers get a blockable id there — otherwise one tap would hide every private
ad. autoscout24 gives private sellers real ids, so they can be hidden.

## Caveats

- Both sites are scraped through undocumented endpoints; a layout or API change
  breaks a source (logged as a warning, never a crash).
- The `X-Mobile-Client` header is what makes mobile.de answer at all. If they
  start rejecting it, that source goes dark until the header/endpoint is
  updated — swapping in Playwright against `suchen.mobile.de` is the fallback.
- The db is committed on every run, so the repo grows one small commit per
  30 minutes. Squash the history occasionally if that bothers you.
