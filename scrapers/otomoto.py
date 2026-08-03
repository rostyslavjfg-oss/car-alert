"""otomoto.pl - the biggest Polish car marketplace (OLX group).

Listings are server-rendered as <article data-id> cards with the specs exposed
through `data-parameter` attributes, so no GraphQL handshake is needed. Class
names are hashed and change on every deploy, which is why nothing here selects
on them - only on data attributes and tag structure.

Prices are PLN and get converted to EUR.
"""

import logging
import re

from bs4 import BeautifulSoup

from core.currency import to_eur

from .base import BaseScraper, BotWallError, Listing, find_vin

log = logging.getLogger(__name__)

BASE = "https://www.otomoto.pl/osobowe"
PER_PAGE = 32

FUEL_WORDS = {"diesel": "diesel", "benzyna": "petrol", "hybryda": "hybrid",
              "elektryczny": "electric", "elektryczne": "electric"}
GEARBOX_WORDS = {"automatyczna": "automatic", "manualna": "manual"}
FUEL_PARAM = {"diesel": "diesel", "petrol": "petrol", "hybrid": "hybrid",
              "electric": "electric"}
# "Bezwypadkowy" means accident-FREE and contains "wypadkow" - the negation has
# to be cut first or every clean car would be flagged
NEGATED_RE = re.compile(r"\bbez\s*wypadkow\w*|\bbezwypadkow\w*|\bnieuszkodzon\w*", re.I)
DAMAGE_RE = re.compile(r"\buszkodzon\w*|\bpowypadkow\w*|\bpo wypadku\b"
                       r"|\bna cz[eę][sś]ci\b|\bdo remontu silnika\b", re.I)


def looks_damaged(text: str) -> bool:
    return bool(DAMAGE_RE.search(NEGATED_RE.sub(" ", text or "")))


def model_slug(brand: str, model: str) -> tuple:
    """-> (slug, exact). otomoto's taxonomy stops at the family for some makes:
    a BMW 320 lives under "seria-3", a Mercedes C 200 under "klasa-c".
    `exact` is False when the slug is wider than what was asked for."""
    raw = str(model).strip()
    plain = re.sub(r"[^a-z0-9]+", "-", raw.lower()).strip("-")
    brand_low = (brand or "").lower()
    if brand_low == "bmw":
        digits = re.match(r"^(\d)\d{2}$", raw.replace(" ", ""))
        if digits:
            return f"seria-{digits.group(1)}", False
    if brand_low.startswith("mercedes"):
        letter = re.match(r"^([a-z])[\s-]*\d*$", raw.lower())
        if letter:
            return f"klasa-{letter.group(1)}", False
    return plain, True


class OtomotoScraper(BaseScraper):
    source = "otomoto"
    countries_served = {"PL"}

    def __init__(self, brands: dict, timeout: int = 25, db=None):
        super().__init__(brands, timeout)
        self.db = db                       # only used to cache the FX rate

    def _headers(self):
        return {**super()._headers(), "Accept-Language": "pl,en;q=0.8"}

    def _url(self, profile: dict) -> str:
        brand = self.brands.get(profile["brand"]) or {}
        slug = brand.get("autoscout24_slug")     # same shape otomoto uses
        if not slug:
            raise ValueError(f"no slug for brand {profile['brand']!r}")
        path = f"{BASE}/{slug}"
        if profile.get("model"):
            model_path, _ = model_slug(profile["brand"], profile["model"])
            if model_path:
                path += "/" + model_path
        return path

    def _params(self, profile: dict, page: int) -> dict:
        p = {"search[order]": "created_at_first:desc"}
        if page > 1:
            p["page"] = page
        # price stays local: otomoto's own bound is PLN, and pushing a converted
        # value would drift with the daily rate
        if profile.get("year_from"):
            p["search[filter_float_year:from]"] = profile["year_from"]
        if profile.get("mileage_max"):
            p["search[filter_float_mileage:to]"] = profile["mileage_max"]
        if profile.get("fuel") in FUEL_PARAM:
            p["search[filter_enum_fuel_type]"] = FUEL_PARAM[profile["fuel"]]
        return p

    def _to_listing(self, card, profile: dict, relaxed: bool = False) -> Listing:
        link = card.select_one("h2 a[href]") or card.find("a", href=True)
        if not link:
            return None
        title = link.get_text(" ", strip=True)
        params = {p.get("data-parameter"): p.get_text(" ", strip=True)
                  for p in card.select("[data-parameter]")}

        price = None
        for heading in card.find_all(["h3", "h4"]):
            digits = re.sub(r"[^\d]", "", heading.get_text(strip=True))
            if digits:
                price = int(digits)
                break
        currency = "PLN"
        if "EUR" in card.get_text():
            currency = "EUR"

        image = card.find("img")
        image_url = image.get("src") if image else None
        if image_url:                       # thumbnails come as ;s=320x240
            image_url = re.sub(r";s=\d+x\d+", ";s=1080x720", image_url)

        subtitle = " ".join(p.get_text(" ", strip=True) for p in card.find_all("p"))
        # location reads "Kacice (Mazowieckie)"; take the token right before the
        # bracket, not everything that precedes it
        location = re.search(r"([\w\-]+)\s*\([^)]*\)", subtitle)
        city = location.group(1) if location else None

        # /osobowe/bmw/320 quietly falls back to all BMWs when otomoto does not
        # know that model, so the real model has to come from the headline -
        # otherwise an X4 would match a "320" search
        model = re.sub(r"^\s*" + re.escape(profile["brand"]) + r"\s*", "", title,
                       flags=re.I).strip()

        return Listing(
            id=f"{self.source}:{card.get('data-id')}",
            source=self.source,
            brand=profile["brand"],
            model=model,
            year=self.year_from_registration(params.get("year")),
            mileage_km=self.to_int(params.get("mileage")),
            price_eur=to_eur(price, currency, self.db),
            fuel=self._word(params.get("fuel_type"), FUEL_WORDS),
            gearbox=self._word(params.get("gearbox"), GEARBOX_WORDS),
            url=link["href"].split("?")[0],
            image_url=image_url,
            images=[image_url] if image_url else [],   # the card carries one photo
            title=f"{title} {subtitle}".strip(),
            country="PL",
            city=city,
            damaged=looks_damaged(f"{title} {subtitle}"),
            vin=find_vin(f"{title} {subtitle}"),
            model_relaxed=relaxed,
            dealer_id=None,                 # the card exposes no stable seller id
            dealer_name=None,
        )

    def search(self, profile: dict, max_pages: int = 3, seen_ids=frozenset()) -> list:
        out, url = [], self._url(profile)
        relaxed = False
        if profile.get("model"):
            _, exact = model_slug(profile["brand"], profile["model"])
            relaxed = not exact
            if relaxed:
                log.info("otomoto: %s %s narrowed only to its family",
                         profile["brand"], profile["model"])
        for page in range(1, max_pages + 1):
            html = self.get(url, params=self._params(profile, page)).text
            soup = BeautifulSoup(html, "html.parser")
            cards = [a for a in soup.find_all("article") if a.get("data-id")]
            if not cards:
                if page == 1 and "captcha" in html[:4000].lower():
                    raise BotWallError("otomoto: captcha wall")
                break
            hit_known = False
            for card in cards:
                listing = self._to_listing(card, profile, relaxed)
                if not listing:
                    continue
                out.append(listing)
                hit_known |= listing.id in seen_ids
            if hit_known or len(cards) < PER_PAGE:
                break
        return out
