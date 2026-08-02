"""mobile.de scraper.

suchen.mobile.de/fahrzeuge/search.html answers 403 to anything that is not a real
browser, so this uses the app-facing JSON service instead - same data, no HTML
parsing, and it only needs the X-Mobile-Client header.

    GET https://www.mobile.de/svc/s/?ms={makeId};{modelId};;&sb=ct&od=down&psz=N

Note: that service has no page parameter - `psz` (page size, max 200) is the only
way to reach deeper results, so max_pages is turned into one larger request.
"""

import logging

from .base import BaseScraper, BotWallError, Listing, COUNTRY_MAP

log = logging.getLogger(__name__)

SVC = "https://www.mobile.de/svc/s/"
CLIENT_HEADER = {"X-Mobile-Client": "de.mobile.android.app", "Accept": "application/json"}
PAGE_SIZE = 50
MAX_PAGE_SIZE = 200

FUEL_PARAM = {"diesel": "DIESEL", "petrol": "PETROL",
              "hybrid": "HYBRID", "electric": "ELECTRICITY"}
GEARBOX_PARAM = {"automatic": "AUTOMATIC_GEAR", "manual": "MANUAL_GEAR"}


class MobileDeScraper(BaseScraper):
    source = "mobilede"

    def _make_id(self, brand_name: str):
        brand = self.brands.get(brand_name) or {}
        return brand.get("mobilede_make_id")

    def _model_id(self, make_id: int, model_name: str):
        """Resolve a model name to mobile.de's model id (exact, then prefix match)."""
        try:
            r = self.get(f"https://www.mobile.de/svc/r/models/{make_id}", headers=CLIENT_HEADER)
            models = r.json().get("models") or []
        except Exception as exc:                       # reference lookup is best-effort
            log.warning("mobilede: model lookup failed for %s: %s", model_name, exc)
            return None
        wanted = str(model_name).strip().lower()
        for m in models:
            if str(m.get("n", "")).strip().lower() == wanted:
                return m.get("i")
        for m in models:
            if str(m.get("n", "")).strip().lower().startswith(wanted):
                return m.get("i")
        log.warning("mobilede: model %r not found for makeId %s", model_name, make_id)
        return None

    def _params(self, profile: dict, page_size: int) -> list:
        make_id = self._make_id(profile["brand"])
        if not make_id:
            raise ValueError(f"no mobile.de makeId for brand {profile['brand']!r}")
        model_id = self._model_id(make_id, profile["model"]) if profile.get("model") else None

        params = [
            ("ms", f"{make_id};{model_id or ''};;"),
            ("sb", "ct"),          # sort by creation time
            ("od", "down"),        # newest first
            ("psz", page_size),
        ]
        if profile.get("price_max") or profile.get("price_min"):
            params.append(("p", f"{profile.get('price_min') or ''}:{profile.get('price_max') or ''}"))
        if profile.get("mileage_max"):
            params.append(("ml", f":{profile['mileage_max']}"))
        if profile.get("year_from"):
            params.append(("fr", f"{profile['year_from']}:"))
        if profile.get("fuel") in FUEL_PARAM:
            params.append(("ft", FUEL_PARAM[profile["fuel"]]))
        if profile.get("gearbox") in GEARBOX_PARAM:
            params.append(("tr", GEARBOX_PARAM[profile["gearbox"]]))
        for code in profile.get("countries") or ["D"]:
            iso = COUNTRY_MAP.get(str(code).upper(), str(code).upper())
            params.append(("cn", iso))
        return params

    @staticmethod
    def _image_url(raw: dict):
        images = raw.get("images") or []
        if not images:
            return None
        # uri looks like "m.mobile.de/yams-proxy/img.classistatic.de/api/v1/mo-prod/images/42/<uuid>"
        uri = (images[0].get("uri") or "").lstrip("/")
        if not uri:
            return None
        _, _, path = uri.partition("yams-proxy/")
        return f"https://{path or uri}?rule=mo-640.jpg"

    def _to_listing(self, raw: dict) -> Listing:
        attr = raw.get("attr") or {}
        price = ((raw.get("price") or {}).get("grs") or {}).get("amount")
        # every private seller shares one bucket sellerId (7723851 today), so only
        # real dealers get an id the user can block
        contact = raw.get("contact") or {}
        dealer_id = (str(raw["sellerId"])
                     if raw.get("sellerId") and contact.get("enumType") == "DEALER" else None)
        return Listing(
            id=f"{self.source}:{raw.get('id')}",
            source=self.source,
            brand=(raw.get("make") or {}).get("localized") or "",
            model=(raw.get("model") or {}).get("localized") or "",
            year=self.year_from_registration(attr.get("fr")),
            mileage_km=self.to_int(attr.get("ml")),
            price_eur=self.to_int(price),
            fuel=self.norm_fuel(attr.get("ft")),
            gearbox=self.norm_gearbox(attr.get("tr")),
            url=raw.get("url") or f"https://suchen.mobile.de/auto-inserat/{raw.get('id')}.html",
            image_url=self._image_url(raw),
            dealer_id=dealer_id,
            dealer_name=contact.get("name") or raw.get("st"),
        )

    def search(self, profile: dict, max_pages: int = 3, seen_ids=frozenset()) -> list:
        page_size = min(PAGE_SIZE * max_pages, MAX_PAGE_SIZE)
        r = self.get(SVC, params=self._params(profile, page_size), headers=CLIENT_HEADER)
        try:
            items = r.json().get("items") or []
        except ValueError as exc:
            raise BotWallError(f"mobilede: non-JSON answer ({exc})")

        # one request already holds the whole window, so return known ads too:
        # they cost nothing extra and keep price-drop detection working
        return [self._to_listing(raw) for raw in items]
