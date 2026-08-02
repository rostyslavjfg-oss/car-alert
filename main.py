#!/usr/bin/env python3
"""Orchestrator: scrape -> dedupe -> match -> notify.

    python main.py --seed      first run: fill the db, send nothing
    python main.py             normal run
    python main.py --dry-run   scrape and match, print instead of sending
    python main.py --drain     also process pending bot commands (Actions mode)

Searches come from the `searches` table, which the Telegram bot writes. On an
empty table config/searches.yml is imported once as a starting point.
"""

import argparse
import asyncio
import json
import logging
import os
import sys
from pathlib import Path

import yaml

from core.db import Db, load_env, now
from core.matcher import filter_matching
from core.notifier import Alert, Notifier, MAX_PER_RUN
from fetch_mobile_makes import load_brands, resolve_missing, save_brands
from scrapers.autoscout24 import AutoScout24Scraper
from scrapers.base import BotWallError
from scrapers.bazos import BazosScraper
from scrapers.mobilede import MobileDeScraper
from scrapers.willhaben import WillhabenScraper

# every source is tried for a search unless its country is not in the profile
SOURCES = [AutoScout24Scraper, MobileDeScraper, BazosScraper, WillhabenScraper]

ROOT = Path(__file__).resolve().parent
SEARCHES_PATH = ROOT / "config" / "searches.yml"
PRICE_DROP_THRESHOLD = 0.05          # notify on >= 5% drop

log = logging.getLogger("car-alert")


def import_yaml_searches(db: Db) -> int:
    """Seed the searches table from searches.yml - only while it is empty."""
    if db.list_searches() or not SEARCHES_PATH.exists():
        return 0
    chat_id = os.environ.get("TELEGRAM_CHAT_ID")
    if not chat_id:
        log.warning("searches table empty and TELEGRAM_CHAT_ID unset - nothing to scrape. "
                    "Send /start then /add to the bot, or set the env var.")
        return 0
    with open(SEARCHES_PATH, encoding="utf-8") as fh:
        profiles = (yaml.safe_load(fh) or {}).get("searches") or []
    for profile in profiles:
        db.add_search(chat_id, profile)
    if profiles:
        log.info("imported %d searches from config/searches.yml", len(profiles))
    return len(profiles)


MAX_COMBOS = 12          # brand x model requests per source per run


def expand_profile(profile: dict) -> list:
    """One search may span several makes and models; sources take one of each.

    Fuel and gearbox stay in the profile instead: with a single value the source
    filters server-side, with several it is cheaper to filter locally than to
    multiply the requests.
    """
    brands = profile.get("brands") or ([profile["brand"]] if profile.get("brand") else [])
    models = profile.get("models") or ([profile["model"]] if profile.get("model") else [])
    fuels = profile.get("fuels") or []
    gearboxes = profile.get("gearboxes") or []

    combos = []
    for brand in brands:
        # models belong to one make, so they only apply when there is one
        for model in (models if len(brands) == 1 and models else [None]):
            combos.append({**profile, "brand": brand, "model": model,
                           "fuel": fuels[0] if len(fuels) == 1 else None,
                           "gearbox": gearboxes[0] if len(gearboxes) == 1 else None})
    if len(combos) > MAX_COMBOS:
        log.warning("  search %s: %d brand/model combos, keeping the first %d",
                    profile.get("name"), len(combos), MAX_COMBOS)
        combos = combos[:MAX_COMBOS]
    return combos


def scrape_profile(scraper, profile: dict, max_pages: int, seen_ids) -> list:
    """Never let one source/profile failure kill the run."""
    try:
        return scraper.search(profile, max_pages=max_pages, seen_ids=seen_ids)
    except BotWallError as exc:
        log.warning("  %-12s %s: blocked (%s)", scraper.source, profile["name"], exc)
    except ValueError as exc:
        log.warning("  %-12s %s: skipped (%s)", scraper.source, profile["name"], exc)
    except Exception as exc:
        log.warning("  %-12s %s: failed (%s: %s)", scraper.source, profile["name"],
                    type(exc).__name__, exc)
    return []


def collect_alerts(db: Db, brands: dict, seed: bool, max_pages: int) -> list:
    """Scrape every active search and return the alerts that should go out."""
    scrapers = [cls(brands) for cls in SOURCES]
    pending = []

    for row in db.dequeue_all():           # overflow from the previous run goes first
        search = db.get_search(row["search_id"]) if row.get("search_id") else None
        pending.append(Alert(row=row, chat_id=row.get("chat_id") or (search or {}).get("chat_id"),
                             search_name=row["search_name"], search_id=row.get("search_id"),
                             old_price=row.get("old_price"), kind=row["kind"]))

    for profile in db.list_searches(active_only=True):
        name, chat_id = profile["name"], profile["chat_id"]
        blocked = db.blocked_dealers(chat_id)
        log.info("search #%s %s", profile["id"], name)
        combos = expand_profile(profile)
        for scraper in scrapers:
            if not scraper.serves(profile.get("countries")):
                continue
            seen_ids = db.known_ids(scraper.source)
            fetched, by_id = [], set()
            for combo in combos:          # several makes -> several requests
                for listing in scrape_profile(scraper, combo, max_pages, seen_ids):
                    if listing.id not in by_id:
                        by_id.add(listing.id)
                        fetched.append(listing)
            new = [l for l in fetched if l.id not in seen_ids]
            matched = [l for l in filter_matching(new, profile) if l.dealer_key not in blocked]
            known = [l for l in filter_matching([l for l in fetched if l.id in seen_ids], profile)
                     if l.dealer_key not in blocked]

            alerts = []
            if not (seed or profile["muted"]):
                for listing in matched:
                    if not db.already_notified(listing.id, name, "new"):
                        alerts.append(Alert(row=listing.as_dict(), chat_id=chat_id,
                                            search_name=name, search_id=profile["id"]))
                for listing in known:
                    old = db.get_price(listing.id)
                    if (old and listing.price_eur
                            and (old - listing.price_eur) / old >= PRICE_DROP_THRESHOLD
                            and not db.already_notified(listing.id, name, "price_drop")):
                        alerts.append(Alert(row=listing.as_dict(), chat_id=chat_id,
                                            search_name=name, search_id=profile["id"],
                                            old_price=old, kind="price_drop"))
            pending.extend(alerts)

            for listing in fetched:
                db.upsert(listing)

            log.info("  %-12s fetched %3d | new %3d | matched %3d | to notify %3d",
                     scraper.source, len(fetched), len(new), len(matched), len(alerts))
        db.commit()
    return pending


