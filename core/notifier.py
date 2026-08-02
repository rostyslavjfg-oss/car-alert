"""Telegram notifications - one message per listing, photo + caption + buttons."""

import asyncio
import json
import logging
import os
from dataclasses import dataclass, field
from typing import Optional

from telegram import Bot, InputMediaPhoto
from telegram.error import TelegramError

from .telegram_app import listing_keyboard

log = logging.getLogger(__name__)

MAX_PER_RUN = 20
SEND_DELAY = 1.2          # stay under Telegram's per-chat flood limits
ALBUM_DELAY = 2.5         # albums count as several messages, so pause longer

FUEL_LABEL = {"diesel": "дизель", "petrol": "бензин", "hybrid": "гибрид", "electric": "электро"}
GEARBOX_LABEL = {"manual": "механика", "automatic": "автомат"}
SOURCE_LABEL = {"mobilede": "mobile.de", "autoscout24": "autoscout24",
                "bazos": "bazos.sk", "willhaben": "willhaben.at"}


@dataclass
class Alert:
    row: dict                      # the listing as a plain dict
    chat_id: str
    search_name: str
    search_id: Optional[int] = None
    old_price: Optional[int] = None
    kind: str = "new"              # 'new' | 'price_drop'
    sent: bool = field(default=False, compare=False)

    @property
    def listing_id(self) -> str:
        return self.row["id"]

    @property
    def photos(self) -> list:
        """Listing rows carry a list; rows read back from sqlite carry JSON."""
        raw = self.row.get("images")
        if isinstance(raw, str):
            try:
                raw = json.loads(raw)
            except ValueError:
                raw = []
        photos = [p for p in (raw or []) if p]
        if not photos and self.row.get("image_url"):
            photos = [self.row["image_url"]]
        return photos[:10]                    # Telegram album hard limit


def _fmt(n, suffix=""):
    return f"{n:,}".replace(",", " ") + suffix if isinstance(n, int) else "?"


def build_caption(row: dict, old_price=None) -> str:
    head = " - ".join([
        f"{row.get('brand') or ''} {row.get('model') or ''}".strip() or "Car",
        str(row.get("year") or "?"),
        _fmt(row.get("mileage_km"), " км"),
        _fmt(row.get("price_eur"), " €"),
    ])
    lines = [head]
    if old_price and row.get("price_eur"):
        drop = round((old_price - row["price_eur"]) / old_price * 100)
        lines.insert(0, f"⬇️ ЦЕНА УПАЛА на {drop}%: "
                        f"{_fmt(old_price, ' €')} → {_fmt(row['price_eur'], ' €')}")
    source = row.get("source") or ""
    details = [FUEL_LABEL.get(row.get("fuel"), row.get("fuel") or "?"),
               GEARBOX_LABEL.get(row.get("gearbox"), row.get("gearbox") or "?"),
               SOURCE_LABEL.get(source, source)]
    if row.get("dealer_name"):
        details.append(str(row["dealer_name"])[:40])
    lines.append(" | ".join(str(d) for d in details))
    lines.append(row.get("url") or "")
    return "\n".join(lines)


class Notifier:
    """Sends up to max_per_run alerts and reports which ones did not fit."""

    def __init__(self, token=None, default_chat_id=None, max_per_run=MAX_PER_RUN, dry_run=False):
        self.token = token or os.environ.get("TELEGRAM_BOT_TOKEN")
        self.default_chat_id = default_chat_id or os.environ.get("TELEGRAM_CHAT_ID")
        self.max_per_run = max_per_run
        self.dry_run = dry_run or not self.token
        if self.dry_run:
            log.warning("notifier: dry-run%s", "" if self.token else " (no TELEGRAM_BOT_TOKEN)")

    def send_batch(self, alerts: list) -> tuple:
        """Marks alerts as sent in place. Returns (sent, deferred)."""
        to_send, deferred = alerts[:self.max_per_run], alerts[self.max_per_run:]
        if not to_send:
            return [], deferred
        if self.dry_run:
            for alert in to_send:
                log.info("[dry-run] -> chat %s\n%s", alert.chat_id,
                         build_caption(alert.row, alert.old_price))
                alert.sent = True
            return to_send, deferred
        asyncio.run(self._send_all(to_send))
        sent = [a for a in to_send if a.sent]
        return sent, deferred + [a for a in to_send if not a.sent]

    async def _send_one(self, bot: Bot, chat_id, alert) -> float:
        """Album + a buttons message, or a single photo when there is only one.

        sendMediaGroup rejects reply_markup, so an album cannot carry the
        buttons - they follow in their own short message.
        """
        caption = build_caption(alert.row, alert.old_price)
        dealer_id = alert.row.get("dealer_id")
        keyboard = listing_keyboard(
            alert.listing_id,
            dealer_key=f"{alert.row.get('source')}:{dealer_id}" if dealer_id else None,
            search_id=alert.search_id)
        photos = alert.photos

        if len(photos) > 1:
            try:
                # photos first, then the text that carries the buttons - keeping the
                # caption out of the album puts description and actions in one message
                await bot.send_media_group(chat_id, [InputMediaPhoto(p) for p in photos],
                                           disable_notification=True)
                await bot.send_message(chat_id, caption, reply_markup=keyboard,
                                       disable_web_page_preview=True)
                alert.sent = True
                return ALBUM_DELAY
            except TelegramError as exc:
                log.warning("album failed for %s (%s), falling back to one photo",
                            alert.listing_id, exc)

        if photos:
            try:
                await bot.send_photo(chat_id, photos[0], caption=caption,
                                     reply_markup=keyboard)
                alert.sent = True
                return SEND_DELAY
            except TelegramError as exc:                 # dead or rejected image url
                log.warning("photo failed for %s (%s), sending text",
                            alert.listing_id, exc)
        await bot.send_message(chat_id, caption, reply_markup=keyboard)
        alert.sent = True
        return SEND_DELAY

    async def _send_all(self, alerts: list) -> None:
        async with Bot(self.token) as bot:
            for alert in alerts:
                chat_id = alert.chat_id or self.default_chat_id
                if not chat_id:
                    log.error("no chat id for listing %s", alert.listing_id)
                    continue
                try:
                    delay = await self._send_one(bot, chat_id, alert)
                except TelegramError as exc:
                    log.error("telegram send failed for %s: %s", alert.listing_id, exc)
                    delay = SEND_DELAY
                await asyncio.sleep(delay)
