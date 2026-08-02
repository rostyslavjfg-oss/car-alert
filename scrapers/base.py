"""Shared scraper plumbing: HTTP with UA rotation + delays, and the normalized schema."""

import logging
import random
import re
import time
import unicodedata
from dataclasses import dataclass, asdict, field
from typing import Optional

import requests

log = logging.getLogger(__name__)

USER_AGENTS = [
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:127.0) Gecko/20100101 Firefox/127.0",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.5 Safari/605.1.15",
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36",
]

MIN_DELAY, MAX_DELAY = 2.0, 4.0
MAX_IMAGES = 5              # Telegram albums hold 10; 5 is enough to judge a car

# autoscout24 single-letter country codes -> ISO used by mobile.de
COUNTRY_FLAG = {"DE": "🇩🇪 Германия", "AT": "🇦🇹 Австрия", "SK": "🇸🇰 Словакия",
                "CZ": "🇨🇿 Чехия", "PL": "🇵🇱 Польша", "NL": "🇳🇱 Нидерланды",
                "BE": "🇧🇪 Бельгия", "LU": "🇱🇺 Люксембург", "IT": "🇮🇹 Италия",
                "ES": "🇪🇸 Испания", "FR": "🇫🇷 Франция", "HU": "🇭🇺 Венгрия",
                "SI": "🇸🇮 Словения", "HR": "🇭🇷 Хорватия", "RO": "🇷🇴 Румыния"}

COUNTRY_MAP = {"D": "DE", "A": "AT", "B": "BE", "NL": "NL", "L": "LU",
               "I": "IT", "E": "ES", "F": "FR", "SK": "SK", "CZ": "CZ", "PL": "PL"}

FUEL_WORDS = {
    "diesel": "diesel",
    "benzin": "petrol", "gasoline": "petrol", "petrol": "petrol", "super": "petrol",
    "elektro": "electric", "electric": "electric", "elektrisch": "electric",
    "hybrid": "hybrid", "hybride": "hybrid",
}
GEARBOX_WORDS = {
    "schaltgetriebe": "manual", "manual": "manual", "manuell": "manual",
    "automatik": "automatic", "automatic": "automatic", "halbautomatik": "automatic",
    "semi-automatic": "automatic",
}


@dataclass
class Listing:
    id: str                 # "{source}:{listing_id}" - globally unique
    source: str
    brand: str
    model: str
    year: Optional[int]
    mileage_km: Optional[int]
    price_eur: Optional[int]
    fuel: Optional[str]
    gearbox: Optional[str]
    url: str
    image_url: Optional[str]            # cover photo, kept for one-photo fallbacks
    images: list = field(default_factory=list)   # up to MAX_IMAGES, sent as an album
    title: Optional[str] = None         # raw ad headline, used where model is fuzzy
    country: Optional[str] = None       # ISO-2, e.g. DE / AT / SK
    city: Optional[str] = None
    damaged: Optional[bool] = None      # None = the source did not say
    # set when a source can only narrow to a model *family* (otomoto knows
    # "seria-3", not "320"); the matcher then trusts the source's own filter
    # instead of dropping everything
    model_relaxed: bool = False
    dealer_id: Optional[str] = None     # seller, for the "hide dealer" button
    dealer_name: Optional[str] = None
    first_seen: Optional[str] = None    # filled by db on insert

    @property
    def dealer_key(self) -> Optional[str]:
        return f"{self.source}:{self.dealer_id}" if self.dealer_id else None

    def as_dict(self):
        return asdict(self)


class BotWallError(RuntimeError):
    """Site answered with a captcha / access-denied page."""


class BaseScraper:
    source = "base"
    # which search countries this source can serve; None = any
    countries_served = None

    @classmethod
    def serves(cls, countries) -> bool:
        if cls.countries_served is None:
            return True
        return bool({str(c).upper() for c in (countries or ["D"])} & cls.countries_served)

    def __init__(self, brands: dict, timeout: int = 25):
        self.brands = brands
        self.timeout = timeout
        self.session = requests.Session()
        self._last_request = 0.0

    # --- HTTP -------------------------------------------------------------
    def _headers(self) -> dict:
        return {
            "User-Agent": random.choice(USER_AGENTS),
            "Accept-Language": "de-DE,de;q=0.9,en;q=0.8",
            "Accept": "text/html,application/xhtml+xml,application/json;q=0.9,*/*;q=0.8",
        }

    def _throttle(self):
        elapsed = time.monotonic() - self._last_request
        wait = random.uniform(MIN_DELAY, MAX_DELAY) - elapsed
        if wait > 0:
            time.sleep(wait)
        self._last_request = time.monotonic()

    def get(self, url: str, params=None, headers=None) -> requests.Response:
        self._throttle()
        r = self.session.get(url, params=params, timeout=self.timeout,
                             headers={**self._headers(), **(headers or {})})
        if r.status_code in (403, 429) or "Zugriff verweigert" in r.text[:2000]:
            raise BotWallError(f"{self.source}: bot-wall / {r.status_code} on {r.url}")
        r.raise_for_status()
        return r

    # --- normalization helpers -------------------------------------------
    @staticmethod
    def to_int(value) -> Optional[int]:
        """'170.000 km' / '€ 12,900' / '12900' -> int"""
        if value is None:
            return None
        if isinstance(value, (int, float)):
            return int(value)
        digits = re.sub(r"[^\d]", "", str(value))
        return int(digits) if digits else None

    @staticmethod
    def year_from_registration(value) -> Optional[int]:
        """'06-1997' / '01/2009' / '2018' -> 1997 / 2009 / 2018"""
        if not value:
            return None
        m = re.search(r"(19|20)\d{2}", str(value))
        return int(m.group(0)) if m else None

    @staticmethod
    def _word(value, table) -> Optional[str]:
        if not value:
            return None
        text = unicodedata.normalize("NFKD", str(value)).encode("ascii", "ignore").decode().lower()
        for key, norm in table.items():
            if key in text:
                return norm
        return None

    @classmethod
    def norm_fuel(cls, value) -> Optional[str]:
        # check hybrid first: "Hybrid (Benzin/Elektro)" must not read as petrol
        if value and "hybrid" in str(value).lower():
            return "hybrid"
        return cls._word(value, FUEL_WORDS)

    @classmethod
    def norm_gearbox(cls, value) -> Optional[str]:
        return cls._word(value, GEARBOX_WORDS)

    # --- interface --------------------------------------------------------
    def search(self, profile: dict, max_pages: int = 3, seen_ids=frozenset()) -> list:
        raise NotImplementedError
