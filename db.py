"""Database layer. Works on SQLite (default, local/dev) and PostgreSQL (production).

Set DATABASE_URL to a postgres:// or postgresql:// URL to use PostgreSQL.
Otherwise a SQLite file is used (path from SQLITE_PATH, default ./data/inventory.db).
"""
import os
import re
import sqlite3
from contextlib import contextmanager

DATABASE_URL = os.environ.get("DATABASE_URL", "").strip()
IS_PG = DATABASE_URL.startswith("postgres://") or DATABASE_URL.startswith("postgresql://")

if IS_PG:
    import psycopg2
    import psycopg2.extras

SQLITE_PATH = os.environ.get("SQLITE_PATH", os.path.join(os.path.dirname(os.path.abspath(__file__)), "data", "inventory.db"))


def _to_pg(sql):
    """Translate the '?' placeholder style used throughout the app into psycopg2's '%s'."""
    out, in_s = [], False
    for ch in sql:
        if ch == "'":
            in_s = not in_s
        if ch == "?" and not in_s:
            out.append("%s")
        else:
            out.append(ch)
    return "".join(out)


class Cursor:
    """Thin cursor wrapper so the rest of the app can always use '?' placeholders
    and always get dict-like rows back."""

    def __init__(self, cur):
        self._cur = cur

    def execute(self, sql, params=()):
        self._cur.execute(_to_pg(sql) if IS_PG else sql, params)
        return self

    def fetchone(self):
        return self._cur.fetchone()

    def fetchall(self):
        return self._cur.fetchall()

    @property
    def lastrowid(self):
        return self._cur.lastrowid


class Conn:
    def __init__(self, raw):
        self.raw = raw

    def cursor(self):
        if IS_PG:
            return Cursor(self.raw.cursor(cursor_factory=psycopg2.extras.RealDictCursor))
        return Cursor(self.raw.cursor())

    def execute(self, sql, params=()):
        return self.cursor().execute(sql, params)

    def query(self, sql, params=()):
        rows = self.execute(sql, params).fetchall()
        return [dict(r) for r in rows]

    def query_one(self, sql, params=()):
        row = self.execute(sql, params).fetchone()
        return dict(row) if row is not None else None

    def scalar(self, sql, params=(), default=0):
        row = self.execute(sql, params).fetchone()
        if row is None:
            return default
        val = list(dict(row).values())[0]
        return default if val is None else val

    def insert(self, table, data):
        """Insert a dict and return the new row id."""
        cols = list(data.keys())
        vals = [data[c] for c in cols]
        ph = ", ".join("?" for _ in cols)
        sql = "INSERT INTO {} ({}) VALUES ({})".format(table, ", ".join(cols), ph)
        if IS_PG:
            row = self.execute(sql + " RETURNING id", vals).fetchone()
            return dict(row)["id"]
        cur = self.execute(sql, vals)
        return cur.lastrowid

    def commit(self):
        self.raw.commit()

    def rollback(self):
        self.raw.rollback()

    def close(self):
        self.raw.close()


def connect():
    if IS_PG:
        url = DATABASE_URL
        if url.startswith("postgres://"):
            url = "postgresql://" + url[len("postgres://"):]
        raw = psycopg2.connect(url)
        return Conn(raw)
    os.makedirs(os.path.dirname(SQLITE_PATH), exist_ok=True)
    raw = sqlite3.connect(SQLITE_PATH)
    raw.row_factory = sqlite3.Row
    raw.execute("PRAGMA foreign_keys = ON")
    return Conn(raw)


@contextmanager
def get_db():
    conn = connect()
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


PK = "SERIAL PRIMARY KEY" if IS_PG else "INTEGER PRIMARY KEY AUTOINCREMENT"
NUM = "DOUBLE PRECISION"

