"""willhaben.at - the dominant Austrian marketplace, dealers and private sellers.

Like autoscout24 it is a Next.js page, so the whole result set sits in
__NEXT_DATA__ as structured attributes - no HTML parsing beyond finding the tag.
"""

import json
import logging

from bs4 import BeautifulSoup

from .base import MAX_IMAGES, BaseScraper, BotWallError, Listing

log = logging.getLogger(__name__)

BASE = "https://www.willhaben.at/iad/gebrauchtwagen/auto/gebrauchtwagenboerse"
PER_PAGE = 30
FUEL_PARAM = {"diesel": "Diesel", "petrol": "Benzin", "hybrid": "Hybrid", "electric": "Elektro"}


class WillhabenScraper(BaseScraper):
    source = "willhaben"
    countries_served = {"A", "AT"}

    def _headers(self):
        return {**super()._headers(), "Accept-Language": "de-AT,de;q=0.9,en;q=0.8"}

    def _params(self, profile: dict, page: int) -> dict:
        p = {
            "keyword": " ".join(filter(None, [profile["brand"], profile.get("model")])),
            "rows": PER_PAGE,
            "page": page,
            "sort": 1,                    # newest first
        }
        if profile.get("price_max"):
            p["PRICE_TO"] = profile["price_max"]
        if profile.get("price_min"):
            p["PRICE_FROM"] = profile["price_min"]
        if profile.get("year_from"):
            p["YEAR_MODEL_FROM"] = profile["year_from"]
        if profile.get("mileage_max"):
            p["MILEAGE_TO"] = profile["mileage_max"]
        return p

    @staticmethod
    def _attrs(ad: dict) -> dict:
        pairs = ((ad.get("attributes") or {}).get("attribute")) or []
        return {a["name"]: a["values"][0] for a in pairs if a.get("values")}

    def _to_listing(self, ad: dict) -> Listing:
        at = self._attrs(ad)
        images = []
        for raw_image in (at.get("ALL_IMAGE_URLS") or "").split(";"):
            raw_image = raw_image.strip()
            if not raw_image:
                continue
            images.append(raw_image if raw_image.startswith("http")
                          else "https://cache.willhaben.at/mmo/" + raw_image.lstrip("/"))
            if len(images) >= MAX_IMAGES:
                break
        seo = at.get("SEO_URL") or ""
        is_private = str(at.get("ISPRIVATE", "0")) == "1"
        condition = " ".join(str(at.get(k) or "") for k in
                             ("CONDITION_RESOLVED", "CONDITION_REPORT")).lower()
        from .base import looks_damaged_de
        damaged = (any(w in condition for w in ("beschädig", "havar"))
                   or looks_damaged_de(f"{ad.get('description') or ''} {at.get('HEADING') or ''}"))
        return Listing(
            id=f"{self.source}:{ad.get('id')}",
            source=self.source,
            brand=at.get("CAR_MODEL/MAKE") or "",
            model=at.get("CAR_MODEL/MODEL") or "",
            year=self.year_from_registration(at.get("YEAR_MODEL")),
            mileage_km=self.to_int(at.get("MILEAGE")),
            price_eur=self.to_int(at.get("PRICE") or at.get("PRICE/AMOUNT")),
            fuel=self.norm_fuel(at.get("ENGINE/FUEL_RESOLVED") or at.get("ENGINE/FUEL")),
            gearbox=self.norm_gearbox(at.get("TRANSMISSION_RESOLVED")),
            url="https://www.willhaben.at/iad/" + seo.lstrip("/") if seo else
                f"https://www.willhaben.at/iad/object?adId={ad.get('id')}",
            image_url=images[0] if images else None,
            images=images,
            title=ad.get("description") or at.get("HEADING"),
            country="AT",
            city=at.get("LOCATION") or at.get("DISTRICT"),
            damaged=damaged,
            dealer_id=None if is_private else at.get("ORGID"),
            dealer_name=at.get("HEADING") if is_private else at.get("ORGNAME"),
        )

    def search(self, profile: dict, max_pages: int = 3, seen_ids=frozenset()) -> list:
        out = []
        for page in range(1, max_pages + 1):
            html = self.get(BASE, params=self._params(profile, page)).text
            tag = BeautifulSoup(html, "html.parser").find("script", id="__NEXT_DATA__")
            if not tag or not tag.string:
                raise BotWallError("willhaben: __NEXT_DATA__ missing (layout change or bot-wall)")
            result = (json.loads(tag.string).get("props", {})
                      .get("pageProps", {}).get("searchResult") or {})
            ads = (result.get("advertSummaryList") or {}).get("advertSummary") or []
            if not ads:
                break
            hit_known = False
            for ad in ads:
                listing = self._to_listing(ad)
                out.append(listing)
                hit_known |= listing.id in seen_ids
            if hit_known or len(ads) < PER_PAGE:
                break
        return out
