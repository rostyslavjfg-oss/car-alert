"""autoscout24.com scraper.

The result page ships every listing as JSON inside <script id="__NEXT_DATA__">,
so there is nothing to parse out of the markup itself - BeautifulSoup only
locates the tag.
"""

import json
import logging

from bs4 import BeautifulSoup

from .base import MAX_IMAGES, BaseScraper, BotWallError, Listing, looks_damaged_de

log = logging.getLogger(__name__)

BASE = "https://www.autoscout24.com/lst"
FUEL_PARAM = {"diesel": "D", "petrol": "B", "electric": "E", "hybrid": "2"}
# a code it does not know (e.g. SK) makes the whole search return zero results
SUPPORTED_COUNTRIES = {"D", "A", "B", "NL", "L", "I", "E", "F"}
GEARBOX_PARAM = {"automatic": "A", "manual": "M"}


class AutoScout24Scraper(BaseScraper):
    source = "autoscout24"
    countries_served = SUPPORTED_COUNTRIES

    def _url(self, profile: dict) -> str:
        brand = self.brands.get(profile["brand"])
        if not brand or not brand.get("autoscout24_slug"):
            raise ValueError(f"no autoscout24 slug for brand {profile['brand']!r}")
        path = brand["autoscout24_slug"]
        if profile.get("model"):
            path += "/" + str(profile["model"]).lower().replace(" ", "-")
        return f"{BASE}/{path}"

    def _params(self, profile: dict, page: int) -> dict:
        p = {
            "atype": "C",
            "cy": ",".join(sorted(SUPPORTED_COUNTRIES.intersection(
                {str(c).upper() for c in (profile.get("countries") or ["D"])}) or {"D"})),
            "sort": "age",       # newest offers first
            "desc": "1",
            "page": page,
            "size": 20,
        }
        if profile.get("year_from"):
            p["fregfrom"] = profile["year_from"]
        if profile.get("mileage_max"):
            p["kmto"] = profile["mileage_max"]
        if profile.get("price_max"):
            p["priceto"] = profile["price_max"]
        if profile.get("price_min"):
            p["pricefrom"] = profile["price_min"]
        if profile.get("fuel") in FUEL_PARAM:
            p["fuel"] = FUEL_PARAM[profile["fuel"]]
        if profile.get("gearbox") in GEARBOX_PARAM:
            p["gear"] = GEARBOX_PARAM[profile["gearbox"]]
        return p

    @staticmethod
    def _next_data(html: str) -> dict:
        tag = BeautifulSoup(html, "html.parser").find("script", id="__NEXT_DATA__")
        if not tag or not tag.string:
            raise BotWallError("autoscout24: __NEXT_DATA__ missing (layout change or bot-wall)")
        return json.loads(tag.string)

    def _to_listing(self, raw: dict) -> Listing:
        v = raw.get("vehicle") or {}
        tracking = raw.get("tracking") or {}
        seller = raw.get("seller") or {}
        location = raw.get("location") or {}
        # thumbnails come as .../250x188.webp - ask for something Telegram-worthy
        images = [i.replace("/250x188.webp", "/640x480.webp")
                  for i in (raw.get("images") or [])][:MAX_IMAGES]
        listing_id = str(raw.get("crossReferenceId") or raw.get("id"))
        return Listing(
            id=f"{self.source}:{listing_id}",
            source=self.source,
            brand=v.get("make") or "",
            model=v.get("model") or v.get("modelGroup") or "",
            year=self.year_from_registration(tracking.get("firstRegistration")),
            mileage_km=self.to_int(tracking.get("mileage") or v.get("mileageInKm")),
            price_eur=self.to_int((raw.get("price") or {}).get("priceRaw")),
            fuel=self.norm_fuel(v.get("fuel")),
            gearbox=self.norm_gearbox(v.get("transmission")),
            url="https://www.autoscout24.com" + (raw.get("url") or ""),
            image_url=images[0] if images else None,
            images=images,
            title=v.get("modelVersionInput") or raw.get("versionTitle"),
            country=(location.get("countryCode") or "").upper() or None,
            city=location.get("city"),
            damaged=bool(v.get("isCurrentlyDamaged"))
            or looks_damaged_de(f"{v.get('modelVersionInput') or ''} {raw.get('url') or ''}"),
            dealer_id=str(seller.get("id")) if seller.get("id") else None,
            dealer_name=seller.get("companyName") or seller.get("contactName"),
        )

    def search(self, profile: dict, max_pages: int = 3, seen_ids=frozenset()) -> list:
        """Returns every listing fetched (known ones included, so prices stay fresh)."""
        out, url = [], self._url(profile)
        for page in range(1, max_pages + 1):
            data = self._next_data(self.get(url, params=self._params(profile, page)).text)
            raw_listings = (data.get("props", {}).get("pageProps", {}) or {}).get("listings") or []
            if not raw_listings:
                break
            hit_known = False
            for raw in raw_listings:
                listing = self._to_listing(raw)
                out.append(listing)
                hit_known |= listing.id in seen_ids
            # sorted newest-first: once a known ad shows up, everything below is older
            if hit_known:
                break
        return out