def run(seed: bool = False, dry_run: bool = False, max_pages: int = 3, db: Db = None) -> str:
    owns_db = db is None
    db = db or Db()
    try:
        brands = load_brands()
        if resolve_missing(brands):
            save_brands(brands)
        import_yaml_searches(db)

        if not db.list_searches(active_only=True):
            log.warning("no active searches")
            return "Активных поисков нет. Создай через /add."

        pending = collect_alerts(db, brands, seed, max_pages)
        db.set_meta("last_run", now())
        export_webapp_decks(db)

        if seed:
            total = len(db.known_ids())
            log.info("seed mode: %d listings stored, no notifications sent", total)
            return f"Засеял базу: {total} объявлений, ничего не отправлял."

        notifier = Notifier(dry_run=dry_run, max_per_run=MAX_PER_RUN)
        sent, deferred = notifier.send_batch(pending)
        for alert in pending:
            if alert.sent:
                db.mark_notified(alert.listing_id, alert.search_name, alert.kind)
                db.drop_from_queue(alert.listing_id, alert.search_name, alert.kind)
            else:
                db.enqueue(alert.listing_id, alert.search_name, alert.kind,
                           alert.old_price, alert.chat_id, alert.search_id)
        db.commit()
        log.info("notified %d | queued for next run %d", len(sent), len(deferred))
        return f"Отправил {len(sent)}, ещё {len(deferred)} в очереди на следующий прогон."
    finally:
        if owns_db:
            db.commit()
            db.close()


WEBAPP_DATA = ROOT / "webapp" / "data"
DECK_FIELDS = ("id", "source", "brand", "model", "year", "mileage_km", "price_eur",
               "fuel", "gearbox", "url", "image_url", "country", "city", "damaged")


def export_webapp_decks(db: Db) -> int:
    """Write one feed file per chat for the swipe Mini App.

    The file name is an unguessable per-chat token, because on a public repo
    anything under webapp/ is world-readable.
    """
    WEBAPP_DATA.mkdir(parents=True, exist_ok=True)
    written = 0
    for row in db.conn.execute("SELECT DISTINCT chat_id FROM searches"):
        chat_id = row["chat_id"]
        deck = []
        for listing in db.swipe_deck(chat_id):
            item = {k: listing.get(k) for k in DECK_FIELDS}
            try:
                item["images"] = json.loads(listing.get("images") or "[]")
            except (TypeError, ValueError):
                item["images"] = []
            deck.append(item)
        path = WEBAPP_DATA / f"{db.chat_token(chat_id)}.json"
        path.write_text(json.dumps({"generated": now(), "listings": deck},
                                   ensure_ascii=False), encoding="utf-8")
        written += 1
        log.info("webapp deck for chat %s: %d listings", chat_id, len(deck))
    return written


def drain_bot_updates(db: Db) -> None:
    """Process bot commands queued since the last run (Actions has no long-poll)."""
    token = os.environ.get("TELEGRAM_BOT_TOKEN")
    if not token:
        return
    from core.telegram_app import build_application

    async def go():
        app = build_application(token, db, load_brands())
        async with app:
            offset = db.get_meta("tg_offset")
            updates = await app.bot.get_updates(
                offset=int(offset) if offset else None, timeout=0, limit=100)
            for update in updates:
                try:
                    await app.process_update(update)
                except Exception:
                    log.exception("failed to process update %s", update.update_id)
                db.set_meta("tg_offset", update.update_id + 1)
            if updates:
                log.info("processed %d bot updates", len(updates))

    try:
        asyncio.run(go())
    except Exception as exc:
        log.warning("could not drain bot updates: %s: %s", type(exc).__name__, exc)


def main():
    load_env()
    logging.basicConfig(level=logging.INFO, stream=sys.stdout,
                        format="%(asctime)s %(levelname)s %(message)s", datefmt="%H:%M:%S")
    ap = argparse.ArgumentParser()
    ap.add_argument("--seed", action="store_true",
                    help="store current listings without notifying (first run)")
    ap.add_argument("--dry-run", action="store_true", help="print messages instead of sending")
    ap.add_argument("--drain", action="store_true", help="process pending bot commands first")
    ap.add_argument("--max-pages", type=int, default=3)
    args = ap.parse_args()

    with Db() as db:
        if args.drain:
            drain_bot_updates(db)
        run(seed=args.seed, dry_run=args.dry_run, max_pages=args.max_pages, db=db)
    return 0


if __name__ == "__main__":
    sys.exit(main())
