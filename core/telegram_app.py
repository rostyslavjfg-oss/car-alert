"""Telegram bot: command handlers and inline-button callbacks.

Dialog state lives in SQLite rather than in process memory, so the exact same
handlers work in two modes:

  * bot.py            - long polling, answers instantly
  * main.py --drain   - one shot inside the GitHub Actions run (<=30 min lag)

Never poll from both at once - Telegram answers the second one with 409.
"""

import html
import logging
from telegram import (InlineKeyboardButton, InlineKeyboardMarkup, KeyboardButton,
                      ReplyKeyboardMarkup, Update, WebAppInfo)
from telegram.constants import ParseMode
from telegram.ext import (Application, CallbackQueryHandler, CommandHandler,
                          ContextTypes, MessageHandler, filters)
import json
import os

from fetch_mobile_makes import normalize

from .db import Db

log = logging.getLogger(__name__)

HELP = """<b>Автоподбор</b> — слежу за объявлениями на mobile.de, autoscout24, bazos.sk и willhaben.at

/add — новый поиск (по шагам)
/list — мои поиски
/del &lt;id&gt; — удалить поиск
/pause &lt;id&gt; — приостановить
/resume &lt;id&gt; — возобновить
/fav — избранное (то, что лайкнул свайпами)
/dealers — скрытые продавцы
/run — прогнать поиск прямо сейчас
/status — счётчики
/cancel — отменить текущий диалог

Новые объявления приходят сами, каждые 30 минут."""

# /add dialog: step -> (question, draft key, parser)
SKIP_WORDS = {"-", "любой", "любая", "неважно", "пропустить", "— пропустить",
              "any", "skip", "no"}


def _is_skip(text: str) -> bool:
    return text.strip().lower() in SKIP_WORDS


def _opt_int(text: str):
    # check for a skip *before* stripping spaces: "— пропустить" has one
    if _is_skip(text):
        return None
    digits = text.strip().replace(" ", "").replace(".", "").replace("\u00a0", "")
    if not digits.isdigit():
        raise ValueError("нужно число или «-»")
    return int(digits)


def _opt_text(text: str):
    return None if _is_skip(text) else text.strip()


def _choice(options: dict):
    """options maps every accepted spelling (ru and en) to the canonical value."""
    def parse(text: str):
        if _is_skip(text):
            return None
        value = text.strip().lower()
        if value not in options:
            raise ValueError("выбери одно из: " + ", ".join(dict.fromkeys(options)) + ", либо «-»")
        return options[value]
    return parse


def _countries(text: str):
    if _is_skip(text):
        return ["D"]
    value = text.strip().upper()
    codes = [c.strip() for c in value.replace(";", ",").split(",") if c.strip()]
    return codes or ["D"]


FUEL_CHOICE = {"diesel": "diesel", "дизель": "diesel", "бензин": "petrol", "petrol": "petrol",
               "гибрид": "hybrid", "hybrid": "hybrid", "электро": "electric", "electric": "electric"}
GEARBOX_CHOICE = {"автомат": "automatic", "automatic": "automatic",
                  "механика": "manual", "manual": "manual", "ручная": "manual"}

POPULAR_BRANDS = ["BMW", "Audi", "Mercedes-Benz", "Volkswagen", "Skoda", "Toyota",
                  "Ford", "Opel", "Peugeot", "Renault", "Hyundai", "Kia",
                  "Volvo", "Mazda", "Nissan", "SEAT", "Honda", "Tesla"]


