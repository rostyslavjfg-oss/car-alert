#!/usr/bin/env python3
"""Self-check: `python tests.py` (add --live to also hit the four sites).

No pytest on purpose - this has to run inside the Actions job without extra deps.
"""

import argparse
import asyncio
import sys
import tempfile
import types
from pathlib import Path

from core import telegram_app as ta
from core.db import Db
from core.matcher import filter_matching, matches
from core.notifier import Alert, build_caption
from fetch_mobile_makes import load_brands
from main import expand_profile
from scrapers.base import BaseScraper, Listing

PASS, FAIL = [], []


def check(name, got, want=True):
    ok = (got == want) if want is not True else bool(got)
    (PASS if ok else FAIL).append(name)
    print(f"  {'ok  ' if ok else 'FAIL'} | {name}" + ("" if ok else f"  (got {got!r}, want {want!r})"))


def listing(**kw):
    base = dict(id="s:1", source="s", brand="BMW", model="320", year=2020, mileage_km=100000,
                price_eur=15000, fuel="diesel", gearbox="automatic", url="u", image_url=None)
    base.update(kw)
    return Listing(**base)


# --- parsing / normalization -------------------------------------------------
def test_normalizers():
    print("\nнормализация")
    check("'170.000 km' -> 170000", BaseScraper.to_int("170.000 km"), 170000)
    check("'€ 12,900' -> 12900", BaseScraper.to_int("€ 12,900"), 12900)
    check("'06-1997' -> 1997", BaseScraper.year_from_registration("06-1997"), 1997)
    check("hybrid beats petrol", BaseScraper.norm_fuel("Hybrid (Benzin/Elektro)"), "hybrid")
    check("Schaltgetriebe -> manual", BaseScraper.norm_gearbox("Schaltgetriebe"), "manual")
    check("'25 000' -> 25000", ta._opt_int("25 000"), 25000)
    check("skip button on a number step", ta._opt_int(ta.BTN_SKIP), None)
    check("bad number raises", _raises(lambda: ta._opt_int("абв")))


def _raises(fn):
    try:
        fn()
    except ValueError:
        return True
    return False


def test_brands():
    print("\nразбор марок")
    brands = load_brands()
    for typed, want in [("Škoda", "Skoda"), ("шкода", "Skoda"), ("бмв", "BMW"),
                        ("vw", "Volkswagen"), ("мерс", "Mercedes-Benz"),
                        ("citroën", "Citroen"), ("сеат", "SEAT")]:
        check(f"{typed} -> {want}", ta._resolve_brand(brands, typed), want)
    check("мусор не проходит", ta._resolve_brand(brands, "Нетакоймарки"), None)
    broken = [a for a, t in ta.BRAND_ALIASES.items() if t not in brands]
    check("все алиасы указывают на реальные марки", broken, [])


def test_damage():
    print("\nбитые машины")
    from scrapers.bazos import looks_damaged
    for text, want in [("havarované", True), ("po nehode", True), ("na diely", True),
                       ("nepojazdné", True), ("NEBÚRANÉ", False),
                       ("bez poškodenia", False), ("nehavarované", False),
                       ("pekný stav", False)]:
        check(f"{text!r} -> {'битая' if want else 'целая'}", looks_damaged(text), want)
    from scrapers.base import find_vin, looks_damaged_de
    check("VIN найден", find_vin("Predám BMW, VIN WBAJB91060B168910, serviska"),
          "WBAJB91060B168910")
    check("VIN с I/O/Q не матчится", find_vin("IIIIIIIIIIIIIIIII WWW00000000000000"), None)
    check("не-VIN 17 цифр отброшен", find_vin("12345678901234567"), None)
    check("нет VIN -> None", find_vin("обычный текст"), None)
    for text, want in [("Motorschaden, fährt nicht", True), ("Getriebeschaden", True),
                       ("Unfallwagen Bastler", True), ("Für Export", True),
                       ("unfallfrei, scheckheftgepflegt", False), ("kein Unfall", False),
                       ("ohne Unfallschaden", False)]:
        check(f"de: {text!r}", looks_damaged_de(text), want)
    from scrapers.bazos import looks_damaged as lb
    check("sk: chybný motor -> битая", lb("chybný motor, treba opravu"), True)
    check("sk: na súčiastky -> битая", lb("predám na súčiastky"), True)
    prof = {"exclude_damaged": 1}
    check("битая отсеивается", matches(listing(damaged=True), prof), False)
    check("целая проходит", matches(listing(damaged=False), prof), True)
    check("неизвестно = не битая", matches(listing(damaged=None), prof), True)
    check("выключенный фильтр пропускает битую",
          matches(listing(damaged=True), {"exclude_damaged": 0}), True)


