#!/usr/bin/env python3
"""Fill in missing mobile.de makeIds in config/brands.json.

mobile.de publishes its make reference list on the app service; the only thing it
wants is the X-Mobile-Client header. Run standalone to refresh the whole file:

    python fetch_mobile_makes.py            # fill missing ids
    python fetch_mobile_makes.py --force    # re-resolve every brand
"""

import argparse
import json
import logging
import re
import unicodedata
from pathlib import Path

import requests

log = logging.getLogger(__name__)

BRANDS_PATH = Path(__file__).resolve().parent / "config" / "brands.json"
MAKES_URL = "https://www.mobile.de/svc/r/makes/Car"
HEADERS = {
    "X-Mobile-Client": "de.mobile.android.app",
    "Accept": "application/json",
    "Accept-Language": "de-DE,de;q=0.9",
    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36",
}


def normalize(name: str) -> str:
    folded = unicodedata.normalize("NFKD", name).encode("ascii", "ignore").decode()
    return re.sub(r"[^a-z0-9]", "", folded.lower())


def fetch_makes(timeout: int = 20) -> dict:
    """{normalized_name: makeId}"""
    r = requests.get(MAKES_URL, headers=HEADERS, timeout=timeout)
    r.raise_for_status()
    return {normalize(m["n"]): m["i"] for m in r.json().get("makes", [])}


def resolve_missing(brands: dict, force: bool = False) -> int:
    """Mutates `brands` in place, returns how many ids were filled."""
    targets = [b for b, cfg in brands.items() if force or not cfg.get("mobilede_make_id")]
    if not targets:
        return 0
    try:
        makes = fetch_makes()
    except requests.RequestException as exc:
        log.warning("mobile.de make lookup failed: %s", exc)
        return 0
    filled = 0
    for brand in targets:
        make_id = makes.get(normalize(brand))
        if make_id and brands[brand].get("mobilede_make_id") != make_id:
            brands[brand]["mobilede_make_id"] = make_id
            filled += 1
    return filled


def load_brands(path=BRANDS_PATH) -> dict:
    with open(path, encoding="utf-8") as fh:
        return json.load(fh)


def save_brands(brands: dict, path=BRANDS_PATH) -> None:
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(brands, fh, indent=2, ensure_ascii=False, sort_keys=True)
        fh.write("\n")


def main():
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    ap = argparse.ArgumentParser()
    ap.add_argument("--force", action="store_true", help="re-resolve every brand")
    args = ap.parse_args()

    brands = load_brands()
    filled = resolve_missing(brands, force=args.force)
    if filled:
        save_brands(brands)
    total = sum(1 for c in brands.values() if c.get("mobilede_make_id"))
    log.info("brands: %d | with mobile.de id: %d | updated this run: %d",
             len(brands), total, filled)


if __name__ == "__main__":
    main()