def _model_options(draft: dict, bot_data: dict) -> list:
    """Model names mobile.de knows for this make - typing one is still allowed."""
    brand = (bot_data.get("brands") or {}).get(draft.get("brand")) or {}
    make_id = brand.get("mobilede_make_id")
    if not make_id:
        return []
    try:
        import requests
        r = requests.get(f"https://www.mobile.de/svc/r/models/{make_id}",
                         headers={"X-Mobile-Client": "de.mobile.android.app",
                                  "Accept": "application/json"}, timeout=15)
        r.raise_for_status()
        models = r.json().get("models") or []
    except Exception as exc:
        log.warning("model options unavailable for %s: %s", draft.get("brand"), exc)
        return []
    # skip the "g" entries: those are groups like "3er Reihe", which neither the
    # autoscout24 url nor the matcher understands
    return [m["n"] for m in models if not m.get("g") and m.get("n")][:24]


COUNTRIES = [("D", "Германия"), ("A", "Австрия"), ("SK", "Словакия"), ("CZ", "Чехия"),
             ("PL", "Польша"), ("NL", "Нидерланды"), ("B", "Бельгия"),
             ("I", "Италия"), ("F", "Франция")]


def _pairs(values):
    """[v, ...] -> [(value, label), ...]"""
    return [(str(v), str(v)) for v in values]


def _brand_pairs(draft, bot_data):
    return _pairs(POPULAR_BRANDS)


def _model_pairs(draft, bot_data):
    # models belong to one make, so this step is only asked for a single brand
    return _pairs(_model_options(draft, bot_data))


# Multi steps store a list under `key`; single steps store one value.
# options: list of (value, label) or f(draft, bot_data) -> same.
STEPS = [
    {"key": "brands", "q": "Марки? Отметь нужные и жми «Готово»",
     "multi": True, "required": True, "options": _brand_pairs, "parse": _opt_text},
    {"key": "models", "q": "Модели? Можно несколько",
     "multi": True, "options": _model_pairs, "parse": _opt_text,
     "skip_if": lambda draft: len(draft.get("brands") or []) != 1},
    {"key": "year_from", "q": "Год от?", "parse": _opt_int,
     "options": _pairs([2015, 2016, 2017, 2018, 2019, 2020, 2021, 2022, 2023])},
    {"key": "price_max", "q": "Максимальная цена, €?", "parse": _opt_int,
     "options": _pairs([5000, 7500, 10000, 15000, 20000, 25000, 30000, 40000, 50000])},
    {"key": "mileage_max", "q": "Максимальный пробег, км?", "parse": _opt_int,
     "options": _pairs([50000, 100000, 150000, 200000, 250000])},
    {"key": "fuels", "q": "Топливо? Можно несколько", "multi": True, "parse": _choice(FUEL_CHOICE),
     "options": [("diesel", "дизель"), ("petrol", "бензин"),
                 ("hybrid", "гибрид"), ("electric", "электро")]},
    {"key": "gearboxes", "q": "Коробка? Можно обе", "multi": True,
     "parse": _choice(GEARBOX_CHOICE),
     "options": [("automatic", "автомат"), ("manual", "механика")]},
    {"key": "countries", "q": "Страны? Отметь нужные и жми «Готово»",
     "multi": True, "parse": _countries, "options": COUNTRIES},
]
STEP_INDEX = {step["key"]: i for i, step in enumerate(STEPS)}


# --- rendering ---------------------------------------------------------------

def describe(profile: dict) -> str:
    brands = profile.get("brands") or ([profile["brand"]] if profile.get("brand") else [])
    models = profile.get("models") or ([profile["model"]] if profile.get("model") else [])
    head = " / ".join(html.escape(b) for b in brands)
    if models:
        head += " " + " / ".join(html.escape(m) for m in models)
    bits = [f"<b>{head}</b>"]
    if profile.get("year_from"):
        bits.append(f"{profile['year_from']}+")
    if profile.get("price_max"):
        bits.append(f"≤{profile['price_max']:,}€".replace(",", " "))
    if profile.get("mileage_max"):
        bits.append(f"≤{profile['mileage_max']:,} km".replace(",", " "))
    for key in ("fuels", "gearboxes"):
        values = profile.get(key) or []
        if values:
            bits.append("/".join(VALUE_LABEL.get(v, v) for v in values))
    bits.append("/".join(profile.get("countries") or ["D"]))
    if profile.get("exclude_damaged", 1):
        bits.append("без аварий")
    flags = []
    if profile.get("paused"):
        flags.append("⏸ на паузе")
    if profile.get("muted"):
        flags.append("\U0001f515 без уведомлений")
    return " · ".join(bits) + ("  " + " ".join(flags) if flags else "")


