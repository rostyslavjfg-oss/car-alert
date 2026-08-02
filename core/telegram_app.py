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

from .db import Db

log = logging.getLogger(__name__)

HELP = """<b>car-alert</b> - watcher for mobile.de and autoscout24

/add - new search (step by step)
/list - your searches
/del &lt;id&gt; - delete a search
/pause &lt;id&gt; - stop scraping it
/resume &lt;id&gt; - start again
/fav - favorites
/dealers - hidden dealers
/run - scrape right now
/status - counters
/cancel - abort the current dialog

Alerts arrive automatically every 30 minutes."""

# /add dialog: step -> (question, draft key, parser)
SKIP_WORDS = {"-", "any", "skip", "no", "-"}


def _opt_int(text: str):
    text = text.strip().replace(" ", "").replace(".", "")
    if text.lower() in SKIP_WORDS:
        return None
    if not text.isdigit():
        raise ValueError("expected a number or -")
    return int(text)


def _opt_text(text: str):
    return None if text.strip().lower() in SKIP_WORDS else text.strip()


def _choice(options):
    def parse(text: str):
        value = text.strip().lower()
        if value in SKIP_WORDS:
            return None
        if value not in options:
            raise ValueError("pick one of: " + ", ".join(options) + ", or -")
        return value
    return parse


def _countries(text: str):
    value = text.strip().upper()
    if value.lower() in SKIP_WORDS:
        return ["D"]
    codes = [c.strip() for c in value.replace(";", ",").split(",") if c.strip()]
    return codes or ["D"]