def test_currency():
    print("\nвалюты")
    from core import currency
    r = currency.rates()
    check("курс CZK есть", r.get("CZK", 0) > 10)
    check("курс PLN есть", r.get("PLN", 0) > 2)
    check("EUR = 1", r["EUR"], 1.0)
    eur = currency.to_eur(500000, "CZK")
    check("500 000 Kč -> разумные евро", 15000 < eur < 30000)
    czk = currency.from_eur(eur, "CZK")
    check("обратная конвертация сходится", abs(czk - 500000) < 2000)
    check("None остаётся None", currency.to_eur(None, "CZK"), None)


def test_otomoto_slugs():
    print("\nмодели otomoto")
    from scrapers.otomoto import model_slug, looks_damaged
    for brand, model, want in [("BMW", "320", ("seria-3", False)),
                               ("BMW", "X5", ("x5", True)),
                               ("Mercedes-Benz", "C 200", ("klasa-c", False)),
                               ("Audi", "A4", ("a4", True)),
                               ("Volkswagen", "Golf", ("golf", True))]:
        check(f"{brand} {model}", model_slug(brand, model), want)
    check("uszkodzony -> битая", looks_damaged("Uszkodzony przód"), True)
    check("bezwypadkowy -> целая", looks_damaged("Bezwypadkowy, serwis ASO"), False)
    check("nieuszkodzony -> целая", looks_damaged("Nieuszkodzony"), False)
    lst = listing(model="Seria 3", title="BMW Seria 3", model_relaxed=True)
    check("ослабленная модель проходит", matches(lst, {"models": ["320"]}), True)
    strict = listing(model="Seria 3", title="BMW Seria 3")
    check("без послабления не проходит", matches(strict, {"models": ["320"]}), False)


def test_matcher():
    print("\nматчер")
    check("одна из двух марок", matches(listing(brand="Audi"), {"brands": ["BMW", "Audi"]}), True)
    check("чужая марка", matches(listing(brand="Kia"), {"brands": ["BMW", "Audi"]}), False)
    check("одна из моделей", matches(listing(model="330"), {"models": ["320", "330"]}), True)
    check("модель по заголовку",
          matches(listing(model="3er-Reihe", title="BMW 320d Touring"), {"models": ["320"]}), True)
    check("любое из двух топлив",
          matches(listing(fuel="hybrid"), {"fuels": ["diesel", "hybrid"]}), True)
    check("топливо мимо", matches(listing(fuel="petrol"), {"fuels": ["diesel"]}), False)
    check("неизвестное топливо не отсеивается",
          matches(listing(fuel=None), {"fuels": ["diesel"]}), True)
    check("цена выше лимита", matches(listing(price_eur=30000), {"price_max": 20000}), False)
    check("год ниже лимита", matches(listing(year=2015), {"year_from": 2018}), False)
    check("старый одиночный формат", matches(listing(brand="BMW"), {"brand": "BMW"}), True)


def test_expand():
    print("\nразворот поиска")
    c = expand_profile({"name": "t", "brands": ["BMW", "Audi"], "models": ["320"],
                        "fuels": ["diesel", "hybrid"], "gearboxes": ["automatic"]})
    check("2 марки -> 2 запроса", len(c), 2)
    check("модель отброшена при нескольких марках", c[0]["model"], None)
    check("одно топливо из двух не идёт на сервер", c[0]["fuel"], None)
    check("одна коробка идёт на сервер", c[0]["gearbox"], "automatic")
    c = expand_profile({"name": "t", "brands": ["BMW"], "models": ["320", "330", "M3"]})
    check("1 марка + 3 модели -> 3 запроса", len(c), 3)
    big = expand_profile({"name": "t", "brands": [f"B{i}" for i in range(20)]})
    check("комбинации ограничены", len(big), 12)