def search_keyboard(profile: dict) -> InlineKeyboardMarkup:
    sid = profile["id"]
    toggle = ("▶️ Возобновить", f"resume:{sid}") if profile["paused"] else \
             ("⏸ Пауза", f"pause:{sid}")
    return InlineKeyboardMarkup([[
        InlineKeyboardButton(*toggle),
        InlineKeyboardButton("\U0001f5d1 Удалить", callback_data=f"del:{sid}"),
    ]])


# --- persistent keyboard under the input field -------------------------------

BTN_ADD = "\u2795 Новый поиск"
BTN_LIST = "\U0001f4cb Мои поиски"
BTN_FAV = "❤️ Избранное"
BTN_RUN = "\U0001f50d Искать сейчас"
BTN_STATUS = "\U0001f4ca Статус"
BTN_DEALERS = "\U0001f6ab Скрытые"
BTN_HELP = "❓ Помощь"
BTN_SWIPE = "\U0001f525 Свайпать"
BTN_CANCEL = "✖️ Отмена"
BTN_SKIP = "— пропустить"


def webapp_url(db=None, chat_id=None):
    """Base url of the swipe Mini App, with this chat's feed token attached."""
    base = (os.environ.get("WEBAPP_URL") or "").strip().rstrip("/")
    if not base or db is None or chat_id is None:
        return None
    return f"{base}/?t={db.chat_token(chat_id)}"


def main_keyboard(db=None, chat_id=None) -> ReplyKeyboardMarkup:
    url = webapp_url(db, chat_id)
    # sendData only works for a web app opened from a reply-keyboard button,
    # which is exactly what this is - an inline button could not report back
    swipe_row = [[KeyboardButton(BTN_SWIPE, web_app=WebAppInfo(url))]] if url else []
    return ReplyKeyboardMarkup(
        swipe_row +
        [[BTN_ADD, BTN_LIST],
         [BTN_RUN, BTN_FAV],
         [BTN_STATUS, BTN_DEALERS, BTN_HELP]],
        resize_keyboard=True, is_persistent=True)


REQUIRED_STEPS = {"brand"}          # everything else may be skipped


def _chunk(items, per_row):
    return [items[i:i + per_row] for i in range(0, len(items), per_row)]


def dialog_keyboard(step: str = "", options=None) -> ReplyKeyboardMarkup:
    """Answers are buttons; typing still works for anything not on the list."""
    per_row = 3 if all(len(str(o)) <= 12 for o in (options or [])) else 2
    rows = _chunk([str(o) for o in (options or [])], per_row)
    if step not in REQUIRED_STEPS:
        rows.append([BTN_SKIP])
    rows.append([BTN_CANCEL])
    return ReplyKeyboardMarkup(rows, resize_keyboard=True, is_persistent=True,
                               one_time_keyboard=False)


def step_options(index: int, draft: dict, bot_data: dict) -> list:
    """-> [(value, label), ...]"""
    options = STEPS[index].get("options")
    if callable(options):
        options = options(draft, bot_data)
    return list(options or [])