STEPS = [
    ("brand", "Brand? (exactly as on the site, e.g. <code>BMW</code>)", _opt_text),
    ("model", "Model? (e.g. <code>320</code>, or <code>-</code> for any)", _opt_text),
    ("year_from", "Year from? (e.g. <code>2018</code>, or <code>-</code>)", _opt_int),
    ("price_max", "Max price in EUR? (e.g. <code>20000</code>, or <code>-</code>)", _opt_int),
    ("mileage_max", "Max mileage in km? (e.g. <code>150000</code>, or <code>-</code>)", _opt_int),
    ("fuel", "Fuel? <code>diesel/petrol/hybrid/electric</code> or <code>-</code>",
     _choice({"diesel", "petrol", "hybrid", "electric"})),
    ("gearbox", "Gearbox? <code>automatic/manual</code> or <code>-</code>",
     _choice({"automatic", "manual"})),
    ("countries", "Countries? autoscout24 codes, e.g. <code>D,A</code> (or <code>-</code> for D)",
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
            bits.append(profile[key])
    bits.append("/".join(profile.get("countries") or ["D"]))
    flags = []
    if profile.get("paused"):
        flags.append("⏸ paused")
    if profile.get("muted"):
        flags.append("\U0001f515 muted")
    return " · ".join(bits) + ("  " + " ".join(flags) if flags else "")


def search_keyboard(profile: dict) -> InlineKeyboardMarkup:
    sid = profile["id"]
    toggle = ("▶️ Resume", f"resume:{sid}") if profile["paused"] else \
             ("⏸ Pause", f"pause:{sid}")
    return InlineKeyboardMarkup([[
        InlineKeyboardButton(*toggle),
        InlineKeyboardButton("\U0001f5d1 Delete", callback_data=f"del:{sid}"),
    ]])


def listing_keyboard(listing_id: str, dealer_key=None, search_id=None) -> InlineKeyboardMarkup:
    row = [InlineKeyboardButton("❤️ Save", callback_data=f"fav:{listing_id}")]
    if dealer_key:
        row.append(InlineKeyboardButton("\U0001f6ab Hide seller", callback_data=f"blk:{dealer_key}"))
    if search_id:
        row.append(InlineKeyboardButton("\U0001f515 Mute", callback_data=f"mute:{search_id}"))
    return InlineKeyboardMarkup([row])


# --- command handlers --------------------------------------------------------

def _db(context) -> Db:
    return context.application.bot_data["db"]


async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat = update.effective_chat
    _db(context).add_subscriber(chat.id, update.effective_user.username if update.effective_user else None)
    await update.message.reply_text(
        f"Chat registered (<code>{chat.id}</code>).\n\n{HELP}", parse_mode=ParseMode.HTML)


async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(HELP, parse_mode=ParseMode.HTML)


async def cmd_add(update: Update, context: ContextTypes.DEFAULT_TYPE):
    db = _db(context)
    chat_id = update.effective_chat.id
    db.add_subscriber(chat_id)
    db.set_dialog(chat_id, STEPS[0][0], {})
    await update.message.reply_text(
        f"New search (1/{len(STEPS)}). {STEPS[0][1]}\n\n/cancel to abort.",
        parse_mode=ParseMode.HTML)


async def cmd_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    _db(context).clear_dialog(update.effective_chat.id)
    await update.message.reply_text("Cancelled.")


async def on_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Only meaningful while an /add dialog is open."""
    db = _db(context)
    chat_id = update.effective_chat.id
    step, draft = db.get_dialog(chat_id)
    if not step:
        await update.message.reply_text("Nothing to answer. /add to create a search, /help for commands.")
        return

    index = STEP_INDEX[step]
    key, _, parse = STEPS[index]
    try:
        value = parse(update.message.text or "")
    except ValueError as exc:
        await update.message.reply_text(f"{exc}. Try again.")
        return

    if key == "brand":
        brands = context.application.bot_data.get("brands") or {}
        match = _resolve_brand(brands, value)
        if not match:
            await update.message.reply_text(
                f"Brand {html.escape(str(value))} is not in config/brands.json. "
                "Check the spelling and send it again.", parse_mode=ParseMode.HTML)
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
        f"Saved as #{search_id}: {describe(profile)}\n\nRun /run to scrape it now.",
        parse_mode=ParseMode.HTML, reply_markup=search_keyboard(profile))


def _resolve_brand(brands: dict, name):
    if not name:
        return None
    norm = lambda s: "".join(ch for ch in str(s).lower() if ch.isalnum())
    wanted = norm(name)
    for brand in brands:
        if norm(brand) == wanted:
            return brand
    return None


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
        await update.message.reply_text("No searches yet. /add to create one.")
        return
    for profile in searches:
        await update.message.reply_text(f"#{profile['id']}  {describe(profile)}",
                                        parse_mode=ParseMode.HTML,
                                        reply_markup=search_keyboard(profile))


async def _by_id(update, context, action):
    db = _db(context)
    chat_id = update.effective_chat.id
    if not context.args or not context.args[0].lstrip("#").isdigit():
        await update.message.reply_text("Usage: /del 3 (id comes from /list)")
        return
    search_id = int(context.args[0].lstrip("#"))
    ok, text = action(db, search_id, chat_id)
    await update.message.reply_text(text if ok else f"No search #{search_id} of yours.")


async def cmd_del(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await _by_id(update, context, lambda db, sid, cid:
                 (db.delete_search(sid, cid), f"Deleted #{sid}."))


async def cmd_pause(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await _by_id(update, context, lambda db, sid, cid:
                 (db.set_search_flag(sid, cid, "paused", 1), f"Paused #{sid}."))


async def cmd_resume(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await _by_id(update, context, lambda db, sid, cid:
                 (db.set_search_flag(sid, cid, "paused", 0), f"Resumed #{sid}."))


async def cmd_fav(update: Update, context: ContextTypes.DEFAULT_TYPE):
    rows = _db(context).list_favorites(update.effective_chat.id)
    if not rows:
        await update.message.reply_text("No favorites yet - tap ❤️ under an alert.")
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
        await update.message.reply_text(f"Unhidden {key}." if removed else f"{key} was not hidden.")
        return
    blocked = db.blocked_dealers(chat_id)
    if not blocked:
        await update.message.reply_text("No hidden dealers.")
        return
    lines = [f"• <code>{html.escape(k)}</code> {html.escape(v or '')}" for k, v in blocked.items()]
    await update.message.reply_text(
        "Hidden dealers:\n" + "\n".join(lines) + "\n\nUnhide: /dealers &lt;key&gt;",
        parse_mode=ParseMode.HTML)


async def cmd_status(update: Update, context: ContextTypes.DEFAULT_TYPE):
    s = _db(context).stats()
    await update.message.reply_text(
        f"listings: {s['listings']}\nsearches: {s['searches']}\nqueued: {s['queued']}\n"
        f"favorites: {s['favorites']}\nlast run: {s['last_run']}")


async def cmd_run(update: Update, context: ContextTypes.DEFAULT_TYPE):
    runner = context.application.bot_data.get("run_scrape")
    if runner is None:
        await update.message.reply_text("This instance cannot scrape - the cron run will pick it up.")
        return
    await update.message.reply_text("Scraping now, this takes a minute...")
    try:
        summary = await runner()
    except Exception as exc:
        log.exception("manual run failed")
        await update.message.reply_text(f"Run failed: {type(exc).__name__}: {exc}")
        return
    await update.message.reply_text(summary or "Done.")


# --- callbacks ---------------------------------------------------------------

async def on_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    db = _db(context)
    chat_id = update.effective_chat.id
    action, _, payload = (query.data or "").partition(":")

    if action == "fav":
        added = db.toggle_favorite(chat_id, payload)
        await query.answer("Saved to favorites" if added else "Removed from favorites")
    elif action == "blk":
        db.block_dealer(chat_id, payload)
        await query.answer("Dealer hidden from future alerts")
    elif action == "mute":
        db.set_search_flag(int(payload), chat_id, "muted", 1)
        await query.answer("Search muted")
    elif action in ("pause", "resume", "del"):
        sid = int(payload)
        if action == "del":
            db.delete_search(sid, chat_id)
            await query.answer("Deleted")
            await query.edit_message_text(f"#{sid} deleted.")
            return
        db.set_search_flag(sid, chat_id, "paused", 1 if action == "pause" else 0)
        profile = db.get_search(sid, chat_id)
        await query.answer("Paused" if action == "pause" else "Resumed")
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
    ("add", "new search"), ("list", "your searches"), ("del", "delete a search"),
    ("pause", "pause a search"), ("resume", "resume a search"), ("fav", "favorites"),
    ("dealers", "hidden dealers"), ("run", "scrape now"), ("status", "counters"),
    ("cancel", "abort dialog"), ("help", "help"),
]
