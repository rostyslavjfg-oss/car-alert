"""Telegram notifications - one message per listing, photo + caption + link."""

import asyncio
import logging
import os

from telegram import Bot
from telegram.error import TelegramError

log = logging.getLogger(__name__)

MAX_PER_RUN = 20
SEND_DELAY = 1.2          # stay under Telegram's ~30 msg/s and per-chat limits

FUEL_LABEL = {"diesel": "Diesel", "petrol": "Petrol", "hybrid": "Hybrid", "electric": "Electric"}
GEARBOX_LABEL = {"manual": "Manual", "automatic": "Automatic"}


def _fmt(n, suffix=""):
    return f"{n:,}".replace(",", " ") + suffix if isinstance(n, int) else "?"


def build_caption(row: dict, old_price=None) -> str:
    head = " - ".join([
        f"{row.get('brand') or ''} {row.get('model') or ''}".strip() or "Car",
        str(row.get("year") or "?"),
        _fmt(row.get("mileage_km"), " km"),
        _fmt(row.get("price_eur"), " EUR"),
    ])
    lines = [head]
    if old_price and row.get("price_eur"):
        drop = round((old_price - row["price_eur"]) / old_price * 100)
        lines.insert(0, f"PRICE DROP -{drop}%: {_fmt(old_price, ' EUR')} -> {_fmt(row['price_eur'], ' EUR')}")
    details = [FUEL_LABEL.get(row.get("fuel"), row.get("fuel") or "?"),
               GEARBOX_LABEL.get(row.get("gearbox"), row.get("gearbox") or "?"),
               (row.get("source") or "").replace("mobilede", "mobile.de")]
    lines.append(" | ".join(str(d) for d in details))
    lines.append(row.get("url") or "")
    return "\n".join(lines)


class Notifier:
    """Collects messages, sends up to MAX_PER_RUN, reports what did not fit."""

    def __init__(self, token=None, chat_id=None, max_per_run=MAX_PER_RUN, dry_run=False):
        self.token = token or os.environ.get("TELEGRAM_BOT_TOKEN")
        self.chat_id = chat_id or os.environ.get("TELEGRAM_CHAT_ID")
        self.max_per_run = max_per_run
        self.dry_run = dry_run or not (self.token and self.chat_id)
        if self.dry_run:
            log.warning("notifier: TELEGRAM_BOT_TOKEN/CHAT_ID missing - running in dry-run mode")

    def send_batch(self, items: list) -> tuple:
        """items: list of (row_dict, old_price_or_None).

        Returns (sent, deferred) - both lists of the same tuples.
        """
        to_send, deferred = items[:self.max_per_run], items[self.max_per_run:]
        if not to_send:
            return [], deferred
        if self.dry_run:
            for row, old in to_send:
                log.info("[dry-run] would send:\n%s", build_caption(row, old))
            return to_send, deferred
        sent = asyncio.run(self._send_all(to_send))
        failed = [i for i in to_send if i not in sent]
        return sent, deferred + failed

    async def _send_all(self, items: list) -> list:
        sent = []
        async with Bot(self.token) as bot:
            for row, old_price in items:
                caption = build_caption(row, old_price)
                try:
                    if row.get("image_url"):
                        try:
                            await bot.send_photo(self.chat_id, row["image_url"],
                                                 caption=caption, parse_mode=None)
                        except TelegramError:            # dead/blocked image URL
                            await bot.send_message(self.chat_id, caption,
                                                   disable_web_page_preview=False)
                    else:
                        await bot.send_message(self.chat_id, caption,
                                               disable_web_page_preview=False)
                    sent.append((row, old_price))
                except TelegramError as exc:
                    log.error("telegram send failed for %s: %s", row.get("id"), exc)
                await asyncio.sleep(SEND_DELAY)
        return sent
