"""SQLite storage. The file is committed back to the repo after every run."""

import json
import secrets
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
    images      TEXT,                      -- JSON array of photo urls
    title       TEXT,
    country     TEXT,
    city        TEXT,
    damaged     INTEGER,
    vin         TEXT,
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
    brand       TEXT NOT NULL,           -- kept for old rows; brands is the live column
    brands      TEXT,                    -- comma separated, one search may span makes
    model       TEXT,
    models      TEXT,
    year_from   INTEGER,
    price_min   INTEGER,
    price_max   INTEGER,
    mileage_max INTEGER,
    fuel        TEXT,
    fuels       TEXT,
    gearbox     TEXT,
    gearboxes   TEXT,
    countries   TEXT DEFAULT 'D',        -- comma separated autoscout24 codes
    exclude_damaged INTEGER NOT NULL DEFAULT 1,
    paused      INTEGER NOT NULL DEFAULT 0,
    muted       INTEGER NOT NULL DEFAULT 0,
    created_at  TEXT NOT NULL,
    UNIQUE (chat_id, name)
);

CREATE TABLE IF NOT EXISTS subscribers (
    chat_id   TEXT PRIMARY KEY,
    username  TEXT,
    token     TEXT,                      -- unguessable name of this chat's webapp feed
    added_at  TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS swipes (
    chat_id    TEXT NOT NULL,
    listing_id TEXT NOT NULL,
    verdict    TEXT NOT NULL,            -- 'like' | 'pass'
    swiped_at  TEXT NOT NULL,
    PRIMARY KEY (chat_id, listing_id)
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
    ("listings", "images", "TEXT"),
    ("listings", "title", "TEXT"),
    ("listings", "country", "TEXT"),
    ("listings", "city", "TEXT"),
    ("listings", "damaged", "INTEGER"),
    ("listings", "vin", "TEXT"),
    ("searches", "exclude_damaged", "INTEGER NOT NULL DEFAULT 1"),
    ("searches", "brands", "TEXT"),
    ("searches", "models", "TEXT"),
    ("searches", "fuels", "TEXT"),
    ("searches", "gearboxes", "TEXT"),
    ("listings", "dealer_id", "TEXT"),
    ("listings", "dealer_name", "TEXT"),
    ("notification_queue", "chat_id", "TEXT"),
    ("notification_queue", "search_id", "INTEGER"),
    ("subscribers", "token", "TEXT"),
]


def load_env(path=None) -> None:
    """Minimal .env loader so tokens never have to sit in the command line."""
    path = Path(path or Path(__file__).resolve().parent.parent / ".env")
    if not path.exists():
        return
    import os
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        os.environ.setdefault(key.strip(), value.strip().strip("'\""))


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
        row = listing.as_dict()
        row["images"] = json.dumps(row.get("images") or [], ensure_ascii=False)
        row["damaged"] = None if row.get("damaged") is None else int(row["damaged"])
        self.conn.execute(
            """INSERT INTO listings (id, source, brand, model, year, mileage_km, price_eur,
                                     fuel, gearbox, url, image_url, images, title,
                                     country, city, damaged, vin, dealer_id,
                                     dealer_name, first_seen, last_seen)
               VALUES (:id, :source, :brand, :model, :year, :mileage_km, :price_eur,
                       :fuel, :gearbox, :url, :image_url, :images, :title,
                       :country, :city, :damaged, :vin, :dealer_id, :dealer_name, :ts, :ts)
               ON CONFLICT(id) DO UPDATE SET
                   price_eur = excluded.price_eur,
                   mileage_km = excluded.mileage_km,
                   url = excluded.url,
                   image_url = excluded.image_url,
                   images = excluded.images,
                   title = excluded.title,
                   country = excluded.country,
                   city = excluded.city,
                   damaged = excluded.damaged,
                   vin = COALESCE(excluded.vin, vin),
                   dealer_id = excluded.dealer_id,
                   dealer_name = excluded.dealer_name,
                   last_seen = excluded.last_seen""",
            {**row, "ts": ts},
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
    @staticmethod
    def _as_list(profile: dict, plural: str, singular: str) -> list:
        """Accept either the list form or a single legacy value."""
        values = profile.get(plural)
        if values is None:
            values = [profile[singular]] if profile.get(singular) else []
        return [str(v) for v in values if v]

    def add_search(self, chat_id, profile: dict) -> int:
        brands = self._as_list(profile, "brands", "brand")
        if not brands:
            raise ValueError("a search needs at least one brand")
        models = self._as_list(profile, "models", "model")
        fuels = self._as_list(profile, "fuels", "fuel")
        gearboxes = self._as_list(profile, "gearboxes", "gearbox")
        cur = self.conn.execute(
            """INSERT INTO searches (chat_id, name, brand, brands, model, models,
                                     year_from, price_min, price_max, mileage_max,
                                     fuel, fuels, gearbox, gearboxes, countries,
                                     exclude_damaged, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (str(chat_id), profile["name"],
             brands[0], ",".join(brands),
             models[0] if models else None, ",".join(models),
             profile.get("year_from"), profile.get("price_min"), profile.get("price_max"),
             profile.get("mileage_max"),
             fuels[0] if fuels else None, ",".join(fuels),
             gearboxes[0] if gearboxes else None, ",".join(gearboxes),
             ",".join(profile.get("countries") or ["D"]),
             int(profile.get("exclude_damaged", 1)), now()))
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
        # rows written before multi-select carry only the singular column
        for plural, singular in (("brands", "brand"), ("models", "model"),
                                 ("fuels", "fuel"), ("gearboxes", "gearbox")):
            raw = p.get(plural)
            values = [v for v in (raw or "").split(",") if v]
            if not values and p.get(singular):
                values = [p[singular]]
            p[plural] = values
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

    def chat_token(self, chat_id) -> str:
        """Stable random name for this chat's webapp feed file."""
        row = self.conn.execute("SELECT token FROM subscribers WHERE chat_id = ?",
                                (str(chat_id),)).fetchone()
        if row and row["token"]:
            return row["token"]
        token = secrets.token_urlsafe(12)
        self.add_subscriber(chat_id)
        self.conn.execute("UPDATE subscribers SET token = ? WHERE chat_id = ?",
                          (token, str(chat_id)))
        self.conn.commit()
        return token

    def chat_for_token(self, token: str):
        row = self.conn.execute("SELECT chat_id FROM subscribers WHERE token = ?",
                                (token,)).fetchone()
        return row["chat_id"] if row else None

    # --- swipes -----------------------------------------------------------
    def record_swipe(self, chat_id, listing_id: str, verdict: str) -> None:
        self.conn.execute(
            "INSERT INTO swipes (chat_id, listing_id, verdict, swiped_at) VALUES (?, ?, ?, ?)"
            " ON CONFLICT(chat_id, listing_id) DO UPDATE SET verdict = excluded.verdict,"
            " swiped_at = excluded.swiped_at",
            (str(chat_id), listing_id, verdict, now()))

    def swiped_ids(self, chat_id) -> set:
        return {r["listing_id"] for r in self.conn.execute(
            "SELECT listing_id FROM swipes WHERE chat_id = ?", (str(chat_id),))}

    def swipe_deck(self, chat_id, limit: int = 200) -> list:
        """Listings already alerted to this chat that have not been swiped yet."""
        rows = self.conn.execute(
            """SELECT DISTINCT l.* FROM listings l
               JOIN notifications_sent n ON n.listing_id = l.id
               JOIN searches s ON s.name = n.search_name AND s.chat_id = ?
               WHERE l.id NOT IN (SELECT listing_id FROM swipes WHERE chat_id = ?)
               ORDER BY l.first_seen DESC LIMIT ?""",
            (str(chat_id), str(chat_id), limit))
        return [dict(r) for r in rows]

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