def test_db():
    print("\nбаза")
    with tempfile.TemporaryDirectory() as tmp:
        db = Db(Path(tmp) / "t.db")
        sid = db.add_search(1, {"name": "x", "brands": ["BMW", "Audi"], "models": ["320"],
                                "fuels": ["diesel"], "countries": ["D", "SK"]})
        p = db.get_search(sid, 1)
        check("списки читаются обратно", p["brands"], ["BMW", "Audi"])
        check("страны читаются обратно", p["countries"], ["D", "SK"])
        check("битые исключены по умолчанию", p["exclude_damaged"], 1)
        legacy = db.add_search(1, {"name": "old", "brand": "Skoda", "fuel": "diesel"})
        check("одиночные поля превращаются в списки",
              db.get_search(legacy, 1)["brands"], ["Skoda"])
        check("без марки нельзя", _raises(lambda: db.add_search(1, {"name": "n"})))

        db.upsert(listing(id="a:1", images=["p1", "p2"], country="DE", damaged=False))
        row = dict(db.conn.execute("SELECT * FROM listings WHERE id='a:1'").fetchone())
        check("фото сохранены как JSON", row["images"], '["p1", "p2"]')
        check("страна сохранена", row["country"], "DE")
        alert = Alert(row=row, chat_id="1", search_name="x")
        check("фото читаются из JSON", alert.photos, ["p1", "p2"])

        check("лайк ставится", db.toggle_favorite(1, "a:1"), True)
        check("повторный лайк снимает", db.toggle_favorite(1, "a:1"), False)
        db.record_swipe(1, "a:1", "like")
        check("свайп записан", db.swiped_ids(1), {"a:1"})
        token = db.chat_token(1)
        check("токен обратим", db.chat_for_token(token), "1")
        db.block_dealer(1, "a:9", "Dealer")
        check("дилер заблокирован", list(db.blocked_dealers(1)), ["a:9"])
        db.close()


def test_caption():
    print("\nкарточка")
    cap = build_caption({"brand": "BMW", "model": "320", "year": 2019, "mileage_km": 130000,
                         "price_eur": 17900, "fuel": "diesel", "gearbox": "automatic",
                         "source": "mobilede", "country": "DE", "city": "Köln",
                         "dealer_name": "Auto X", "url": "https://x"})
    check("страна в карточке", "🇩🇪 Германия, Köln" in cap)
    check("источник читаемо", "mobile.de" in cap)
    check("русские значения", "дизель" in cap and "автомат" in cap)
    drop = build_caption({"brand": "BMW", "model": "320", "price_eur": 9000,
                          "source": "bazos", "country": "SK", "damaged": True, "url": "u"},
                         old_price=12000)
    check("падение цены посчитано", "на 25%" in drop)
    check("битая помечена", "⚠️ битая" in drop)


# --- dialog ------------------------------------------------------------------
class _Msg:
    def __init__(self, text=None, log=None):
        self.text, self.log = text, log

    async def reply_text(self, text, **kw):
        kb = kw.get("reply_markup")
        rows = None
        if hasattr(kb, "keyboard"):
            rows = [[b.text for b in r] for r in kb.keyboard]
        elif hasattr(kb, "inline_keyboard"):
            rows = [[b.text for b in r] for r in kb.inline_keyboard]
        self.log.append((text.split("\n")[0], rows))
        return _Msg(log=self.log)


class _Query:
    def __init__(self, data, log):
        self.data, self.log = data, log
        self.message = _Msg(log=log)

    async def answer(self, text=None, **kw):
        if text:
            self.log.append((f"[toast] {text}", None))

    async def edit_message_text(self, text, **kw):
        self.log.append((f"[edit] {text}", None))

    async def edit_message_reply_markup(self, kb):
        self.log.append(("[кнопки]", [[b.text for b in r] for r in kb.inline_keyboard]))


