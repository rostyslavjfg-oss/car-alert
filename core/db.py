"""SQLite storage. The file is committed back to the repo after every run."""

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
    queued_at   TEXT NOT NULL,
    PRIMARY KEY (listing_id, search_name, kind)
);

CREATE INDEX IF NOT EXISTS idx_listings_source ON listings(source);
"""


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class Db:
    def __init__(self, path=DB_PATH):
        self.path = Path(path)
        self.conn = sqlite3.connect(self.path)
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript(SCHEMA)
        self.conn.commit()

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
                                     fuel, gearbox, url, image_url, first_seen, last_seen)
               VALUES (:id, :source, :brand, :model, :year, :mileage_km, :price_eur,
                       :fuel, :gearbox, :url, :image_url, :ts, :ts)
               ON CONFLICT(id) DO UPDATE SET
                   price_eur = excluded.price_eur,
                   mileage_km = excluded.mileage_km,
                   url = excluded.url,
                   image_url = excluded.image_url,
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
    def enqueue(self, listing_id: str, search_name: str, kind: str, old_price=None) -> None:
        self.conn.execute(
            "INSERT OR IGNORE INTO notification_queue"
            " (listing_id, search_name, kind, old_price, queued_at) VALUES (?, ?, ?, ?, ?)",
            (listing_id, search_name, kind, old_price, now()))

    def dequeue_all(self) -> list:
        rows = self.conn.execute(
            """SELECT q.listing_id, q.search_name, q.kind, q.old_price, l.*
               FROM notification_queue q JOIN listings l ON l.id = q.listing_id
               ORDER BY q.queued_at""").fetchall()
        return [dict(r) for r in rows]

    def drop_from_queue(self, listing_id: str, search_name: str, kind: str) -> None:
        self.conn.execute(
            "DELETE FROM notification_queue WHERE listing_id=? AND search_name=? AND kind=?",
            (listing_id, search_name, kind))

    def commit(self):
        self.conn.commit()