def multi_keyboard(index: int, options: list, chosen) -> InlineKeyboardMarkup:
    """Every tap toggles one option; «Готово» closes the step.

    Callback data carries the option's position, not its value: a make like
    "Mercedes-AMG GT 4-Door Coupé" would blow the 64-byte callback limit.
    """
    picked = set(chosen or [])
    buttons = [InlineKeyboardButton(("✅ " if value in picked else "▫️ ") + label,
                                    callback_data=f"ms:{index}:{i}")
               for i, (value, label) in enumerate(options)]
    per_row = 2 if any(len(l) > 11 for _, l in options) else 3
    rows = _chunk(buttons, per_row)
    done = f"Готово ({len(picked)})" if picked else "Готово — не важно"
    if STEPS[index].get("required") and not picked:
        done = "Выбери хотя бы одну"
    rows.append([InlineKeyboardButton(done, callback_data=f"ms:{index}:done")])
    return InlineKeyboardMarkup(rows)


def listing_keyboard(listing_id: str, dealer_key=None, search_id=None) -> InlineKeyboardMarkup:
    row = [InlineKeyboardButton("❤️ В избранное", callback_data=f"fav:{listing_id}")]
    if dealer_key:
        row.append(InlineKeyboardButton("\U0001f6ab Скрыть продавца", callback_data=f"blk:{dealer_key}"))
    if search_id:
        row.append(InlineKeyboardButton("\U0001f515 Не уведомлять", callback_data=f"mute:{search_id}"))
    return InlineKeyboardMarkup([row])


# --- command handlers --------------------------------------------------------

def _db(context) -> Db:
    return context.application.bot_data["db"]


async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat = update.effective_chat
    _db(context).add_subscriber(chat.id, update.effective_user.username if update.effective_user else None)
    await update.message.reply_text(
        f"Чат подключён (<code>{chat.id}</code>).\n\n{HELP}",
        parse_mode=ParseMode.HTML, reply_markup=main_keyboard(_db(context), chat.id))


async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        HELP, parse_mode=ParseMode.HTML,
        reply_markup=main_keyboard(_db(context), update.effective_chat.id))


async def ask_step(message, context, db, chat_id, index: int, draft: dict):
    """Ask the step at `index`, skipping the ones that do not apply."""
    while index < len(STEPS) and STEPS[index].get("skip_if", lambda d: False)(draft):
        index += 1
    if index >= len(STEPS):
        await finish_dialog(message, db, chat_id, draft)
        return

    step = STEPS[index]
    db.set_dialog(chat_id, step["key"], draft)
    prefix = f"({index + 1}/{len(STEPS)}) "
    options = step_options(index, draft, context.application.bot_data)

    if step.get("multi"):
        hint = "" if options else "\n\nСписок пуст — напиши значения текстом."
        await message.reply_text(prefix + step["q"] + hint,
                                 reply_markup=multi_keyboard(index, options,
                                                             draft.get(step["key"])))
        return
    hint = "\n\nНет в списке — просто напиши." if not options else ""
    await message.reply_text(prefix + step["q"] + hint, parse_mode=ParseMode.HTML,
                             reply_markup=dialog_keyboard(step["key"],
                                                          [l for _, l in options]))


async def cmd_add(update: Update, context: ContextTypes.DEFAULT_TYPE):
    db = _db(context)
    chat_id = update.effective_chat.id
    db.add_subscriber(chat_id)
    await ask_step(update.message, context, db, chat_id, 0, {})


async def cmd_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    _db(context).clear_dialog(update.effective_chat.id)
    await update.message.reply_text(
        "Отменено.", reply_markup=main_keyboard(_db(context), update.effective_chat.id))