def test_dialog(live_models=False):
    print("\nдиалог /add с многовыбором")
    with tempfile.TemporaryDirectory() as tmp:
        db, log = Db(Path(tmp) / "d.db"), []
        brands = load_brands()
        ctx = types.SimpleNamespace(
            application=types.SimpleNamespace(
                bot_data={"db": db, "brands": brands, "run_scrape": None}),
            args=[])

        def upd(text=None, cb=None):
            u = types.SimpleNamespace()
            u.effective_chat = types.SimpleNamespace(id=9)
            u.effective_user = types.SimpleNamespace(username="t")
            u.message = _Msg(text, log) if text is not None else None
            u.callback_query = _Query(cb, log) if cb else None
            return u

        async def go():
            await ta.on_text(upd(ta.BTN_ADD), ctx)
            # required multi step refuses an empty selection
            await ta.on_callback(upd(cb="ms:0:done"), ctx)
            await ta.on_callback(upd(cb="ms:0:0"), ctx)      # BMW
            await ta.on_text(upd("ауди"), ctx)               # typed second brand
            await ta.on_callback(upd(cb="ms:0:done"), ctx)
            await ta.on_text(upd("2019"), ctx)               # year (models skipped)
            await ta.on_text(upd("20000"), ctx)
            await ta.on_text(upd(ta.BTN_SKIP), ctx)          # mileage
            await ta.on_callback(upd(cb="ms:5:0"), ctx)      # diesel
            await ta.on_callback(upd(cb="ms:5:2"), ctx)      # hybrid
            await ta.on_callback(upd(cb="ms:5:done"), ctx)
            await ta.on_callback(upd(cb="ms:6:done"), ctx)   # gearbox: any
            await ta.on_callback(upd(cb="ms:7:0"), ctx)      # D
            await ta.on_callback(upd(cb="ms:7:2"), ctx)      # SK
            await ta.on_callback(upd(cb="ms:7:done"), ctx)

        asyncio.run(go())
        texts = " || ".join(t for t, _ in log)
        check("пустой обязательный шаг отклонён", "Нужно выбрать хотя бы одну" in texts)
        check("шаг моделей пропущен при 2 марках", "Модели?" not in texts)
        saved = db.list_searches(9)
        check("поиск сохранён", len(saved), 1)
        if saved:
            p = saved[0]
            check("две марки", p["brands"], ["BMW", "Audi"])
            check("два топлива", p["fuels"], ["diesel", "hybrid"])
            check("коробка не важна", p["gearboxes"], [])
            check("две страны", p["countries"], ["D", "SK"])
            check("год записан", p["year_from"], 2019)
            check("пробег пропущен", p["mileage_max"], None)
            check("в описании обе марки", "BMW / Audi" in ta.describe(p))
        db.close()


def test_dialog_single_brand():
    print("\nдиалог: одна марка -> шаг моделей появляется")
    with tempfile.TemporaryDirectory() as tmp:
        db, log = Db(Path(tmp) / "d2.db"), []
        ctx = types.SimpleNamespace(
            application=types.SimpleNamespace(
                bot_data={"db": db, "brands": load_brands(), "run_scrape": None}),
            args=[])

        def upd(text=None, cb=None):
            u = types.SimpleNamespace()
            u.effective_chat = types.SimpleNamespace(id=11)
            u.effective_user = types.SimpleNamespace(username="t")
            u.message = _Msg(text, log) if text is not None else None
            u.callback_query = _Query(cb, log) if cb else None
            return u

        async def go():
            await ta.on_text(upd(ta.BTN_ADD), ctx)
            await ta.on_callback(upd(cb="ms:0:0"), ctx)      # BMW only
            await ta.on_callback(upd(cb="ms:0:done"), ctx)
            await ta.on_text(upd("320"), ctx)                # typed model
            await ta.on_text(upd("330"), ctx)                # second typed model
            await ta.on_callback(upd(cb="ms:1:done"), ctx)

        asyncio.run(go())
        texts = " || ".join(t for t, _ in log)
        check("шаг моделей показан", "Модели?" in texts)
        step, draft = db.get_dialog(11)
        check("две модели набраны текстом", draft.get("models"), ["320", "330"])
        check("дальше идёт год", step, "year_from")
        db.close()


def test_keyboards():
    print("\nклавиатуры")
    kb = ta.main_keyboard()
    labels = [b.text for row in kb.keyboard for b in row]
    check("основные кнопки на месте", ta.BTN_ADD in labels and ta.BTN_RUN in labels)
    check("все кнопки имеют обработчик",
          [l for l in labels if l not in ta.BUTTONS], [])
    kb = ta.multi_keyboard(0, [("BMW", "BMW"), ("Audi", "Audi")], ["BMW"])
    flat = [b.text for row in kb.inline_keyboard for b in row]
    check("выбранное отмечено", "✅ BMW" in flat and "▫️ Audi" in flat)
    check("callback короче 64 байт",
          all(len(b.callback_data.encode()) <= 64
              for row in kb.inline_keyboard for b in row))
    # every inline button must carry callback_data and no url - passing the
    # action positionally made it a url and Telegram answered BadRequest
    for paused in (0, 1):
        sk = ta.search_keyboard({"id": 3, "paused": paused})
        row = sk.inline_keyboard[0]
        check(f"кнопки поиска (paused={paused}) шлют callback",
              [b.callback_data for b in row],
              ["resume:3" if paused else "pause:3", "del:3"])
        check(f"кнопки поиска (paused={paused}) без url",
              [b.url for b in row], [None, None])
    lk = ta.listing_keyboard("mobilede:123", "mobilede:456", 7)
    check("кнопки под объявлением",
          [b.callback_data for b in lk.inline_keyboard[0]],
          ["fav:mobilede:123", "blk:mobilede:456", "mute:7"])


