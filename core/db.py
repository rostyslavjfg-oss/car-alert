"""SQLite storage. The file is committed back to the repo after every run."""

import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

DB_PATH = Path(__file__).resolve().parent.parent / "listings.db"

SCHEMA = """
CREATE TABLE IF NOT EXISTS listings (
    id          TEXT PRIMARY KEY,          -- "{source}:{listing_id}"
    source      TEXT NOT NULL,
    brand       TEXT,
    model       TEXT,
    year        INTEGER,
    mileage_km  INTEGER,
    price_eur   INTEGER,
    fuel        TEXT,
    gearbox     TEXT,
    url         TEXT,
    image_url   TEXT,
    dealer_id   TEXT,
    dealer_name TEXT,
    first_seen  TEXT NOT NULL,
    last_seen   TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS notifications_sent (
    listing_id  TEXT NOT NULL,
    search_name TEXT NOT NULL,
    kind        TEXT NOT NULL DEFAULT 'new',   -- 'new' | 'price_drop'
    sent_at     TEXT NOT NULL,
    PRIMARY KEY (listing_id, search_name, kind)
);

CREATE TABLE IF NOT EXISTS notification_queue (
    listing_id  TEXT NOT NULL,
    search_name TEXT NOT NULL,
    kind        TEXT NOT NULL DEFAULT 'new',
    old_price   INTEGER,
    chat_id     TEXT,
    search_id   INTEGER,
    queued_at   TEXT NOT NULL,
    PRIMARY KEY (listing_id, search_name, kind)
);

-- everything below is owned by the Telegram bot ------------------------------

CREATE TABLE IF NOT EXISTS searches (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    chat_id     TEXT NOT NULL,
    name        TEXT NOT NULL,
    brand       TEXT NOT NULL,
    model       TEXT,
    year_from   INTEGER,
    price_min   INTEGER,
    price_max   INTEGER,
    mileage_max INTEGER,
    fuel        TEXT,
    gearbox     TEXT,
    countries   TEXT DEFAULT 'D',        -- comma separated autoscout24 codes
    paused      INTEGER NOT NULL DEFAULT 0,
    muted       INTEGER NOT NULL DEFAULT 0,
    created_at  TEXT NOT NULL,
    UNIQUE (chat_id, name)
);

CREATE TABLE IF NOT EXISTS subscribers (
    chat_id   TEXT PRIMARY KEY,
    username  TEXT,
    added_at  TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS favorites (
    chat_id    TEXT NOT NULL,
    listing_id TEXT NOT NULL,
    added_at   TEXT NOT NULL,
    PRIMARY KEY (chat_id, listing_id)
);

CREATE TABLE IF NOT EXISTS blocked_dealers (
    chat_id    TEXT NOT NULL,
    dealer_key TEXT NOT NULL,            -- "{source}:{dealer_id}"
    name       TEXT,
    added_at   TEXT NOT NULL,
    PRIMARY KEY (chat_id, dealer_key)
);

CREATE TABLE IF NOT EXISTS dialog_state (
    chat_id  TEXT PRIMARY KEY,
    step     TEXT NOT NULL,
    draft    TEXT NOT NULL DEFAULT '{}',
    started  TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT
);

CREATE INDEX IF NOT EXISTS idx_listings_source ON listings(source);
CREATE INDEX IF NOT EXISTS idx_listings_dealer ON listings(source, dealer_id);
CREATE INDEX IF NOT EXISTS idx_searches_chat ON searches(chat_id);
"""