async def on_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Keyboard taps first, then the /add dialog, then a nudge to /help."""
    text = (update.message.text or "").strip()
    handler = BUTTONS.get(text)
    if handler:
        # a tap must win over an open dialog, otherwise "Отмена" becomes an answer
        context.args = []
        await handler(update, context)
        return

    db = _db(context)
    chat_id = update.effective_chat.id
    step, draft = db.get_dialog(chat_id)
    if not step:
        await update.message.reply_text(
            "Не на что отвечать — жми кнопку снизу или /help.",
            reply_markup=main_keyboard(db, chat_id))
        return

    index = STEP_INDEX[step]
    spec = STEPS[index]
    options = step_options(index, draft, context.application.bot_data)

    # a label typed by hand (or tapped on the reply keyboard) counts as its value
    by_label = {str(label).lower(): value for value, label in options}
    typed = text.lower()
    if typed in by_label:
        value = by_label[typed]
    else:
        try:
            value = spec["parse"](text)
        except ValueError as exc:
            await update.message.reply_text(
                f"{exc}. Попробуй ещё раз.",
                reply_markup=dialog_keyboard(step, [l for _, l in options]))
            return

    if spec.get("multi"):
        await _add_typed_to_multi(update, context, db, chat_id, index, draft, value)
        return

    draft[spec["key"]] = value
    await ask_step(update.message, context, db, chat_id, index + 1, draft)


async def _add_typed_to_multi(update, context, db, chat_id, index, draft, value):
    """Typing during a multi step appends to the selection and redraws it."""
    spec = STEPS[index]
    values = [value] if not isinstance(value, list) else list(value)
    if spec["key"] == "brands":
        brands = context.application.bot_data.get("brands") or {}
        resolved = [_resolve_brand(brands, v) for v in values]
        unknown = [v for v, r in zip(values, resolved) if not r]
        if unknown:
            await update.message.reply_text(
                "Не нашёл в справочнике: " + html.escape(", ".join(map(str, unknown))),
                parse_mode=ParseMode.HTML)
            return
        values = resolved

    chosen = list(draft.get(spec["key"]) or [])
    for v in values:
        if v and v not in chosen:
            chosen.append(v)
    draft[spec["key"]] = chosen
    db.set_dialog(chat_id, spec["key"], draft)
    options = step_options(index, draft, context.application.bot_data)
    # typed values may be outside the option list - show them so they can be removed
    known = {v for v, _ in options}
    options = options + [(v, v) for v in chosen if v not in known]
    await update.message.reply_text(
        f"({index + 1}/{len(STEPS)}) {spec['q']}",
        reply_markup=multi_keyboard(index, options, chosen))


async def finish_dialog(message, db, chat_id, draft: dict):
    db.clear_dialog(chat_id)
    draft["name"] = _unique_name(db, chat_id, draft)
    search_id = db.add_search(chat_id, draft)
    profile = db.get_search(search_id, chat_id)
    await message.reply_text(f"Сохранил как #{search_id}: {describe(profile)}",
                             parse_mode=ParseMode.HTML,
                             reply_markup=search_keyboard(profile))
    await message.reply_text("Готово. Жми «Искать сейчас», чтобы не ждать полчаса.",
                             reply_markup=main_keyboard(db, chat_id))


# the UI is Russian, so people type Cyrillic and shorthands
BRAND_ALIASES = {
    "вw": "Volkswagen", "vw": "Volkswagen", "фольксваген": "Volkswagen", "фолькс": "Volkswagen",
    "бмв": "BMW", "бэха": "BMW",
    "мерседес": "Mercedes-Benz", "мерс": "Mercedes-Benz", "мерин": "Mercedes-Benz",
    "ауди": "Audi", "шкода": "Skoda", "škoda": "Skoda",
    "тойота": "Toyota", "ниссан": "Nissan", "хонда": "Honda", "мазда": "Mazda",
    "хендай": "Hyundai", "хёндай": "Hyundai", "хундай": "Hyundai", "киа": "Kia",
    "рено": "Renault", "пежо": "Peugeot", "ситроен": "Citroen", "ситроён": "Citroen",
    "опель": "Opel", "форд": "Ford", "фиат": "Fiat", "вольво": "Volvo",
    "порше": "Porsche", "порш": "Porsche", "мини": "MINI", "сеат": "SEAT",
    "субару": "Subaru", "митсубиси": "Mitsubishi", "мицубиси": "Mitsubishi",
    "лексус": "Lexus", "ягуар": "Jaguar", "тесла": "Tesla", "дачия": "Dacia",
    "сузуки": "Suzuki", "ленд ровер": "Land Rover", "лендровер": "Land Rover",
}


def _resolve_brand(brands: dict, name):
    """"skoda" / "Škoda" / "ŠKODA" all have to reach the "Skoda" key."""
    if not name:
        return None
    alias = BRAND_ALIASES.get(str(name).strip().lower())
    if alias and alias in brands:
        return alias
    wanted = normalize(name)
    for brand in brands:
        if normalize(brand) == wanted:
            return brand
    # last chance: unique prefix, so "mercedes" finds "Mercedes-Benz"
    hits = [b for b in brands if normalize(b).startswith(wanted)] if len(wanted) >= 3 else []
    return hits[0] if len(hits) == 1 else None


def _unique_name(db: Db, chat_id, draft: dict) -> str:
    brands = draft.get("brands") or []
    models = draft.get("models") or []
    parts = ["+".join(brands[:2]).lower().replace(" ", "-")]
    if models:
        parts.append("+".join(models[:2]).lower().replace(" ", "-"))
    if len(brands) > 2 or len(models) > 2:
        parts.append("more")
    base = "-".join(filter(None, parts)) or "search"
    name, n = base, 2
    while db.search_name_taken(chat_id, name):
        name, n = f"{base}-{n}", n + 1
    return name


async def cmd_list(update: Update, context: ContextTypes.DEFAULT_TYPE):
    db = _db(context)
    searches = db.list_searches(update.effective_chat.id)
    if not searches:
        await update.message.reply_text("Поисков пока нет. /add — создать.")
        return
    for profile in searches:
        await update.message.reply_text(f"#{profile['id']}  {describe(profile)}",
                                        parse_mode=ParseMode.HTML,
                                        reply_markup=search_keyboard(profile))


async def _by_id(update, context, action):
    db = _db(context)
    chat_id = update.effective_chat.id
    if not context.args or not context.args[0].lstrip("#").isdigit():
        await update.message.reply_text("Как пользоваться: /del 3 (номер берётся из /list)")
        return
    search_id = int(context.args[0].lstrip("#"))
    ok, text = action(db, search_id, chat_id)
    await update.message.reply_text(text if ok else f"Поиска #{search_id} у тебя нет.")


async def cmd_del(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await _by_id(update, context, lambda db, sid, cid:
                 (db.delete_search(sid, cid), f"Удалил #{sid}."))


async def cmd_pause(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await _by_id(update, context, lambda db, sid, cid:
                 (db.set_search_flag(sid, cid, "paused", 1), f"Поставил #{sid} на паузу."))


async def cmd_resume(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await _by_id(update, context, lambda db, sid, cid:
                 (db.set_search_flag(sid, cid, "paused", 0), f"Возобновил #{sid}."))


async def cmd_fav(update: Update, context: ContextTypes.DEFAULT_TYPE):
    rows = _db(context).list_favorites(update.effective_chat.id)
    if not rows:
        await update.message.reply_text("Избранного пока нет — жми ❤️ под объявлением.")
        return
    lines = [f"• {r['brand']} {r['model']} · {r['year'] or '?'} · "
             f"{(r['price_eur'] or 0):,} €\n{r['url']}".replace(",", " ") for r in rows]
    await update.message.reply_text("\n\n".join(lines), disable_web_page_preview=True)


async def cmd_dealers(update: Update, context: ContextTypes.DEFAULT_TYPE):
    db = _db(context)
    chat_id = update.effective_chat.id
    if context.args:                       # /dealers unblock <key>
        key = context.args[-1]
        removed = db.unblock_dealer(chat_id, key)
        await update.message.reply_text(f"Снял скрытие с {key}." if removed else f"{key} и так не скрыт.")
        return
    blocked = db.blocked_dealers(chat_id)
    if not blocked:
        await update.message.reply_text("Скрытых продавцов нет.")
        return
    lines = [f"• <code>{html.escape(k)}</code> {html.escape(v or '')}" for k, v in blocked.items()]
    await update.message.reply_text(
        "Скрытые продавцы:\n" + "\n".join(lines) + "\n\nВернуть: /dealers &lt;ключ&gt;",
        parse_mode=ParseMode.HTML)


async def cmd_status(update: Update, context: ContextTypes.DEFAULT_TYPE):
    s = _db(context).stats()
    await update.message.reply_text(
        f"объявлений в базе: {s['listings']}\nпоисков: {s['searches']}\n"
        f"в очереди на отправку: {s['queued']}\nв избранном: {s['favorites']}\n"
        f"последний прогон: {s['last_run']}")


async def cmd_run(update: Update, context: ContextTypes.DEFAULT_TYPE):
    runner = context.application.bot_data.get("run_scrape")
    if runner is None:
        await update.message.reply_text("Этот процесс не умеет скрейпить — заберёт плановый прогон.")
        return
    await update.message.reply_text("Пошёл искать, это займёт минуту…")
    try:
        summary = await runner()
    except Exception as exc:
        log.exception("manual run failed")
        await update.message.reply_text(f"Прогон упал: {type(exc).__name__}: {exc}")
        return
    await update.message.reply_text(summary or "Готово.")


async def on_webapp_data(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Verdicts sent back by the swipe Mini App: likes become favorites."""
    db = _db(context)
    chat_id = update.effective_chat.id
    try:
        payload = json.loads(update.message.web_app_data.data)
        swipes = payload["swipes"]
    except (AttributeError, KeyError, TypeError, ValueError) as exc:
        log.warning("bad web_app_data from %s: %s", chat_id, exc)
        await update.message.reply_text("Не разобрал ответ приложения, свайпы не сохранились.")
        return

    likes = 0
    for swipe in swipes:
        listing_id, verdict = swipe.get("id"), swipe.get("verdict")
        if not listing_id or verdict not in ("like", "pass"):
            continue
        db.record_swipe(chat_id, listing_id, verdict)
        if verdict == "like":
            # a second like must not toggle an existing favorite back off
            if listing_id not in {r["id"] for r in db.list_favorites(chat_id, limit=10000)}:
                db.toggle_favorite(chat_id, listing_id)
            likes += 1
    db.commit()
    await update.message.reply_text(
        f"Записал {len(swipes)} свайпов, из них ❤️ {likes}. "
        f"Понравившиеся — в «Избранное»." if swipes else "Свайпов не было.",
        reply_markup=main_keyboard(db, chat_id))


