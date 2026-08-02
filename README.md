# car-alert

Free listing watcher for **mobile.de** and **autoscout24.com**. Runs on GitHub
Actions every 30 minutes, keeps state in a committed SQLite file, and pushes new
matches to Telegram. No VPS, no paid APIs.

## Setup

1. Create a bot with [@BotFather](https://t.me/BotFather), send it a message,
   and read your chat id from
   `https://api.telegram.org/bot<TOKEN>/getUpdates`.
2. Repo → Settings → Secrets and variables → Actions:
   `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID`.
3. Edit `config/searches.yml`.
4. Seed the database once so the first real run does not fire hundreds of
   messages — Actions → *scrape* → *Run workflow* with **seed = true**, or
   locally:

   ```bash
   pip install -r requirements.txt
   python main.py --seed
   ```

From then on the cron takes over.

## Local usage

```bash
python main.py --dry-run      # scrape + match, print messages instead of sending
python main.py                # real run (needs the two env vars)
python main.py --max-pages 1  # shallower scrape
python fetch_mobile_makes.py  # refresh mobile.de make ids in config/brands.json
```

## Search profiles

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
- **Logging** — one line per source per search:
  `fetched / new / matched / to notify`.

## Layout

```
.github/workflows/scrape.yml   cron, run, commit listings.db back
config/brands.json             314 brands: autoscout24 slug + mobile.de makeId
config/searches.yml            your search profiles
scrapers/base.py               HTTP, UA rotation, throttling, Listing schema
scrapers/autoscout24.py
scrapers/mobilede.py
core/db.py                     SQLite: listings, notifications_sent, queue
core/matcher.py                profile matching
core/notifier.py               Telegram sending + caption building
fetch_mobile_makes.py          resolves missing mobile.de make ids
main.py                        orchestrator
```

## Normalized schema

`{id, source, brand, model, year, mileage_km, price_eur, fuel, gearbox, url, image_url, first_seen}`

## Caveats

- Both sites are scraped through undocumented endpoints; a layout or API change
  breaks a source (logged as a warning, never a crash).
- The `X-Mobile-Client` header is what makes mobile.de answer at all. If they
  start rejecting it, that source goes dark until the header/endpoint is
  updated — swapping in Playwright against `suchen.mobile.de` is the fallback.
- The db is committed on every run, so the repo grows one small commit per
  30 minutes. Squash the history occasionally if that bothers you.
