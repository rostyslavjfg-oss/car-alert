#!/usr/bin/env python3
"""Orchestrator: scrape -> dedupe -> match -> notify.

    python main.py --seed     first run: fill the db, send nothing
    python main.py            normal run
    python main.py --dry-run  scrape and match, print instead of sending
"""

import argparse
import logging
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

import yaml

from core.db import Db
from core.matcher import filter_matching
from core.notifier import Notifier, MAX_PER_RUN
from fetch_mobile_makes import load_brands, resolve_missing, save_brands
from scrapers.autoscout24 import AutoScout24Scraper
from scrapers.base import BotWallError
from scrapers.mobilede import MobileDeScraper

ROOT = Path(__file__).resolve().parent
SEARCHES_PATH = ROOT / "config" / "searches.yml"
PRICE_DROP_THRESHOLD = 0.05          # notify on >= 5% drop

log = logging.getLogger("car-alert")


@dataclass
class Alert:
    row: dict                      # listing as a plain dict (must stay one object: tracked by identity)
    old_price: Optional[int]
    search_name: str
    kind: str                      # 'new' | 'price_drop'


def load_searches(path=SEARCHES_PATH) -> list:
    with open(path, encoding="utf-8") as fh:
        return (yaml.safe_load(fh) or {}).get("searches") or []


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


def run(seed: bool = False, dry_run: bool = False, max_pages: int = 3) -> int:
    searches = load_searches()
    if not searches:
        log.error("config/searches.yml has no searches")
        return 1

    brands = load_brands()
    if resolve_missing(brands):
        save_brands(brands)

    scrapers = [AutoScout24Scraper(brands), MobileDeScraper(brands)]
    pending = []          # [Alert(row, old_price, search_name, kind)]

    with Db() as db:
        # overflow queued by the previous run goes out first
        for row in db.dequeue_all():
            pending.append(Alert(row, row.get("old_price"), row["search_name"], row["kind"]))

        for profile in searches:
            name = profile["name"]
            log.info("search %s", name)
            for scraper in scrapers:
                seen_ids = db.known_ids(scraper.source)
                fetched = scrape_profile(scraper, profile, max_pages, seen_ids)
                new = [l for l in fetched if l.id not in seen_ids]
                matched = filter_matching(new, profile)
                known_matched = filter_matching(
                    [l for l in fetched if l.id in seen_ids], profile)

                alerts = []
                if not seed:
                    for listing in matched:
                        if not db.already_notified(listing.id, name, "new"):
                            alerts.append(Alert(listing.as_dict(), None, name, "new"))
                    for listing in known_matched:
                        old = db.get_price(listing.id)
                        if (old and listing.price_eur
                                and (old - listing.price_eur) / old >= PRICE_DROP_THRESHOLD
                                and not db.already_notified(listing.id, name, "price_drop")):
                            alerts.append(Alert(listing.as_dict(), old, name, "price_drop"))
                pending.extend(alerts)

                for listing in fetched:
                    db.upsert(listing)

                log.info("  %-12s fetched %3d | new %3d | matched %3d | to notify %3d",
                         scraper.source, len(fetched), len(new), len(matched), len(alerts))
            db.commit()

        if seed:
            log.info("seed mode: %d listings stored, no notifications sent",
                     len(db.known_ids()))
            return 0

        notifier = Notifier(dry_run=dry_run, max_per_run=MAX_PER_RUN)
        sent, deferred = notifier.send_batch([(a.row, a.old_price) for a in pending])
        sent_ids = {id(row) for row, _ in sent}

        for alert in pending:
            if id(alert.row) in sent_ids:
                db.mark_notified(alert.row["id"], alert.search_name, alert.kind)
                db.drop_from_queue(alert.row["id"], alert.search_name, alert.kind)
            else:
                db.enqueue(alert.row["id"], alert.search_name, alert.kind, alert.old_price)
        db.commit()

        log.info("notified %d | queued for next run %d", len(sent), len(deferred))
    return 0


def main():
    logging.basicConfig(level=logging.INFO, stream=sys.stdout,
                        format="%(asctime)s %(levelname)s %(message)s",
                        datefmt="%H:%M:%S")
    ap = argparse.ArgumentParser()
    ap.add_argument("--seed", action="store_true",
                    help="store current listings without notifying (first run)")
    ap.add_argument("--dry-run", action="store_true", help="print messages instead of sending")
    ap.add_argument("--max-pages", type=int, default=3)
    args = ap.parse_args()
    sys.exit(run(seed=args.seed, dry_run=args.dry_run, max_pages=args.max_pages))


if __name__ == "__main__":
    main()