async def _on_multi_tap(query, context, db, chat_id, payload: str):
    """payload is "<step index>:<option index>" or "<step index>:done"."""
    step_key, draft = db.get_dialog(chat_id)
    try:
        index_text, choice = payload.split(":", 1)
        index = int(index_text)
    except ValueError:
        await query.answer()
        return
    if step_key != STEPS[index]["key"]:
        await query.answer("Этот шаг уже пройден")
        return

    spec = STEPS[index]
    chosen = list(draft.get(spec["key"]) or [])
    options = step_options(index, draft, context.application.bot_data)
    known = {v for v, _ in options}
    options = options + [(v, v) for v in chosen if v not in known]

    if choice == "done":
        if spec.get("required") and not chosen:
            await query.answer("Нужно выбрать хотя бы одну", show_alert=True)
            return
        draft[spec["key"]] = chosen
        labels = dict(options)
        await query.answer()
        await query.edit_message_text(
            f"{spec['q'].split('?')[0]}: " +
            (", ".join(labels.get(c, c) for c in chosen) if chosen else "не важно"))
        await ask_step(query.message, context, db, chat_id, index + 1, draft)
        return

    try:
        value = options[int(choice)][0]
    except (ValueError, IndexError):
        await query.answer()
        return
    if value in chosen:
        chosen.remove(value)
    else:
        chosen.append(value)
    draft[spec["key"]] = chosen
    db.set_dialog(chat_id, spec["key"], draft)
    await query.answer()
    await query.edit_message_reply_markup(multi_keyboard(index, options, chosen))


