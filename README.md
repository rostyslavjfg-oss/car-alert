# car-alert

Free listing watcher for **mobile.de**, **autoscout24.com**, **bazos.sk** and
**willhaben.at** with a Russian-language Telegram bot front end. Runs on GitHub Actions every 30 minutes, keeps state in a
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
| `/add` | 8-step dialog: brands → models → year → price → mileage → fuel → gearbox → countries |
| `/list` | your searches, each with ⏸ Pause / 🗑 Delete buttons |
| `/del <id>` `/pause <id>` `/resume <id>` | same, by id |
| `/fav` | saved listings |
| `/dealers` | hidden sellers; `/dealers <key>` unhides one |
| `/run` | scrape immediately (only when `bot.py` is running) |
| `/status` | counters and last run time |
| `/cancel` | abort the `/add` dialog |

**Brands, models, fuel, gearbox and countries are multi-select** — tap to toggle,
«Готово» closes the step. Anything typed by hand is added to the selection, so a
make or model missing from the list still works. The models step is skipped when
more than one brand is picked: models belong to one make, and asking for
"BMW + Audi → 320 or A4" would need a per-brand sub-dialog.

One search with several makes becomes several requests per source (brand and
model are single-valued on every site), capped at 12 combinations per source per
run — anything dropped is logged, never silently. Fuel and gearbox go the other
way: one value is passed to the site's own filter, several are filtered locally
instead of multiplying requests.

A persistent keyboard sits under the input field, so nothing has to be typed:
**➕ Новый поиск · 📋 Мои поиски · 🔍 Искать сейчас · ❤️ Избранное ·
📊 Статус · 🚫 Скрытые · ❓ Помощь**. During `/add` it is replaced by
**— пропустить / ✖️ Отмена** (the skip button is hidden on the brand step, which
is the one answer that cannot be skipped). A tap is handled before the dialog
reads the message, so pressing Отмена mid-dialog cancels instead of being
swallowed as an answer.

Every alert arrives as an album of up to 5 photos, followed by one message with
the details and three buttons: **❤️ Save**, **🚫 Hide seller**, **🔕 Mute**
(silences that search without deleting it). Telegram rejects `reply_markup` on
`sendMediaGroup`, which is why the album and the buttons are two messages —
keeping the caption out of the album puts text and actions together. Listings
with a single photo (bazos.sk) stay one message.

### Two ways to run the bot

**Zero hosting (default, and the only consistent one).** The Actions run calls
`main.py --drain`, so commands are handled by the same job that owns the
database. Replies land within the cron interval (10 minutes).

**Do not run `bot.py` alongside the cron.** The state lives in `listings.db`,
which the Actions job commits. A locally running bot writes to *its own copy*:
searches added there never reach the cron, so they silently produce no alerts,
and the two files cannot be merged (SQLite is binary). Run `bot.py` only if you
turn the cron off, or move the state to a shared database first.

**Instant replies** — only with the cron disabled:

```bash
export TELEGRAM_BOT_TOKEN=...
python bot.py
```

Then set the repo variable `SELF_HOSTED_BOT=true` so the workflow stops draining.
Telegram allows exactly one `getUpdates` consumer; running both gives HTTP 409.

## Swipe Mini App

`webapp/index.html` is a Tinder-style card deck served as a Telegram Mini App:
drag right to like, left to pass, tap the photo edges to flip through the 5
pictures, ↩ undoes the last card. Likes land in **❤️ Избранное**, passes are
remembered so the same ad never comes back.

It needs a static https host, because Telegram will not open a Mini App from a
local file:

1. Push the repo to GitHub, Settings → Pages → deploy from `main` / root.
2. Repo → Settings → Secrets and variables → Actions → **Variables**:
   `WEBAPP_URL = https://<user>.github.io/<repo>/webapp`
3. Send `/start` again — a **🔥 Свайпать** button appears above the keyboard.

Any static host works (Vercel, Cloudflare Pages); only the URL changes.

**How the data moves.** Each run writes `webapp/data/<token>.json` — the deck of
listings already alerted to that chat and not yet swiped — and commits it. The
file name is a random per-chat token because everything under `webapp/` is
world-readable on a public repo. Verdicts travel back through
`Telegram.WebApp.sendData()`, which only works for a Mini App opened from a
**reply-keyboard** button; an inline button cannot report back, which is why the
swipe entry point lives on the bottom keyboard.

## Tests

```bash
python tests.py          # 86 offline checks, no network
python tests.py --live   # + 16 checks against the four live sites
```

Covers parsing, brand resolution, damage detection incl. Slovak negations, the
matcher, profile expansion, the db round-trip, captions, the whole /add dialog
driven by synthetic Telegram updates, keyboard wiring and country gating.

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

