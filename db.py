"""Database layer. Works on SQLite (default, local/dev) and PostgreSQL (production).

Set DATABASE_URL to a postgres:// or postgresql:// URL to use PostgreSQL.
Otherwise a SQLite file is used (path from SQLITE_PATH, default ./data/inventory.db).
"""
import os
import re
import sqlite3
import threading
import time
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


def _is_disconnect(exc):
    return IS_PG and isinstance(exc, (psycopg2.OperationalError, psycopg2.InterfaceError))


class Cursor:
    """Thin cursor wrapper so the rest of the app can always use '?' placeholders
    and always get dict-like rows back."""

    def __init__(self, cur, conn=None):
        self._cur = cur
        self._conn = conn

    def _run(self, fn):
        try:
            fn(self._cur)
        except Exception as exc:
            # A pooled connection the server dropped while it sat idle (Neon
            # suspends its compute) fails on its first statement. Nothing has
            # run in this transaction yet, so reconnecting and retrying is safe.
            if not (self._conn is not None and self._conn.reused and _is_disconnect(exc)):
                raise
            self._conn.reconnect()
            self._cur = self._conn.raw_cursor()
            fn(self._cur)
        if self._conn is not None:
            self._conn.reused = False

    def execute(self, sql, params=()):
        q = _to_pg(sql) if IS_PG else sql
        self._run(lambda cur: cur.execute(q, params))
        return self

    def executemany(self, sql, seq):
        seq = list(seq)
        if not seq:
            return self
        if IS_PG:
            # one round trip per page instead of one per row - matters on a
            # remote database such as Neon
            q = _to_pg(sql)
            self._run(lambda cur: psycopg2.extras.execute_batch(cur, q, seq, page_size=500))
        else:
            self._run(lambda cur: cur.executemany(sql, seq))
        return self

    def fetchone(self):
        return self._cur.fetchone()

    def fetchall(self):
        return self._cur.fetchall()

    @property
    def lastrowid(self):
        return self._cur.lastrowid


class Conn:
    def __init__(self, raw, reused=False):
        self.raw = raw
        self.reused = reused    # came from the idle pool and is not yet proven alive

    def raw_cursor(self):
        if IS_PG:
            return self.raw.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        return self.raw.cursor()

    def cursor(self):
        return Cursor(self.raw_cursor(), self)

    def reconnect(self):
        try:
            self.raw.close()
        except Exception:
            pass
        self.raw = psycopg2.connect(_pg_url())
        self.reused = False

    def execute(self, sql, params=()):
        return self.cursor().execute(sql, params)

    def executemany(self, sql, seq):
        return self.cursor().executemany(sql, seq)

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


# ---------------------------------------------------------------- pooling
# Opening a fresh TLS connection to a hosted Postgres (Neon) costs 100-300 ms,
# and every page used to open two. A small per-process pool keeps connections
# warm. Neon closes idle connections when its compute sleeps; such a connection
# fails on its first statement and is replaced there and then (see Cursor._run),
# so no request pays for a liveness ping.
POOL_MAX = int(os.environ.get("DB_POOL_MAX", "4"))
IDLE_SECONDS = 600     # close a connection left idle longer than this
_idle = []          # [(raw_connection, last_used_time)]
_idle_pid = None
_idle_lock = threading.Lock()


def _pg_url():
    url = DATABASE_URL
    if url.startswith("postgres://"):
        url = "postgresql://" + url[len("postgres://"):]
    return url


def _checkout():
    """A warm connection from this process's idle list, or a new one. A worker
    forked from another process never reuses its parent's sockets."""
    global _idle_pid
    while True:
        with _idle_lock:
            if _idle_pid != os.getpid():
                _idle[:] = []
                _idle_pid = os.getpid()
            item = _idle.pop() if _idle else None
        if item is None:
            return psycopg2.connect(_pg_url()), False
        raw, last_used = item
        if not raw.closed and time.time() - last_used < IDLE_SECONDS:
            return raw, True
        try:
            raw.close()
        except Exception:
            pass


def _checkin(raw, broken=False):
    try:
        if broken or raw.closed:
            raw.close()
            return
        raw.rollback()      # never hand on an open transaction
        with _idle_lock:
            if _idle_pid == os.getpid() and len(_idle) < POOL_MAX:
                _idle.append((raw, time.time()))
                return
        raw.close()
    except Exception:
        pass


