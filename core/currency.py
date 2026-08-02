"""EUR conversion for the sources that price in their local currency.

sauto.cz quotes CZK and otomoto.pl quotes PLN, but every filter and every alert
speaks EUR, so a wrong rate would silently break price_max on those two.
Rates come from the ECB daily reference feed and are cached in the db for a day;
the constants below are only a floor so a feed outage cannot crash a run.
"""

import logging
import re
from datetime import date

import requests

log = logging.getLogger(__name__)

ECB_URL = "https://www.ecb.europa.eu/stats/eurofxref/eurofxref-daily.xml"
# rough values from 2026-08; refreshed on the first run of each day
FALLBACK = {"CZK": 24.2, "PLN": 4.31, "HUF": 390.0, "EUR": 1.0}

_cache = {}


def _fetch_rates(timeout: int = 15) -> dict:
    r = requests.get(ECB_URL, timeout=timeout)
    r.raise_for_status()
    rates = {m.group(1): float(m.group(2)) for m in
             re.finditer(r"currency='([A-Z]{3})'\s+rate='([\d.]+)'", r.text)}
    if not rates:
        raise ValueError("no rates in the ECB feed")
    rates["EUR"] = 1.0
    return rates


def rates(db=None) -> dict:
    """{currency: units per 1 EUR}. Refreshed once a day, cached in the db."""
    today = date.today().isoformat()
    if _cache.get("day") == today:
        return _cache["rates"]

    stored_day = db.get_meta("fx_day") if db else None
    if db and stored_day == today:
        try:
            values = dict(pair.split(":") for pair in (db.get_meta("fx") or "").split(","))
            parsed = {k: float(v) for k, v in values.items()}
            if parsed:
                _cache.update(day=today, rates={**FALLBACK, **parsed})
                return _cache["rates"]
        except (ValueError, AttributeError):
            pass

    try:
        fetched = _fetch_rates()
    except Exception as exc:
        log.warning("ECB rates unavailable (%s), using the built-in fallback", exc)
        fetched = dict(FALLBACK)
    else:
        if db:
            keep = {c: fetched[c] for c in FALLBACK if c in fetched}
            db.set_meta("fx", ",".join(f"{k}:{v}" for k, v in keep.items()))
            db.set_meta("fx_day", today)

    merged = {**FALLBACK, **fetched}
    _cache.update(day=today, rates=merged)
    return merged


def to_eur(amount, currency: str, db=None):
    if amount is None:
        return None
    rate = rates(db).get(currency.upper())
    if not rate:
        log.warning("no rate for %s, leaving the amount as is", currency)
        return int(amount)
    return int(round(float(amount) / rate))


def from_eur(amount, currency: str, db=None):
    if amount is None:
        return None
    rate = rates(db).get(currency.upper())
    return int(round(float(amount) * rate)) if rate else int(amount)