# --- callbacks ---------------------------------------------------------------

async def on_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    db = _db(context)
    chat_id = update.effective_chat.id
    action, _, payload = (query.data or "").partition(":")

    if action == "ms":
        await _on_multi_tap(query, context, db, chat_id, payload)
    elif action == "fav":
        added = db.toggle_favorite(chat_id, payload)
        await query.answer("Добавил в избранное" if added else "Убрал из избранного")
    elif action == "blk":
        db.block_dealer(chat_id, payload)
        await query.answer("Продавец скрыт — больше не покажу")
    elif action == "mute":
        db.set_search_flag(int(payload), chat_id, "muted", 1)
        await query.answer("Поиск замьючен")
    elif action in ("pause", "resume", "del"):
        sid = int(payload)
        if action == "del":
            db.delete_search(sid, chat_id)
            await query.answer("Удалено")
            await query.edit_message_text(f"#{sid} удалён.")
            return
        db.set_search_flag(sid, chat_id, "paused", 1 if action == "pause" else 0)
        profile = db.get_search(sid, chat_id)
        await query.answer("На паузе" if action == "pause" else "Возобновлён")
        if profile:
            await query.edit_message_text(f"#{sid}  {describe(profile)}",
                                          parse_mode=ParseMode.HTML,
                                          reply_markup=search_keyboard(profile))
    else:
        await query.answer()


