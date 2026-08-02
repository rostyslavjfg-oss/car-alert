"""Match scraped listings against a search profile.

Both sources already filter server-side; this is the safety net for sloppy
server-side filters and for fields one of the sources ignores.
"""


def _model_matches(listing_model: str, wanted: str) -> bool:
    listing_model = (listing_model or "").lower()
    wanted = (wanted or "").lower().strip()
    return not wanted or wanted in listing_model or listing_model in wanted


def matches(listing, profile: dict) -> bool:
    brand = (profile.get("brand") or "").lower()
    if brand and brand not in (listing.brand or "").lower():
        return False
    if not _model_matches(listing.model, profile.get("model")):
        return False

    if profile.get("year_from") and listing.year and listing.year < profile["year_from"]:
        return False
    if profile.get("year_to") and listing.year and listing.year > profile["year_to"]:
        return False
    if profile.get("mileage_max") and listing.mileage_km and listing.mileage_km > profile["mileage_max"]:
        return False
    if profile.get("price_max") and listing.price_eur and listing.price_eur > profile["price_max"]:
        return False
    if profile.get("price_min") and listing.price_eur and listing.price_eur < profile["price_min"]:
        return False

    # unknown fuel/gearbox on the listing is not a reason to drop it
    if profile.get("fuel") and listing.fuel and listing.fuel != profile["fuel"]:
        return False
    if profile.get("gearbox") and listing.gearbox and listing.gearbox != profile["gearbox"]:
        return False
    return True


def filter_matching(listings, profile: dict) -> list:
    return [l for l in listings if matches(l, profile)]
