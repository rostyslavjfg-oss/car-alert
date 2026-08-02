"""openlane.eu - pan-European B2B dealer auctions.

Unlike every other source this one cannot run on the cron. The whole site sits
behind Cloudflare and answers 403 to anything that is not a real browser: plain
requests, and even Playwright's own APIRequestContext, get the challenge page.
What does work is a headless Chromium that loads the page and then calls the
site's own search endpoint from inside it, reusing the page's cookies and
anti-forgery token.

So this source is opt-in (`OPENLANE=1`) and meant for a machine you control -
GitHub Actions has neither a browser nor a residential IP.

Three query filters were reverse-engineered and are sent to the server -
`MakeModels`, `PriceRange` and `RegistrationYearRange`. Mileage, fuel and
gearbox keys were not found (every shape tried was silently ignored), so those
stay local. Without the make filter this source is unusable: lots arrive in
batches of one model, so the newest 100 can be all Volkswagens.

These are auctions, not classifieds. The price reported is BuyNowPrice when the
lot has one, otherwise the current bid - which can still climb.
"""

import logging
import os
import re

from .base import BaseScraper, BotWallError, Listing

log = logging.getLogger(__name__)

START_URL = "https://www.openlane.eu/sk/findcar"
SEARCH_PATH = "/en/findcarv6/search"
PER_PAGE = 50
NAV_TIMEOUT = 60000

# the endpoint is same-origin, so the call has to happen inside the page
SEARCH_JS = """async ([payload, token]) => {
  const r = await fetch('%s', {
    method: 'POST',
    credentials: 'include',
    headers: {'content-type': 'application/json',
              'x-requested-with': 'XMLHttpRequest',
              '__requestverificationtoken': token},
    body: JSON.stringify(payload)});
  return {status: r.status, body: r.ok ? await r.json() : (await r.text()).slice(0, 200)};
}""" % SEARCH_PATH

FUEL_WORDS = {"diesel": "diesel", "petrol": "petrol", "gasoline": "petrol",
              "hybrid": "hybrid", "electric": "electric"}
GEARBOX_WORDS = {"automatic": "automatic", "manual": "manual"}


def enabled() -> bool:
    return os.environ.get("OPENLANE", "").strip().lower() in ("1", "true", "yes")


class OpenLaneScraper(BaseScraper):
    source = "openlane"
    countries_served = None          # stock sits all over the EU

    @classmethod
    def serves(cls, countries) -> bool:
        # needs a browser, so it stays out of the cron unless switched on
        return enabled()

    def _spec(self, name: str) -> str:
        """CarNameEn reads "Audi A3 Sportback 30 TDI Advanced - Diesel - Automatic - 116"."""
        return " ".join(part.strip() for part in str(name or "").split(" - ")[1:]).lower()

    def _to_listing(self, auction: dict) -> Listing:
        name = auction.get("CarNameEn") or ""
        spec = self._spec(name)
        make = auction.get("CleanMake") or ""
        model = auction.get("CleanModel")
        if not model and make and name.lower().startswith(make.lower()):
            model = name[len(make):].split(" - ")[0].strip()
        thumb = auction.get("ThumbnailUrl") or None
        price = auction.get("BuyNowPrice") or auction.get("CurrentPrice")
        year = None
        registered = auction.get("DateFirstRegistration") or ""
        if re.match(r"^(19|20)\d{2}", str(registered)):
            year = int(str(registered)[:4])
        car_id = auction.get("CarId") or auction.get("AuctionId")
        # the site links lots by AuctionId, not CarId - /sk/car/<CarId> is a 404
        auction_id = auction.get("AuctionId") or car_id
        return Listing(
            id=f"{self.source}:{car_id}",
            source=self.source,
            brand=make,
            model=model or "",
            year=year,
            mileage_km=self.to_int(auction.get("Mileage")),
            price_eur=self.to_int(price) if auction.get("CurrencyCodeId") == "EUR"
            else self.to_int(price),
            fuel=self._word(spec, FUEL_WORDS),
            gearbox=self._word(spec, GEARBOX_WORDS),
            url=f"https://www.openlane.eu/sk/car/info?auctionId={auction_id}",
            image_url=thumb,
            images=[thumb] if thumb else [],
            title=name,
            country=(auction.get("CarCountryExtended") or "").upper() or None,
            city=None,
            damaged=None,                 # auctions do not expose a damage flag
            dealer_id=None,
            dealer_name=None,
        )

    @staticmethod
    def _query(profile: dict) -> dict:
        models = [m for m in ([profile["model"]] if profile.get("model") else []) if m]
        query = {"MakeModels": [{"Make": profile["brand"], "Models": models}]}
        if profile.get("price_max") or profile.get("price_min"):
            query["PriceRange"] = {"From": profile.get("price_min") or 0,
                                   "To": profile.get("price_max") or 1000000}
        if profile.get("year_from"):
            query["RegistrationYearRange"] = {"From": profile["year_from"], "To": 2100}
        return query

    def search(self, profile: dict, max_pages: int = 3, seen_ids=frozenset()) -> list:
        if not enabled():
            raise ValueError("openlane is off (set OPENLANE=1 on a machine with a browser)")
        try:
            from playwright.sync_api import sync_playwright
        except ImportError:
            raise ValueError("openlane needs playwright: pip install playwright "
                             "&& playwright install chromium")

        out = []
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True)
            try:
                context = browser.new_context(
                    locale="sk-SK",
                    user_agent="Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                               "AppleWebKit/537.36 (KHTML, like Gecko) "
                               "Chrome/126.0.0.0 Safari/537.36")
                page = context.new_page()
                page.goto(START_URL, wait_until="networkidle", timeout=NAV_TIMEOUT)
                token = page.evaluate(
                    "() => (document.querySelector('input[name=__RequestVerificationToken]')"
                    " || {}).value || ''")
                if not token:
                    raise BotWallError("openlane: no anti-forgery token (challenge page?)")

                for number in range(1, max_pages + 1):
                    payload = {
                        "query": self._query(profile),
                        "FacetRequest": [],
                        "Sort": {"Field": "BatchStartDateForSorting",
                                 "Direction": "descending", "SortType": "Field"},
                        "Paging": {"PageNumber": number, "ItemsPerPage": PER_PAGE},
                    }
                    result = page.evaluate(SEARCH_JS, [payload, token])
                    if result.get("status") != 200:
                        raise BotWallError(
                            f"openlane: search returned {result.get('status')}")
                    auctions = (result.get("body") or {}).get("Auctions") or []
                    if not auctions:
                        break
                    hit_known = False
                    for auction in auctions:
                        listing = self._to_listing(auction)
                        out.append(listing)
                        hit_known |= listing.id in seen_ids
                    if hit_known or len(auctions) < PER_PAGE:
                        break
            finally:
                browser.close()
        return out