# --- wiring ------------------------------------------------------------------

def build_application(token: str, db: Db, brands: dict, run_scrape=None,
                      post_init=None) -> Application:
    builder = Application.builder().token(token)
    if post_init:
        builder = builder.post_init(post_init)
    app = builder.build()
    app.bot_data.update(db=db, brands=brands, run_scrape=run_scrape)
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("help", cmd_help))
    app.add_handler(CommandHandler("add", cmd_add))
    app.add_handler(CommandHandler("cancel", cmd_cancel))
    app.add_handler(CommandHandler("list", cmd_list))
    app.add_handler(CommandHandler("del", cmd_del))
    app.add_handler(CommandHandler("pause", cmd_pause))
    app.add_handler(CommandHandler("resume", cmd_resume))
    app.add_handler(CommandHandler("fav", cmd_fav))
    app.add_handler(CommandHandler("dealers", cmd_dealers))
    app.add_handler(CommandHandler("status", cmd_status))
    app.add_handler(CommandHandler("run", cmd_run))
    app.add_handler(MessageHandler(filters.StatusUpdate.WEB_APP_DATA, on_webapp_data))
    app.add_handler(CallbackQueryHandler(on_callback))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_text))
    return app


# keyboard label -> handler; defined here because the handlers exist by now
BUTTONS = {
    BTN_ADD: cmd_add,
    BTN_LIST: cmd_list,
    BTN_FAV: cmd_fav,
    BTN_RUN: cmd_run,
    BTN_STATUS: cmd_status,
    BTN_DEALERS: cmd_dealers,
    BTN_HELP: cmd_help,
    BTN_SWIPE: cmd_help,          # only reachable if the web app failed to open
    BTN_CANCEL: cmd_cancel,
}

BOT_COMMANDS = [
    ("add", "новый поиск"), ("list", "мои поиски"), ("del", "удалить поиск"),
    ("pause", "приостановить"), ("resume", "возобновить"), ("fav", "избранное"),
    ("dealers", "скрытые продавцы"), ("run", "искать сейчас"), ("status", "счётчики"),
    ("cancel", "отменить диалог"), ("help", "помощь"),
]

VALUE_LABEL = {"diesel": "дизель", "petrol": "бензин", "hybrid": "гибрид",
               "electric": "электро", "automatic": "автомат", "manual": "механика"}