| | autoscout24 | mobile.de | bazos.sk | willhaben.at | sauto.cz | otomoto.pl | openlane.eu |
|---|---|---|---|---|---|---|---|
| Countries | D A B NL L I E F | + SK CZ PL | SK | A | CZ | PL | all EU |
| Data | `__NEXT_DATA__` | native JSON | HTML | `__NEXT_DATA__` | public JSON API | `<article data-id>` HTML | internal JSON API |
| Currency | EUR | EUR | EUR | EUR | **CZK** | **PLN** | EUR |
| Auth | none | `X-Mobile-Client` header | none | none | none | none | headless browser |

`sauto.cz` and `otomoto.pl` quote local currency; both are converted with the
ECB daily reference rate, cached in the db for a day (`core/currency.py`).
Without that, `price_max` on those two would be nonsense.

Two taxonomy traps, both handled:
- sauto has no manufacturer filter (only free-text `phrase`) and leaves
  `manufacturing_date` empty on most ads with no server-side year filter, so the
  build year is topped up from its detail endpoint — capped at 40 lookups a run.
  Falling back to `create_date` would have stamped everything 2026 and sailed
  past every `year_from`.
- otomoto knows `seria-3`, not `320`. The scraper maps numeric BMW/Mercedes
  models to their family and marks those listings `model_relaxed`, so the
  matcher trusts otomoto's own filter instead of dropping all of them. A "320"
  search therefore returns the whole 3 series from Poland.

### openlane.eu — opt-in, needs a browser

A B2B auction site behind Cloudflare: plain requests get 403, and so does
Playwright's own request context. What works is a headless Chromium that opens
the page and then calls the site's search endpoint **from inside it**, reusing
the page cookies and anti-forgery token.

```bash
pip install playwright && playwright install chromium
OPENLANE=1 python main.py
```

Without `OPENLANE=1` the source reports `serves() == False` and is skipped
silently. It stays off on GitHub Actions on purpose — no browser, and a
datacenter IP that Cloudflare rates poorly.

Reverse-engineered query keys: `MakeModels`, `PriceRange`,
`RegistrationYearRange`. Mileage, fuel and gearbox keys were not found and are
filtered locally. These are **auctions**: the price shown is the buy-now price
when the lot has one, otherwise the current bid, which can still climb.

A source only runs when the search's `countries` overlap the ones it serves, so
a `["SK"]` search never touches autoscout24 and a `["D"]` search never touches
bazos. Passing a country a site does not know (`cy=D,A,SK` on autoscout24)
silently returns zero results, so unsupported codes are filtered out per source.

**Data quality differs.** autoscout24, mobile.de and willhaben return structured
fields. bazos.sk has none — year, mileage, fuel and gearbox are parsed out of the
seller's free text and stay `None` when they aren't written. The matcher treats
an unknown field as "don't care", so a terse ad is never silently dropped.

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
- **Damaged cars** — excluded by default (`exclude_damaged`). mobile.de filters
  them server-side with `dam=false`; autoscout24 has no such parameter but marks
  `isCurrentlyDamaged`, willhaben is read from its condition fields, and bazos.sk
  only has the seller's own words. Negations are stripped before matching there,
  because "NEBÚRANÉ" and "bez poškodenia" mean the opposite and a false positive
  silently hides a good car. An unstated condition is never treated as damaged.
- **Country** — every alert shows 🇩🇪/🇦🇹/🇸🇰 plus the city where the source
  gives one.
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

`{id, source, brand, model, year, mileage_km, price_eur, fuel, gearbox, url, image_url, images, title, dealer_id, dealer_name, first_seen}`

`images` is a JSON array of up to 5 photo urls (`image_url` stays as the cover
for one-photo fallbacks). bazos.sk only exposes a thumbnail in its result list —
more would cost one extra request per ad.

`dealer_id`/`dealer_name` back the "Hide seller" button. On mobile.de **every
private seller shares one bucket `sellerId`** (7723851 today), so only real
dealers get a blockable id there — otherwise one tap would hide every private
ad. autoscout24 gives private sellers real ids, so they can be hidden.

## Facebook Marketplace

Not implemented, and not because of effort. It answers **HTTP 400** to any
request without a logged-in session, so scraping it needs your personal Facebook
cookies, a headless browser, and continuous maintenance against Meta's
anti-automation. That also breaks their ToS and puts the account at risk of
being restricted. bazos.sk covers the same need — private sellers, Slovakia —
with a public page and no account on the line.

If you still want it, say so: it would be a Playwright scraper reading
`FB_SESSION_COOKIE` from the environment, and it should use a throwaway account.

## Caveats

- Both sites are scraped through undocumented endpoints; a layout or API change
  breaks a source (logged as a warning, never a crash).
- The `X-Mobile-Client` header is what makes mobile.de answer at all. If they
  start rejecting it, that source goes dark until the header/endpoint is
  updated — swapping in Playwright against `suchen.mobile.de` is the fallback.
- The db is committed on every run, so the repo grows one small commit per
  30 minutes. Squash the history occasionally if that bothers you.