def connect():
    if IS_PG:
        return Conn(psycopg2.connect(_pg_url()))
    os.makedirs(os.path.dirname(SQLITE_PATH), exist_ok=True)
    raw = sqlite3.connect(SQLITE_PATH)
    raw.row_factory = sqlite3.Row
    raw.execute("PRAGMA foreign_keys = ON")
    return Conn(raw)


@contextmanager
def get_db():
    if not IS_PG:
        conn = connect()
        try:
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()
        return
    raw, reused = _checkout()
    conn = Conn(raw, reused)
    broken = False
    try:
        yield conn
        conn.commit()
    except Exception as exc:
        broken = isinstance(exc, (psycopg2.OperationalError, psycopg2.InterfaceError))
        try:
            conn.rollback()
        except Exception:
            broken = True
        raise
    finally:
        _checkin(conn.raw, broken)


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
        terms TEXT DEFAULT '',
        admin_hash TEXT DEFAULT '',
        admin_salt TEXT DEFAULT ''
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
        created_at TEXT NOT NULL DEFAULT '',
        updated_at TEXT DEFAULT '',
        edit_count INTEGER NOT NULL DEFAULT 0
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
        created_at TEXT NOT NULL DEFAULT '',
        updated_at TEXT DEFAULT '',
        edit_count INTEGER NOT NULL DEFAULT 0
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
    # Recipe: how much of each raw material goes into one unit of a product.
    """CREATE TABLE IF NOT EXISTS recipe_lines (
        id {PK},
        product_id INTEGER NOT NULL,
        raw_id INTEGER NOT NULL,
        qty_per_unit {NUM} NOT NULL DEFAULT 0
    )""",
    # A production batch turns raw materials into a finished product.
    """CREATE TABLE IF NOT EXISTS productions (
        id {PK},
        batch_no TEXT DEFAULT '',
        prod_date TEXT NOT NULL,
        product_id INTEGER NOT NULL,
        product_name TEXT DEFAULT '',
        qty_out {NUM} NOT NULL DEFAULT 0,
        raw_cost {NUM} NOT NULL DEFAULT 0,
        other_cost {NUM} NOT NULL DEFAULT 0,
        total_cost {NUM} NOT NULL DEFAULT 0,
        notes TEXT DEFAULT '',
        created_at TEXT NOT NULL DEFAULT '',
        updated_at TEXT DEFAULT '',
        edit_count INTEGER NOT NULL DEFAULT 0
    )""",
    """CREATE TABLE IF NOT EXISTS production_lines (
        id {PK},
        production_id INTEGER NOT NULL,
        raw_id INTEGER NOT NULL,
        raw_name TEXT DEFAULT '',
        qty {NUM} NOT NULL DEFAULT 0,
        cost_rate {NUM} NOT NULL DEFAULT 0,
        cost {NUM} NOT NULL DEFAULT 0
    )""",
    """CREATE TABLE IF NOT EXISTS users (
        id {PK},
        email TEXT NOT NULL UNIQUE,
        name TEXT DEFAULT '',
        role TEXT NOT NULL DEFAULT 'member',
        active INTEGER NOT NULL DEFAULT 1,
        pw_hash TEXT DEFAULT '',
        pw_salt TEXT DEFAULT '',
        token_hash TEXT DEFAULT '',
        token_expires TEXT DEFAULT '',
        session_version INTEGER NOT NULL DEFAULT 1,
        failed_count INTEGER NOT NULL DEFAULT 0,
        locked_until TEXT DEFAULT '',
        last_login TEXT DEFAULT '',
        created_at TEXT DEFAULT ''
    )""",
    # ---- accounting
    # Chart of accounts. sys_key marks the accounts the app posts to on its own
    # (cash, bank, debtors, sales, GST...); those can be renamed, not removed.
    """CREATE TABLE IF NOT EXISTS accounts (
        id {PK},
        code TEXT DEFAULT '',
        name TEXT NOT NULL,
        type TEXT NOT NULL,
        grp TEXT NOT NULL DEFAULT '',
        sys_key TEXT DEFAULT '',
        active INTEGER NOT NULL DEFAULT 1,
        notes TEXT DEFAULT '',
        created_at TEXT DEFAULT ''
    )""",
    # Day-to-day running costs: rent, packing, delivery, salaries...
    """CREATE TABLE IF NOT EXISTS expenses (
        id {PK},
        exp_date TEXT NOT NULL,
        account_id INTEGER NOT NULL,
        party_id INTEGER,
        party_name TEXT DEFAULT '',
        ref_no TEXT DEFAULT '',
        description TEXT DEFAULT '',
        amount {NUM} NOT NULL DEFAULT 0,
        gst_rate {NUM} NOT NULL DEFAULT 0,
        interstate INTEGER NOT NULL DEFAULT 0,
        cgst {NUM} NOT NULL DEFAULT 0,
        sgst {NUM} NOT NULL DEFAULT 0,
        igst {NUM} NOT NULL DEFAULT 0,
        total {NUM} NOT NULL DEFAULT 0,
        payment_mode TEXT DEFAULT 'Cash',
        created_at TEXT DEFAULT '',
        updated_at TEXT DEFAULT '',
        edit_count INTEGER NOT NULL DEFAULT 0
    )""",
    # Money received or paid that is not a sale or purchase: a customer settling
    # udhaar, paying a supplier, capital put in, a loan, GST paid...
    """CREATE TABLE IF NOT EXISTS vouchers (
        id {PK},
        v_type TEXT NOT NULL,
        v_no TEXT DEFAULT '',
        v_date TEXT NOT NULL,
        account_id INTEGER NOT NULL,
        party_id INTEGER,
        party_name TEXT DEFAULT '',
        amount {NUM} NOT NULL DEFAULT 0,
        payment_mode TEXT DEFAULT 'Cash',
        ref_no TEXT DEFAULT '',
        notes TEXT DEFAULT '',
        created_at TEXT DEFAULT '',
        updated_at TEXT DEFAULT '',
        edit_count INTEGER NOT NULL DEFAULT 0
    )""",
    # Hand-written double entries, for anything the documents above do not cover.
    """CREATE TABLE IF NOT EXISTS journals (
        id {PK},
        j_no TEXT DEFAULT '',
        j_date TEXT NOT NULL,
        narration TEXT DEFAULT '',
        created_at TEXT DEFAULT '',
        updated_at TEXT DEFAULT '',
        edit_count INTEGER NOT NULL DEFAULT 0
    )""",
    """CREATE TABLE IF NOT EXISTS journal_lines (
        id {PK},
        journal_id INTEGER NOT NULL,
        account_id INTEGER NOT NULL,
        party_id INTEGER,
        party_name TEXT DEFAULT '',
        debit {NUM} NOT NULL DEFAULT 0,
        credit {NUM} NOT NULL DEFAULT 0,
        memo TEXT DEFAULT ''
    )""",
    "CREATE INDEX IF NOT EXISTS idx_jl_journal ON journal_lines(journal_id)",
    "CREATE INDEX IF NOT EXISTS idx_j_date ON journals(j_date)",
    "CREATE INDEX IF NOT EXISTS idx_e_date ON expenses(exp_date)",
    "CREATE INDEX IF NOT EXISTS idx_v_date ON vouchers(v_date)",
    "CREATE INDEX IF NOT EXISTS idx_pl_purchase ON purchase_lines(purchase_id)",
    "CREATE INDEX IF NOT EXISTS idx_prl_prod ON production_lines(production_id)",
    "CREATE INDEX IF NOT EXISTS idx_rl_product ON recipe_lines(product_id)",
    "CREATE INDEX IF NOT EXISTS idx_pr_date ON productions(prod_date)",
    "CREATE INDEX IF NOT EXISTS idx_sl_sale ON sale_lines(sale_id)",
    "CREATE INDEX IF NOT EXISTS idx_p_date ON purchases(bill_date)",
    "CREATE INDEX IF NOT EXISTS idx_s_date ON sales(invoice_date)",
]

