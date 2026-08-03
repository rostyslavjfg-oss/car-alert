"""auto.bazos.sk - Slovak classifieds, mostly private sellers.

This is the closest working equivalent to Facebook Marketplace for SK: plain
HTML, no login, no bot-wall. The trade-off is unstructured data - year, mileage,
fuel and gearbox only exist inside free text, so they are parsed best-effort and
left as None when the seller did not write them. The matcher treats an unknown
field as "don't care", so a sloppy ad is not silently dropped.
"""

import logging
import re

from bs4 import BeautifulSoup

from .base import BaseScraper, Listing, find_vin

log = logging.getLogger(__name__)

BASE = "https://auto.bazos.sk/"
PER_PAGE = 20

YEAR_RE = re.compile(r"(?:r\.?\s*v\.?|rok|rv)[\s.:]*?((?:19|20)\d{2})", re.I)
BARE_YEAR_RE = re.compile(r"\b(19[89]\d|20[0-3]\d)\b")
KM_RE = re.compile(r"(\d[\d\s.,]{2,})\s*(?:tis\.?\s*)?km\b", re.I)
TKM_RE = re.compile(r"(\d{2,3})\s*(?:tis|tkm)\b", re.I)
# matched with word boundaries - a stray substring must not invent a spec the
# seller never wrote, because a wrong value lets a non-matching car through
FUEL_WORDS = [(r"naft\w*|diesel|\btdi\b|\bhdi\b|\bcdti\b|\bdci\b", "diesel"),
              (r"benz[ií]n\w*|\btsi\b|\btfsi\b", "petrol"),
              (r"hybrid\w*", "hybrid"),
              (r"elektro\w*|\bev\b", "electric")]
GEARBOX_WORDS = [(r"automat\w*|\bdsg\b|\bsteptronic\b", "automatic"),
                 (r"manu[áa]l\w*|\bmanual\b", "manual")]
# Sellers advertise the opposite just as often ("NEBÚRANÉ", "bez poškodenia"),
# and a false positive silently hides a good car - so negations are cut first.
NEGATED_RE = re.compile(
    r"\bbez\s+(poškoden|poskoden|nehod|nehôd|havár|havar|búrač|burac)\w*"
    r"|\bne(havar|búran|buran|poškoden|poskoden)\w*", re.I)
DAMAGE_RE = re.compile(
    r"\bhavar\w*|\bpo nehode\b|\bnabúran\w*|\bnaburan\w*|\bbúran\w*|\bburan\w*"
    r"|\bpoškoden\w*|\bposkoden\w*|\bna diely\b|\bna náhradné diely\b"
    r"|\bnepojazdn\w*|\bbez tp\b|\bbúračk\w*|\bburack\w*"
    r"|\bchybn\w* motor|\bvadn\w* motor|\bporucha motor\w*|\bzadret\w*"
    r"|\bna súčiastky\b|\bna suciastky\b", re.I)


def looks_damaged(text: str) -> bool:
    return bool(DAMAGE_RE.search(NEGATED_RE.sub(" ", text)))


class BazosScraper(BaseScraper):
    source = "bazos"
    countries_served = {"SK"}

    def _headers(self):
        return {**super()._headers(), "Accept-Language": "sk,cs;q=0.9,en;q=0.8"}

    def _params(self, profile: dict, page: int) -> dict:
        query = " ".join(filter(None, [profile["brand"], profile.get("model")]))
        p = {"hledat": query, "rubriky": "auto", "order": "", "crz": (page - 1) * PER_PAGE}
        if profile.get("price_min"):
            p["cenaod"] = profile["price_min"]
        if profile.get("price_max"):
            p["cenado"] = profile["price_max"]
        return p

    @staticmethod
    def _parse_year(text: str):
        m = YEAR_RE.search(text)
        if m:
            return int(m.group(1))
        years = [int(y) for y in BARE_YEAR_RE.findall(text)]
        return max(years) if years else None

    @staticmethod
    def _parse_mileage(text: str):
        m = TKM_RE.search(text)
        if m:
            return int(m.group(1)) * 1000
        for raw in KM_RE.findall(text):
            km = int(re.sub(r"[^\d]", "", raw) or 0)
            if 1000 <= km <= 999000:
                return km
        return None

    @staticmethod
    def _word(text: str, patterns):
        low = text.lower()
        for pattern, value in patterns:
            if re.search(pattern, low):
                return value
        return None

    def _to_listing(self, item, profile: dict):
        link = item.select_one("h2.nadpis a")
        if not link or not link.get("href"):
            return None
        href = link["href"]
        listing_id = (re.search(r"/inzerat/(\d+)/", href) or [None, href])[1]
        title = link.get_text(" ", strip=True)
        desc = item.select_one("div.popis")
        text = f"{title} {desc.get_text(' ', strip=True) if desc else ''}"
        price_tag = item.select_one("div.inzeratycena")
        image = item.select_one("img")
        return Listing(
            id=f"{self.source}:{listing_id}",
            source=self.source,
            brand=profile["brand"],
            model=profile.get("model") or "",
            year=self._parse_year(text),
            mileage_km=self._parse_mileage(text),
            price_eur=self.to_int(price_tag.get_text(strip=True)) if price_tag else None,
            fuel=self._word(text, FUEL_WORDS),
            gearbox=self._word(text, GEARBOX_WORDS),
            url="https://auto.bazos.sk" + href if href.startswith("/") else href,
            # the result list holds one thumbnail; more would cost a request per ad
            image_url=image.get("src") if image else None,
            images=[image["src"]] if image and image.get("src") else [],
            title=title,
            country="SK",
            city=None,
            # bazos has no damage flag - the seller either writes it or not
            damaged=looks_damaged(text),
            vin=find_vin(text),
            dealer_id=None,        # bazos exposes no stable seller id in the result list
            dealer_name=None,
        )

    def search(self, profile: dict, max_pages: int = 3, seen_ids=frozenset()) -> list:
        out = []
        for page in range(1, max_pages + 1):
            html = self.get(BASE, params=self._params(profile, page)).text
            items = BeautifulSoup(html, "html.parser").select("div.inzeraty.inzeratyflex")
            if not items:
                break
            hit_known = False
            for item in items:
                listing = self._to_listing(item, profile)
                if not listing:
                    continue
                out.append(listing)
                hit_known |= listing.id in seen_ids
            if hit_known or len(items) < PER_PAGE:
                break
        return out
