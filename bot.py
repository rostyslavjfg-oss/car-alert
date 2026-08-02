#!/usr/bin/env python3
"""Long-polling Telegram bot - run it wherever you want instant replies.

    export TELEGRAM_BOT_TOKEN=...
    python bot.py

The scraper itself keeps running on the GitHub Actions cron; this process only
handles commands, buttons and /run. Do not run it at the same time as
`main.py --drain` - Telegram allows a single getUpdates consumer and answers the
second one with HTTP 409.
"""

import asyncio
import logging
import os
import sys

from telegram import BotCommand

from core.db import Db, load_env
from core.telegram_app import BOT_COMMANDS, build_application
from fetch_mobile_makes import load_brands
from main import run as run_scrape

log = logging.getLogger("car-alert.bot")


def make_runner(db: Db):
    """/run handler: scrape in a worker thread so polling keeps answering."""
    lock = asyncio.Lock()

    async def runner() -> str:
        if lock.locked():
            return "A scrape is already running."
        async with lock:
            return await asyncio.to_thread(run_scrape, False, False, 3, db)

    return runner


async def post_init(app):
    await app.bot.set_my_commands([BotCommand(c, d) for c, d in BOT_COMMANDS])
    me = await app.bot.get_me()
    log.info("bot @%s is up", me.username)


def main():
    logging.basicConfig(level=logging.INFO, stream=sys.stdout,
                        format="%(asctime)s %(levelname)s %(name)s %(message)s",
                        datefmt="%H:%M:%S")
    load_env()
    token = os.environ.get("TELEGRAM_BOT_TOKEN")
    if not token:
        sys.exit("TELEGRAM_BOT_TOKEN is not set")

    # the /run handler scrapes in a worker thread, so the connection must be shareable
    db = Db(check_same_thread=False)
    app = build_application(token, db, load_brands(),
                            run_scrape=make_runner(db), post_init=post_init)
    try:
        app.run_polling(drop_pending_updates=False)
    finally:
        db.commit()
        db.close()


if __name__ == "__main__":
    main()