SCHEMA = [
    """CREATE TABLE IF NOT EXISTS settings (
        id INTEGER PRIMARY KEY,
        business_name TEXT NOT NULL DEFAULT 'My Dry Fruits Business',
        gstin TEXT DEFAULT '',
        address TEXT DEFAULT '',
        state_code TEXT NOT NULL DEFAULT '27',
        phone TEXT DEFAULT '',
        email TEXT DEFAULT '',
        invoice_prefix TEXT NOT NULL DEFAULT 'SDF',
        gst_enabled INTEGER NOT NULL DEFAULT 1,
        bank_details TEXT DEFAULT '',
        terms TEXT DEFAULT ''
    )""",
    """CREATE TABLE IF NOT EXISTS items (
        id {PK},
        name TEXT NOT NULL,
        hsn TEXT DEFAULT '',
        gst_rate {NUM} NOT NULL DEFAULT 5,
        unit TEXT NOT NULL DEFAULT 'kg',
        low_stock_qty {NUM} NOT NULL DEFAULT 0,
        sale_rate {NUM} NOT NULL DEFAULT 0,
        opening_qty {NUM} NOT NULL DEFAULT 0,
        opening_rate {NUM} NOT NULL DEFAULT 0,
        stock_qty {NUM} NOT NULL DEFAULT 0,
        stock_value {NUM} NOT NULL DEFAULT 0,
        active INTEGER NOT NULL DEFAULT 1,
        created_at TEXT NOT NULL DEFAULT ''
    )""",
    """CREATE TABLE IF NOT EXISTS parties (
        id {PK},
        name TEXT NOT NULL,
        kind TEXT NOT NULL DEFAULT 'customer',
        gstin TEXT DEFAULT '',
        phone TEXT DEFAULT '',
        address TEXT DEFAULT '',
        state_code TEXT NOT NULL DEFAULT '27',
        active INTEGER NOT NULL DEFAULT 1,
        created_at TEXT NOT NULL DEFAULT ''
    )""",
    """CREATE TABLE IF NOT EXISTS purchases (
        id {PK},
        bill_no TEXT DEFAULT '',
        bill_date TEXT NOT NULL,
        party_id INTEGER,
        party_name TEXT DEFAULT '',
        state_code TEXT NOT NULL DEFAULT '27',
        interstate INTEGER NOT NULL DEFAULT 0,
        taxable {NUM} NOT NULL DEFAULT 0,
        cgst {NUM} NOT NULL DEFAULT 0,
        sgst {NUM} NOT NULL DEFAULT 0,
        igst {NUM} NOT NULL DEFAULT 0,
        round_off {NUM} NOT NULL DEFAULT 0,
        total {NUM} NOT NULL DEFAULT 0,
        payment_mode TEXT DEFAULT 'Cash',
        notes TEXT DEFAULT '',
        created_at TEXT NOT NULL DEFAULT ''
    )""",
    """CREATE TABLE IF NOT EXISTS purchase_lines (
        id {PK},
        purchase_id INTEGER NOT NULL,
        item_id INTEGER NOT NULL,
        item_name TEXT DEFAULT '',
        hsn TEXT DEFAULT '',
        qty {NUM} NOT NULL DEFAULT 0,
        rate {NUM} NOT NULL DEFAULT 0,
        discount_pct {NUM} NOT NULL DEFAULT 0,
        taxable {NUM} NOT NULL DEFAULT 0,
        gst_rate {NUM} NOT NULL DEFAULT 0,
        cgst {NUM} NOT NULL DEFAULT 0,
        sgst {NUM} NOT NULL DEFAULT 0,
        igst {NUM} NOT NULL DEFAULT 0,
        total {NUM} NOT NULL DEFAULT 0
    )""",
    """CREATE TABLE IF NOT EXISTS sales (
        id {PK},
        invoice_no TEXT NOT NULL,
        invoice_date TEXT NOT NULL,
        party_id INTEGER,
        party_name TEXT DEFAULT '',
        state_code TEXT NOT NULL DEFAULT '27',
        interstate INTEGER NOT NULL DEFAULT 0,
        taxable {NUM} NOT NULL DEFAULT 0,
        cgst {NUM} NOT NULL DEFAULT 0,
        sgst {NUM} NOT NULL DEFAULT 0,
        igst {NUM} NOT NULL DEFAULT 0,
        round_off {NUM} NOT NULL DEFAULT 0,
        total {NUM} NOT NULL DEFAULT 0,
        cogs {NUM} NOT NULL DEFAULT 0,
        payment_mode TEXT DEFAULT 'Cash',
        notes TEXT DEFAULT '',
        created_at TEXT NOT NULL DEFAULT ''
    )""",
    """CREATE TABLE IF NOT EXISTS sale_lines (
        id {PK},
        sale_id INTEGER NOT NULL,
        item_id INTEGER NOT NULL,
        item_name TEXT DEFAULT '',
        hsn TEXT DEFAULT '',
        qty {NUM} NOT NULL DEFAULT 0,
        rate {NUM} NOT NULL DEFAULT 0,
        discount_pct {NUM} NOT NULL DEFAULT 0,
        taxable {NUM} NOT NULL DEFAULT 0,
        gst_rate {NUM} NOT NULL DEFAULT 0,
        cgst {NUM} NOT NULL DEFAULT 0,
        sgst {NUM} NOT NULL DEFAULT 0,
        igst {NUM} NOT NULL DEFAULT 0,
        total {NUM} NOT NULL DEFAULT 0,
        cost_rate {NUM} NOT NULL DEFAULT 0
    )""",
    "CREATE INDEX IF NOT EXISTS idx_pl_purchase ON purchase_lines(purchase_id)",
    "CREATE INDEX IF NOT EXISTS idx_sl_sale ON sale_lines(sale_id)",
    "CREATE INDEX IF NOT EXISTS idx_p_date ON purchases(bill_date)",
    "CREATE INDEX IF NOT EXISTS idx_s_date ON sales(invoice_date)",
]

SEED_ITEMS = [
    # name, hsn, gst_rate, unit, low_stock, sale_rate
    ("Masala Kaju (Spiced Cashew)", "2008", 5, "kg", 5, 1100),
    ("Peri Peri Almonds", "2008", 5, "kg", 5, 950),
    ("Spicy Roasted Makhana", "2008", 5, "kg", 3, 800),
    ("Chilli Garlic Peanuts", "2008", 5, "kg", 5, 320),
    ("Plain Cashew (Kaju W240)", "0801", 5, "kg", 5, 900),
    ("Almonds (Badam)", "0802", 5, "kg", 5, 780),
    ("Raisins (Kishmish)", "0806", 5, "kg", 5, 320),
    ("Pistachios (Pista)", "0802", 5, "kg", 3, 1400),
]


def init_db():
    with get_db() as conn:
        for stmt in SCHEMA:
            conn.execute(stmt.format(PK=PK, NUM=NUM))
        row = conn.query_one("SELECT id FROM settings WHERE id = 1")
        if not row:
            conn.execute("INSERT INTO settings (id) VALUES (1)")
        n = conn.scalar("SELECT COUNT(*) AS c FROM items")
        if not n:
            for name, hsn, rate, unit, low, srate in SEED_ITEMS:
                conn.insert("items", dict(
                    name=name, hsn=hsn, gst_rate=rate, unit=unit,
                    low_stock_qty=low, sale_rate=srate, created_at="",
                ))