# columns added after the first release - applied to existing db files on open
MIGRATIONS = [
    ("listings", "dealer_id", "TEXT"),
    ("listings", "dealer_name", "TEXT"),
    ("notification_queue", "chat_id", "TEXT"),
    ("notification_queue", "search_id", "INTEGER"),
]


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class Db:
    def __init__(self, path=DB_PATH, check_same_thread=True):
        self.path = Path(path)
        self.conn = sqlite3.connect(self.path, check_same_thread=check_same_thread)
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript(SCHEMA)
        self._migrate()
        self.conn.commit()

    def _migrate(self) -> None:
        for table, column, coltype in MIGRATIONS:
            existing = {r["name"] for r in self.conn.execute(f"PRAGMA table_info({table})")}
            if column not in existing:
                self.conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {coltype}")

    def close(self):
        self.conn.close()

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.conn.commit()
        self.close()

    # --- listings ---------------------------------------------------------
    def known_ids(self, source=None) -> set:
        sql = "SELECT id FROM listings"
        args = ()
        if source:
            sql += " WHERE source = ?"
            args = (source,)
        return {r["id"] for r in self.conn.execute(sql, args)}

    def get_price(self, listing_id: str):
        row = self.conn.execute("SELECT price_eur FROM listings WHERE id = ?",
                                (listing_id,)).fetchone()
        return row["price_eur"] if row else None

    def upsert(self, listing) -> None:
        ts = now()
        self.conn.execute(
            """INSERT INTO listings (id, source, brand, model, year, mileage_km, price_eur,
                                     fuel, gearbox, url, image_url, dealer_id, dealer_name,
                                     first_seen, last_seen)
               VALUES (:id, :source, :brand, :model, :year, :mileage_km, :price_eur,
                       :fuel, :gearbox, :url, :image_url, :dealer_id, :dealer_name, :ts, :ts)
               ON CONFLICT(id) DO UPDATE SET
                   price_eur = excluded.price_eur,
                   mileage_km = excluded.mileage_km,
                   url = excluded.url,
                   image_url = excluded.image_url,
                   dealer_id = excluded.dealer_id,
                   dealer_name = excluded.dealer_name,
                   last_seen = excluded.last_seen""",
            {**listing.as_dict(), "ts": ts},
        )

    # --- notifications ----------------------------------------------------
    def already_notified(self, listing_id: str, search_name: str, kind: str = "new") -> bool:
        return self.conn.execute(
            "SELECT 1 FROM notifications_sent WHERE listing_id=? AND search_name=? AND kind=?",
            (listing_id, search_name, kind)).fetchone() is not None

    def mark_notified(self, listing_id: str, search_name: str, kind: str = "new") -> None:
        self.conn.execute(
            "INSERT OR REPLACE INTO notifications_sent (listing_id, search_name, kind, sent_at)"
            " VALUES (?, ?, ?, ?)", (listing_id, search_name, kind, now()))

    # --- queue (rate-limit overflow carried to the next run) --------------
    def enqueue(self, listing_id: str, search_name: str, kind: str, old_price=None,
                chat_id=None, search_id=None) -> None:
        self.conn.execute(
            "INSERT OR IGNORE INTO notification_queue"
            " (listing_id, search_name, kind, old_price, chat_id, search_id, queued_at)"
            " VALUES (?, ?, ?, ?, ?, ?, ?)",
            (listing_id, search_name, kind, old_price,
             str(chat_id) if chat_id else None, search_id, now()))

    def dequeue_all(self) -> list:
        rows = self.conn.execute(
            """SELECT l.*, q.search_name, q.kind, q.old_price, q.chat_id, q.search_id
               FROM notification_queue q JOIN listings l ON l.id = q.listing_id
               ORDER BY q.queued_at""").fetchall()
        return [dict(r) for r in rows]

    def drop_from_queue(self, listing_id: str, search_name: str, kind: str) -> None:
        self.conn.execute(
            "DELETE FROM notification_queue WHERE listing_id=? AND search_name=? AND kind=?",
            (listing_id, search_name, kind))

    # --- searches (owned by the bot) --------------------------------------
    def add_search(self, chat_id, profile: dict) -> int:
        cur = self.conn.execute(
            """INSERT INTO searches (chat_id, name, brand, model, year_from, price_min,
                                     price_max, mileage_max, fuel, gearbox, countries, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (str(chat_id), profile["name"], profile["brand"], profile.get("model"),
             profile.get("year_from"), profile.get("price_min"), profile.get("price_max"),
             profile.get("mileage_max"), profile.get("fuel"), profile.get("gearbox"),
             ",".join(profile.get("countries") or ["D"]), now()))
        self.conn.commit()
        return cur.lastrowid

    def list_searches(self, chat_id=None, active_only=False) -> list:
        sql, args = "SELECT * FROM searches", []
        where = []
        if chat_id is not None:
            where.append("chat_id = ?")
            args.append(str(chat_id))
        if active_only:
            where.append("paused = 0")
        if where:
            sql += " WHERE " + " AND ".join(where)
        sql += " ORDER BY id"
        return [self._row_to_profile(r) for r in self.conn.execute(sql, args)]

    @staticmethod
    def _row_to_profile(row) -> dict:
        p = dict(row)
        p["countries"] = [c for c in (p.get("countries") or "D").split(",") if c]
        return p

    def get_search(self, search_id: int, chat_id=None):
        sql, args = "SELECT * FROM searches WHERE id = ?", [search_id]
        if chat_id is not None:
            sql += " AND chat_id = ?"
            args.append(str(chat_id))
        row = self.conn.execute(sql, args).fetchone()
        return self._row_to_profile(row) if row else None

    def delete_search(self, search_id: int, chat_id) -> bool:
        cur = self.conn.execute("DELETE FROM searches WHERE id = ? AND chat_id = ?",
                                (search_id, str(chat_id)))
        self.conn.commit()
        return cur.rowcount > 0

    def set_search_flag(self, search_id: int, chat_id, field: str, value: int) -> bool:
        if field not in ("paused", "muted"):
            raise ValueError(field)
        cur = self.conn.execute(
            f"UPDATE searches SET {field} = ? WHERE id = ? AND chat_id = ?",
            (int(value), search_id, str(chat_id)))
        self.conn.commit()
        return cur.rowcount > 0

    def search_name_taken(self, chat_id, name: str) -> bool:
        return self.conn.execute("SELECT 1 FROM searches WHERE chat_id = ? AND name = ?",
                                 (str(chat_id), name)).fetchone() is not None

    # --- subscribers ------------------------------------------------------
    def add_subscriber(self, chat_id, username=None) -> None:
        self.conn.execute(
            "INSERT INTO subscribers (chat_id, username, added_at) VALUES (?, ?, ?)"
            " ON CONFLICT(chat_id) DO UPDATE SET username = excluded.username",
            (str(chat_id), username, now()))
        self.conn.commit()

    # --- favorites --------------------------------------------------------
    def toggle_favorite(self, chat_id, listing_id: str) -> bool:
        """Returns True if the listing is a favorite after the call."""
        cur = self.conn.execute("DELETE FROM favorites WHERE chat_id = ? AND listing_id = ?",
                                (str(chat_id), listing_id))
        if cur.rowcount:
            self.conn.commit()
            return False
        self.conn.execute(
            "INSERT INTO favorites (chat_id, listing_id, added_at) VALUES (?, ?, ?)",
            (str(chat_id), listing_id, now()))
        self.conn.commit()
        return True

    def list_favorites(self, chat_id, limit: int = 30) -> list:
        rows = self.conn.execute(
            """SELECT l.* FROM favorites f JOIN listings l ON l.id = f.listing_id
               WHERE f.chat_id = ? ORDER BY f.added_at DESC LIMIT ?""",
            (str(chat_id), limit))
        return [dict(r) for r in rows]

    # --- blocked dealers --------------------------------------------------
    def block_dealer(self, chat_id, dealer_key: str, name=None) -> None:
        self.conn.execute(
            "INSERT OR IGNORE INTO blocked_dealers (chat_id, dealer_key, name, added_at)"
            " VALUES (?, ?, ?, ?)", (str(chat_id), dealer_key, name, now()))
        self.conn.commit()

    def unblock_dealer(self, chat_id, dealer_key: str) -> bool:
        cur = self.conn.execute("DELETE FROM blocked_dealers WHERE chat_id = ? AND dealer_key = ?",
                                (str(chat_id), dealer_key))
        self.conn.commit()
        return cur.rowcount > 0

    def blocked_dealers(self, chat_id) -> dict:
        return {r["dealer_key"]: r["name"] for r in self.conn.execute(
            "SELECT dealer_key, name FROM blocked_dealers WHERE chat_id = ?", (str(chat_id),))}

    # --- /add dialog state ------------------------------------------------
    def get_dialog(self, chat_id):
        row = self.conn.execute("SELECT step, draft FROM dialog_state WHERE chat_id = ?",
                                (str(chat_id),)).fetchone()
        return (row["step"], json.loads(row["draft"])) if row else (None, {})

    def set_dialog(self, chat_id, step: str, draft: dict) -> None:
        self.conn.execute(
            "INSERT INTO dialog_state (chat_id, step, draft, started) VALUES (?, ?, ?, ?)"
            " ON CONFLICT(chat_id) DO UPDATE SET step = excluded.step, draft = excluded.draft",
            (str(chat_id), step, json.dumps(draft, ensure_ascii=False), now()))
        self.conn.commit()

    def clear_dialog(self, chat_id) -> None:
        self.conn.execute("DELETE FROM dialog_state WHERE chat_id = ?", (str(chat_id),))
        self.conn.commit()

    # --- meta -------------------------------------------------------------
    def get_meta(self, key: str, default=None):
        row = self.conn.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
        return row["value"] if row else default

    def set_meta(self, key: str, value) -> None:
        self.conn.execute(
            "INSERT INTO meta (key, value) VALUES (?, ?)"
            " ON CONFLICT(key) DO UPDATE SET value = excluded.value", (key, str(value)))
        self.conn.commit()

    def stats(self) -> dict:
        one = lambda sql: self.conn.execute(sql).fetchone()[0]
        return {
            "listings": one("SELECT count(*) FROM listings"),
            "searches": one("SELECT count(*) FROM searches"),
            "queued": one("SELECT count(*) FROM notification_queue"),
            "favorites": one("SELECT count(*) FROM favorites"),
            "last_run": self.get_meta("last_run", "never"),
        }

    def commit(self):
        self.conn.commit()