# Enforced separately: on a database that somehow already holds a duplicate
# invoice number this cannot be created, and that must not stop the app booting.
UNIQUE_INVOICE_INDEX = "CREATE UNIQUE INDEX IF NOT EXISTS idx_s_invoice_no ON sales(invoice_no)"

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


# People allowed to sign in on a brand-new install. Only used while the users
# table is empty, so removing someone later from the Users page sticks.
SEED_USERS = [
    # email, name, role ('owner' can manage who has access)
    ("ramesh.goswami100@gmail.com", "Ramesh Goswami", "owner"),
    ("saurabhsethi9211@gmail.com", "Saurabh Sethi", "owner"),
]


# Columns added after the first release. CREATE TABLE IF NOT EXISTS leaves an
# existing table alone, so these are applied by hand on every start.
MIGRATIONS = [
    ("settings", "admin_hash", "TEXT DEFAULT ''"),
    ("settings", "admin_salt", "TEXT DEFAULT ''"),
    # random key that signs login sessions when SECRET_KEY is not set
    ("settings", "app_secret", "TEXT DEFAULT ''"),
    ("sales", "updated_at", "TEXT DEFAULT ''"),
    ("sales", "edit_count", "INTEGER NOT NULL DEFAULT 0"),
    ("purchases", "updated_at", "TEXT DEFAULT ''"),
    ("purchases", "edit_count", "INTEGER NOT NULL DEFAULT 0"),
    # 'product' = something you sell; 'raw' = an ingredient you buy and use up
    ("items", "kind", "TEXT NOT NULL DEFAULT 'product'"),
    # value that actually left stock with each sale line (cost_rate x qty, except
    # the line that empties a shelf takes whatever value was left on it)
    ("sale_lines", "cost", "DOUBLE PRECISION NOT NULL DEFAULT 0"),
    # how a production batch's packing/labour costs were paid
    ("productions", "other_cost_mode", "TEXT DEFAULT 'Cash'"),
    # date the opening stock is booked on in the accounts
    ("settings", "books_start", "TEXT DEFAULT ''"),
]

