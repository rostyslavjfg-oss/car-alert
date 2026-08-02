"""Telegram bot: command handlers and inline-button callbacks.

Dialog state lives in SQLite rather than in process memory, so the exact same
handlers work in two modes:

  * bot.py            - long polling, answers instantly
  * main.py --drain   - one shot inside the GitHub Actions run (<=30 min lag)

Never poll from both at once - Telegram answers the second one with 409.
"""

import html
import logging
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.constants import ParseMode
from telegram.ext import (Application, CallbackQueryHandler, CommandHandler,
                          ContextTypes, MessageHandler, filters)

from fetch_mobile_makes import normalize

from .db import Db

log = logging.getLogger(__name__)

HELP = """<b>Автоподбор</b> — слежу за объявлениями на mobile.de, autoscout24, bazos.sk и willhaben.at

/add — новый поиск (по шагам)
/list — мои поиски
/del &lt;id&gt; — удалить поиск
/pause &lt;id&gt; — приостановить
/resume &lt;id&gt; — возобновить
/fav — избранное
/dealers — скрытые продавцы
/run — прогнать поиск прямо сейчас
/status — счётчики
/cancel — отменить текущий диалог

Новые объявления приходят сами, каждые 30 минут."""

# /add dialog: step -> (question, draft key, parser)
SKIP_WORDS = {"-", "любой", "любая", "неважно", "пропустить", "any", "skip", "no"}


def _opt_int(text: str):
    text = text.strip().replace(" ", "").replace(".", "")
    if text.lower() in SKIP_WORDS:
        return None
    if not text.isdigit():
        raise ValueError("нужно число или «-»")
    return int(text)


def _opt_text(text: str):
    return None if text.strip().lower() in SKIP_WORDS else text.strip()


def _choice(options: dict):
    """options maps every accepted spelling (ru and en) to the canonical value."""
    def parse(text: str):
        value = text.strip().lower()
        if value in SKIP_WORDS:
            return None
        if value not in options:
            raise ValueError("выбери одно из: " + ", ".join(dict.fromkeys(options)) + ", либо «-»")
        return options[value]
    return parse


def _countries(text: str):
    value = text.strip().upper()
    if value.lower() in SKIP_WORDS:
        return ["D"]
    codes = [c.strip() for c in value.replace(";", ",").split(",") if c.strip()]
    return codes or ["D"]


FUEL_CHOICE = {"diesel": "diesel", "дизель": "diesel", "бензин": "petrol", "petrol": "petrol",
               "гибрид": "hybrid", "hybrid": "hybrid", "электро": "electric", "electric": "electric"}
GEARBOX_CHOICE = {"автомат": "automatic", "automatic": "automatic",
                  "механика": "manual", "manual": "manual", "ручная": "manual"}

STEPS = [
    ("brand", "Марка? (как на сайте, например <code>BMW</code>)", _opt_text),
    ("model", "Модель? (например <code>320</code>, или <code>-</code> — любая)", _opt_text),
    ("year_from", "Год от? (например <code>2018</code>, или <code>-</code>)", _opt_int),
    ("price_max", "Максимальная цена в €? (например <code>20000</code>, или <code>-</code>)", _opt_int),
    ("mileage_max", "Максимальный пробег в км? (например <code>150000</code>, или <code>-</code>)", _opt_int),
    ("fuel", "Топливо? <code>дизель / бензин / гибрид / электро</code> или <code>-</code>",
     _choice(FUEL_CHOICE)),
    ("gearbox", "Коробка? <code>автомат / механика</code> или <code>-</code>",
     _choice(GEARBOX_CHOICE)),
    ("countries", "Страны? <code>D</code>—Германия, <code>A</code>—Австрия, <code>SK</code>—Словакия.\n"
                  "Можно списком: <code>D,A,SK</code> (или <code>-</code> — только Германия)",
     _countries),
]
STEP_INDEX = {name: i for i, (name, _, _) in enumerate(STEPS)}


# --- rendering ---------------------------------------------------------------

