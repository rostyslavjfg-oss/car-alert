"""sauto.cz - the largest Czech car marketplace.

It exposes a public JSON API (`/api/v1/items/search`), so nothing is parsed out
of HTML. Two things it does not do: filter by manufacturer id (only free-text
`phrase` narrows the make) and sort by insertion date - results come back in a
mixed order, so they are sorted locally on `create_date`.

Prices are CZK and get converted to EUR, otherwise price_max would be nonsense.
"""

import logging
import time

from core.currency import from_eur, to_eur

from .base import MAX_IMAGES, BaseScraper, BotWallError, Listing

log = logging.getLogger(__name__)

API = "https://www.sauto.cz/api/v1/items/search"
DETAIL = "https://www.sauto.cz/api/v1/items/{}"
CATEGORY_CARS = 838
PER_PAGE = 40
# the list endpoint leaves manufacturing_date empty on most ads and has no
# server-side year filter, so the build year is topped up from the detail
# endpoint - cheap JSON, but capped so a first run cannot crawl forever
MAX_YEAR_LOOKUPS = 40
YEAR_LOOKUP_DELAY = 0.4

FUEL_PARAM = {"petrol": 1, "diesel": 2, "electric": 4, "hybrid": 5}
FUEL_NAME = {1: "petrol", 2: "diesel", 3: "petrol", 4: "electric", 5: "hybrid", 6: "petrol"}
GEARBOX_PARAM = {"manual": 1, "automatic": 3}
GEARBOX_NAME = {1: "manual", 3: "automatic"}
DAMAGE_WORDS = ("havar", "po nehodě", "po nehode", "poškozen", "nepojízdn", "na díly",
                "bourané", "po bouračce", "vadný motor", "na náhradní díly")


class SautoScraper(BaseScraper):
    source = "sauto"
    countries_served = {"CZ"}

    def __init__(self, brands: dict, timeout: int = 25, db=None):
        super().__init__(brands, timeout)
        self.db = db                       # only used to cache the FX rate

    def _headers(self):
        return {**super()._headers(), "Accept": "application/json",
                "Accept-Language": "cs,sk;q=0.9,en;q=0.8"}

    def _params(self, profile: dict, offset: int) -> dict:
        p = {
            "category_id": CATEGORY_CARS,
            "limit": PER_PAGE,
            "offset": offset,
            "phrase": " ".join(filter(None, [profile["brand"], profile.get("model")])),
        }
        if profile.get("price_max"):
            p["price_to"] = from_eur(profile["price_max"], "CZK", self.db)
        if profile.get("price_min"):
            p["price_from"] = from_eur(profile["price_min"], "CZK", self.db)
        if profile.get("mileage_max"):
            p["tachometer_to"] = profile["mileage_max"]
        if profile.get("fuel") in FUEL_PARAM:
            p["fuel_cb"] = FUEL_PARAM[profile["fuel"]]
        if profile.get("gearbox") in GEARBOX_PARAM:
            p["gearbox_cb"] = GEARBOX_PARAM[profile["gearbox"]]
        return p

    @staticmethod
    def _year(item: dict):
        """Only the real build date. Falling back to create_date would stamp
        every listing 2026 and sail past any year_from filter."""
        raw = item.get("manufacturing_date")
        return int(str(raw)[:4]) if raw and str(raw)[:4].isdigit() else None

    def _to_listing(self, item: dict) -> Listing:
        images = []
        for image in (item.get("images") or [])[:MAX_IMAGES]:
            url = (image.get("url") or "").strip()
            if url:
                images.append("https:" + url if url.startswith("//") else url)
        name = item.get("name") or ""
        premise = item.get("premise") or {}
        locality = item.get("locality") or {}
        text = f"{name} {item.get('additional_model_name') or ''}".lower()
        return Listing(
            id=f"{self.source}:{item.get('id')}",
            source=self.source,
            brand=(item.get("manufacturer_cb") or {}).get("name") or "",
            model=(item.get("model_cb") or {}).get("name") or "",
            year=self._year(item),
            mileage_km=self.to_int(item.get("tachometer")),
            price_eur=to_eur(item.get("price"), "CZK", self.db),
            fuel=FUEL_NAME.get((item.get("fuel_cb") or {}).get("value")),
            gearbox=GEARBOX_NAME.get((item.get("gearbox_cb") or {}).get("value")),
            url="https://www.sauto.cz/osobni/detail/{}/{}/{}".format(
                (item.get("manufacturer_cb") or {}).get("seo_name") or "x",
                (item.get("model_cb") or {}).get("seo_name") or "x", item.get("id")),
            image_url=images[0] if images else None,
            images=images,
            title=name,
            country="CZ",
            city=locality.get("district") or None,
            damaged=any(w in text for w in DAMAGE_WORDS),
            dealer_id=str(premise["id"]) if premise.get("id") else None,
            dealer_name=premise.get("name"),
        )

    def _fill_missing_years(self, listings: list) -> None:
        """Ask the detail endpoint for the year the list view leaves empty.
        The same answer carries the VIN, so it is picked up for free."""
        missing = [l for l in listings if l.year is None or l.vin is None]
        if not missing:
            return
        if len(missing) > MAX_YEAR_LOOKUPS:
            log.info("sauto: %d ads without a year, looking up the first %d",
                     len(missing), MAX_YEAR_LOOKUPS)
            missing = missing[:MAX_YEAR_LOOKUPS]
        for listing in missing:
            item_id = listing.id.split(":", 1)[1]
            try:
                time.sleep(YEAR_LOOKUP_DELAY)
                r = self.session.get(DETAIL.format(item_id), timeout=self.timeout,
                                     headers=self._headers())
                r.raise_for_status()
                detail = r.json().get("result") or {}
            except Exception as exc:
                log.debug("sauto: year lookup failed for %s: %s", item_id, exc)
                continue
            raw = detail.get("manufacturing_date") or detail.get("in_operation_date")
            if listing.year is None and raw and str(raw)[:4].isdigit():
                listing.year = int(str(raw)[:4])
            if not listing.vin and detail.get("vin"):
                listing.vin = str(detail["vin"]).strip().upper()

    def search(self, profile: dict, max_pages: int = 3, seen_ids=frozenset()) -> list:
        out, by_id = [], set()
        for page in range(max_pages):
            r = self.get(API, params=self._params(profile, page * PER_PAGE))
            try:
                results = r.json().get("results") or []
            except ValueError as exc:
                raise BotWallError(f"sauto: non-JSON answer ({exc})")
            if not results:
                break
            for item in results:
                listing = self._to_listing(item)
                if listing.id not in by_id:
                    by_id.add(listing.id)
                    out.append((item.get("create_date") or "", listing))
            if len(results) < PER_PAGE:
                break
        # the API has no "newest first" sort, so ordering happens here
        out.sort(key=lambda pair: pair[0], reverse=True)
        listings = [listing for _, listing in out]
        self._fill_missing_years(listings)
        return listings