# Columns whose values are computed by the stock replay; adding one means the
# replay must run once so old rows are filled in.
NEEDS_REBUILD = {("sale_lines", "cost")}


def existing_columns(conn, table):
    if IS_PG:
        rows = conn.query(
            "SELECT column_name AS name FROM information_schema.columns WHERE table_name = ?",
            (table,))
    else:
        rows = conn.query("PRAGMA table_info({})".format(table))
    return {r["name"] for r in rows}


def migrate(conn):
    """Add any column a newer version expects but an older database lacks.
    Returns the (table, column) pairs that were added."""
    added = []
    by_table = {}
    for table, col, ddl in MIGRATIONS:
        by_table.setdefault(table, []).append((col, ddl))
    for table, cols in by_table.items():
        have = existing_columns(conn, table)
        for col, ddl in cols:
            if col not in have:
                conn.execute("ALTER TABLE {} ADD COLUMN {} {}".format(table, col, ddl))
                added.append((table, col))
    return added


def init_db():
    """Create and upgrade the schema. Returns True when the stock replay has to
    run once to fill in a newly added computed column."""
    with get_db() as conn:
        if IS_PG:   # gunicorn workers boot at the same moment; migrate one at a time
            conn.execute("SELECT pg_advisory_xact_lock(724100)")
        for stmt in SCHEMA:
            conn.execute(stmt.format(PK=PK, NUM=NUM))
        added = migrate(conn)
        row = conn.query_one("SELECT id FROM settings WHERE id = 1")
        if not row:
            conn.execute("INSERT INTO settings (id) VALUES (1)")
        if not conn.scalar("SELECT COUNT(*) AS c FROM users"):
            for email, name, role in SEED_USERS:
                conn.insert("users", dict(email=email, name=name, role=role, created_at=""))
        n = conn.scalar("SELECT COUNT(*) AS c FROM items")
        if not n:
            for name, hsn, rate, unit, low, srate in SEED_ITEMS:
                conn.insert("items", dict(
                    name=name, hsn=hsn, gst_rate=rate, unit=unit,
                    low_stock_qty=low, sale_rate=srate, created_at="",
                ))

    # Its own transaction: on a database that somehow already holds a duplicate
    # invoice number this cannot be created, and a failure here must not undo
    # the migration above or stop the app booting.
    try:
        with get_db() as conn:
            conn.execute(UNIQUE_INVOICE_INDEX)
    except Exception:
        pass
    return any(a in NEEDS_REBUILD for a in added)