def describe(profile: dict) -> str:
    bits = [f"<b>{html.escape(profile['brand'])}"
            + (f" {html.escape(profile['model'])}" if profile.get("model") else "") + "</b>"]
    if profile.get("year_from"):
        bits.append(f"{profile['year_from']}+")
    if profile.get("price_max"):
        bits.append(f"≤{profile['price_max']:,}€".replace(",", " "))
    if profile.get("mileage_max"):
        bits.append(f"≤{profile['mileage_max']:,} km".replace(",", " "))
    for key in ("fuel", "gearbox"):
        if profile.get(key):
            bits.append(VALUE_LABEL.get(profile[key], profile[key]))
    bits.append("/".join(profile.get("countries") or ["D"]))
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
        f"Чат подключён (<code>{chat.id}</code>).\n\n{HELP}", parse_mode=ParseMode.HTML)


async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(HELP, parse_mode=ParseMode.HTML)


async def cmd_add(update: Update, context: ContextTypes.DEFAULT_TYPE):
    db = _db(context)
    chat_id = update.effective_chat.id
    db.add_subscriber(chat_id)
    db.set_dialog(chat_id, STEPS[0][0], {})
    await update.message.reply_text(
        f"Новый поиск (1/{len(STEPS)}). {STEPS[0][1]}\n\n/cancel — отменить.",
        parse_mode=ParseMode.HTML)


async def cmd_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    _db(context).clear_dialog(update.effective_chat.id)
    await update.message.reply_text("Отменено.")


async def on_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Only meaningful while an /add dialog is open."""
    db = _db(context)
    chat_id = update.effective_chat.id
    step, draft = db.get_dialog(chat_id)
    if not step:
        await update.message.reply_text("Не на что отвечать. /add — создать поиск, /help — команды.")
        return

    index = STEP_INDEX[step]
    key, _, parse = STEPS[index]
    try:
        value = parse(update.message.text or "")
    except ValueError as exc:
        await update.message.reply_text(f"{exc}. Попробуй ещё раз.")
        return

    if key == "brand":
        brands = context.application.bot_data.get("brands") or {}
        match = _resolve_brand(brands, value)
        if not match:
            await update.message.reply_text(
                f"Марки «{html.escape(str(value))}» нет в справочнике. "
                "Проверь написание и пришли ещё раз.", parse_mode=ParseMode.HTML)
            return
        value = match
    draft[key] = value

    if index + 1 < len(STEPS):
        next_key, question, _ = STEPS[index + 1]
        db.set_dialog(chat_id, next_key, draft)
        await update.message.reply_text(f"({index + 2}/{len(STEPS)}) {question}",
                                        parse_mode=ParseMode.HTML)
        return

    db.clear_dialog(chat_id)
    draft["name"] = _unique_name(db, chat_id, draft)
    search_id = db.add_search(chat_id, draft)
    profile = db.get_search(search_id, chat_id)
    await update.message.reply_text(
        f"Сохранил как #{search_id}: {describe(profile)}\n\n/run — прогнать прямо сейчас.",
        parse_mode=ParseMode.HTML, reply_markup=search_keyboard(profile))


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
    base = "-".join(filter(None, [str(draft.get("brand", "")).lower().replace(" ", "-"),
                                  str(draft.get("model") or "").lower().replace(" ", "-"),
                                  draft.get("fuel") or ""])) or "search"
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


# --- callbacks ---------------------------------------------------------------

async def on_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    db = _db(context)
    chat_id = update.effective_chat.id
    action, _, payload = (query.data or "").partition(":")

    if action == "fav":
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
    app.add_handler(CallbackQueryHandler(on_callback))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_text))
    return app


BOT_COMMANDS = [
    ("add", "новый поиск"), ("list", "мои поиски"), ("del", "удалить поиск"),
    ("pause", "приостановить"), ("resume", "возобновить"), ("fav", "избранное"),
    ("dealers", "скрытые продавцы"), ("run", "искать сейчас"), ("status", "счётчики"),
    ("cancel", "отменить диалог"), ("help", "помощь"),
]

VALUE_LABEL = {"diesel": "дизель", "petrol": "бензин", "hybrid": "гибрид",
               "electric": "электро", "automatic": "автомат", "manual": "механика"}