def test_countries_gating():
    print("\nгеография источников")
    from scrapers.autoscout24 import AutoScout24Scraper
    from scrapers.bazos import BazosScraper
    from scrapers.mobilede import MobileDeScraper
    from scrapers.willhaben import WillhabenScraper
    check("as24 не лезет в чисто-SK поиск", AutoScout24Scraper.serves(["SK"]), False)
    check("bazos только SK", BazosScraper.serves(["D"]), False)
    check("willhaben только AT", WillhabenScraper.serves(["A"]), True)
    check("mobile.de везде", MobileDeScraper.serves(["SK"]), True)
    from scrapers.otomoto import OtomotoScraper
    from scrapers.sauto import SautoScraper
    check("sauto только CZ", SautoScraper.serves(["CZ"]), True)
    check("sauto не для D", SautoScraper.serves(["D"]), False)
    check("otomoto только PL", OtomotoScraper.serves(["PL"]), True)
    import os
    from scrapers.openlane import OpenLaneScraper
    was = os.environ.pop("OPENLANE", None)
    check("openlane выключен без флага", OpenLaneScraper.serves(["D"]), False)
    os.environ["OPENLANE"] = "1"
    check("openlane включается флагом", OpenLaneScraper.serves(["D"]), True)
    q = OpenLaneScraper._query({"brand": "BMW", "model": "3 Series",
                                "price_max": 25000, "year_from": 2016})
    check("запрос openlane: марка", q["MakeModels"], [{"Make": "BMW", "Models": ["3 Series"]}])
    check("запрос openlane: цена", q["PriceRange"]["To"], 25000)
    check("запрос openlane: год", q["RegistrationYearRange"]["From"], 2016)
    if was is None:
        os.environ.pop("OPENLANE", None)
    else:
        os.environ["OPENLANE"] = was
    s = AutoScout24Scraper(load_brands())
    check("неизвестный код страны выкинут",
          s._params({"brand": "BMW", "countries": ["D", "A", "SK"]}, 1)["cy"], "A,D")


def test_live():
    print("\nживые источники (--live)")
    from scrapers.autoscout24 import AutoScout24Scraper
    from scrapers.bazos import BazosScraper
    from scrapers.mobilede import MobileDeScraper
    from scrapers.otomoto import OtomotoScraper
    from scrapers.sauto import SautoScraper
    from scrapers.willhaben import WillhabenScraper
    brands = load_brands()
    prof = {"name": "t", "brands": ["BMW"], "models": ["320"], "brand": "BMW", "model": "320",
            "price_max": 30000, "year_from": 2015, "exclude_damaged": 1}
    for cls in (AutoScout24Scraper, MobileDeScraper, BazosScraper, WillhabenScraper,
                SautoScraper, OtomotoScraper):
        try:
            got = cls(brands).search(prof, max_pages=1)
        except Exception as exc:
            check(f"{cls.source}: запрос", f"{type(exc).__name__}: {exc}", True)
            continue
        check(f"{cls.source}: объявления пришли", len(got) > 0)
        if not got:
            continue
        kept = filter_matching(got, prof)
        with_photo = sum(1 for l in got if l.images)
        countries = {l.country for l in got}
        check(f"{cls.source}: цены разобраны", all(l.price_eur is not None for l in got[:5]))
        check(f"{cls.source}: страна проставлена", None not in countries)
        years = [l.year for l in got if l.year]
        check(f"{cls.source}: годы правдоподобны",
              not years or all(1980 <= y <= 2027 for y in years))
        check(f"{cls.source}: фото есть", with_photo > 0)
        print(f"       {len(got)} шт | после фильтра {len(kept)} | страны {countries}"
              f" | с фото {with_photo}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--live", action="store_true", help="also hit the four sites")
    args = ap.parse_args()

    test_normalizers()
    test_brands()
    test_damage()
    test_currency()
    test_otomoto_slugs()
    test_matcher()
    test_expand()
    test_db()
    test_caption()
    test_dialog()
    test_dialog_single_brand()
    test_keyboards()
    test_countries_gating()
    if args.live:
        test_live()

    print(f"\n{'=' * 46}\nпройдено {len(PASS)}, провалено {len(FAIL)}")
    for name in FAIL:
        print("  FAIL:", name)
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
