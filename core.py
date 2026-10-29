"""Business logic: GST computation, stock valuation, invoice numbering."""
import hashlib
import hmac
import os
import re
import secrets
from datetime import date, datetime, timedelta

from db import get_db

# GST state codes (as printed on a GSTIN's first two digits)
STATE_CODES = [
    ("01", "Jammu & Kashmir"), ("02", "Himachal Pradesh"), ("03", "Punjab"),
    ("04", "Chandigarh"), ("05", "Uttarakhand"), ("06", "Haryana"),
    ("07", "Delhi"), ("08", "Rajasthan"), ("09", "Uttar Pradesh"),
    ("10", "Bihar"), ("11", "Sikkim"), ("12", "Arunachal Pradesh"),
    ("13", "Nagaland"), ("14", "Manipur"), ("15", "Mizoram"),
    ("16", "Tripura"), ("17", "Meghalaya"), ("18", "Assam"),
    ("19", "West Bengal"), ("20", "Jharkhand"), ("21", "Odisha"),
    ("22", "Chhattisgarh"), ("23", "Madhya Pradesh"), ("24", "Gujarat"),
    ("26", "Dadra & Nagar Haveli and Daman & Diu"), ("27", "Maharashtra"),
    ("29", "Karnataka"), ("30", "Goa"), ("31", "Lakshadweep"),
    ("32", "Kerala"), ("33", "Tamil Nadu"), ("34", "Puducherry"),
    ("35", "Andaman & Nicobar Islands"), ("36", "Telangana"),
    ("37", "Andhra Pradesh"), ("38", "Ladakh"), ("97", "Other Territory"),
]
STATE_NAME = dict(STATE_CODES)

GST_RATES = [0, 5, 12, 18, 28, 40]
UNITS = ["kg", "g", "pcs", "box", "packet", "litre"]
PAYMENT_MODES = ["Cash", "UPI", "Bank Transfer", "Card", "Credit (Udhaar)"]
# Ways money actually moves (no credit) - for receipts, payments and expenses paid now.
MONEY_MODES = ["Cash", "UPI", "Bank Transfer", "Card"]


def money(x):
    """Round to 2 decimals the way invoices do (half-up, not banker's rounding)."""
    try:
        v = float(x)
    except (TypeError, ValueError):
        return 0.0
    return float(int(abs(v) * 100 + 0.5)) / 100 * (1 if v >= 0 else -1)


def qty(x):
    try:
        return round(float(x), 3)
    except (TypeError, ValueError):
        return 0.0


def today_str():
    return date.today().isoformat()


def fy_of(d):
    """Indian financial year label for a date string, e.g. '2026-27'."""
    dt = datetime.strptime(d, "%Y-%m-%d").date()
    start = dt.year if dt.month >= 4 else dt.year - 1
    return "{}-{}".format(start, str(start + 1)[-2:])


def fy_bounds(fy_label):
    start_year = int(fy_label.split("-")[0])
    return "{}-04-01".format(start_year), "{}-03-31".format(start_year + 1)


# ---------------------------------------------------------------- GST maths

def compute_line(qty_, rate, discount_pct, gst_rate, interstate, gst_enabled=True):
    """Return the tax breakup for one invoice line.

    Taxable value = qty x rate, less line discount. GST is charged on that
    taxable value: split evenly into CGST+SGST within the state, charged as
    IGST across state lines.
    """
    gross = float(qty_) * float(rate)
    disc = gross * (float(discount_pct) / 100.0)
    taxable = money(gross - disc)
    r = float(gst_rate) if gst_enabled else 0.0
    if not gst_enabled or r <= 0:
        cgst = sgst = igst = 0.0
    elif interstate:
        igst = money(taxable * r / 100.0)
        cgst = sgst = 0.0
    else:
        cgst = money(taxable * r / 200.0)
        sgst = money(taxable * r / 200.0)
        igst = 0.0
    return dict(
        qty=qty(qty_), rate=money(rate), discount_pct=float(discount_pct or 0),
        taxable=taxable, gst_rate=r, cgst=cgst, sgst=sgst, igst=igst,
        total=money(taxable + cgst + sgst + igst),
    )


def totals_from_lines(lines):
    taxable = money(sum(l["taxable"] for l in lines))
    cgst = money(sum(l["cgst"] for l in lines))
    sgst = money(sum(l["sgst"] for l in lines))
    igst = money(sum(l["igst"] for l in lines))
    raw = taxable + cgst + sgst + igst
    total = float(int(raw + 0.5)) if raw >= 0 else float(int(raw - 0.5))
    return dict(taxable=taxable, cgst=cgst, sgst=sgst, igst=igst,
                round_off=money(total - raw), total=money(total))


def rate_wise_breakup(lines):
    """Group lines by GST rate - the shape a GSTR-1/3B summary wants."""
    buckets = {}
    for l in lines:
        r = float(l["gst_rate"])
        b = buckets.setdefault(r, dict(gst_rate=r, taxable=0.0, cgst=0.0, sgst=0.0, igst=0.0))
        b["taxable"] += float(l["taxable"])
        b["cgst"] += float(l["cgst"])
        b["sgst"] += float(l["sgst"])
        b["igst"] += float(l["igst"])
    out = []
    for r in sorted(buckets):
        b = buckets[r]
        for k in ("taxable", "cgst", "sgst", "igst"):
            b[k] = money(b[k])
        b["tax"] = money(b["cgst"] + b["sgst"] + b["igst"])
        out.append(b)
    return out


# ------------------------------------------------------- stock & valuation

def rebuild_stock(conn):
    """Replay every purchase, production batch and sale in date order to
    recompute weighted-average cost, stock on hand and cost of goods sold.
    Called after any change so that edits and deletions can never leave the
    valuation drifting.

    Everything is read in a handful of queries and only rows whose figures
    actually changed are written back, in batches - so the cost of a save no
    longer grows with one database round trip per bill ever recorded."""
    items = conn.query("SELECT id, opening_qty, opening_rate, stock_qty, stock_value FROM items")
    state = {}
    for it in items:
        q = float(it["opening_qty"] or 0)
        r = float(it["opening_rate"] or 0)
        state[it["id"]] = [q, money(q * r)]

    def grouped(sql, key):
        out = {}
        for row in conn.query(sql):
            out.setdefault(row[key], []).append(row)
        return out

    p_lines = grouped("SELECT purchase_id, item_id, qty, taxable FROM purchase_lines ORDER BY id", "purchase_id")
    s_lines = grouped("SELECT id, sale_id, item_id, qty, cost_rate, cost FROM sale_lines ORDER BY id", "sale_id")
    m_lines = grouped("SELECT id, production_id, raw_id, qty, cost_rate, cost FROM production_lines ORDER BY id",
                      "production_id")
    sales = {r["id"]: r for r in conn.query("SELECT id, invoice_date, cogs FROM sales")}
    prods = {r["id"]: r for r in conn.query(
        "SELECT id, prod_date, product_id, qty_out, other_cost, raw_cost, total_cost FROM productions")}

    events = [(p["bill_date"], 0, p["id"], "P") for p in conn.query("SELECT id, bill_date FROM purchases")]
    # Same day: purchases land first, then production, then sales - so a batch
    # can use what was bought that morning and its output can be sold that day.
    events += [(m["prod_date"], 1, m["id"], "M") for m in prods.values()]
    events += [(s["invoice_date"], 2, s["id"], "S") for s in sales.values()]
    events.sort(key=lambda e: (e[0], e[1], e[2]))

    upd_sl, upd_s, upd_ml, upd_m = [], [], [], []
    for _, _, doc_id, kind in events:
        if kind == "P":
            for l in p_lines.get(doc_id, ()):
                st = state.setdefault(l["item_id"], [0.0, 0.0])
                st[0] = qty(st[0] + float(l["qty"]))
                st[1] = money(st[1] + float(l["taxable"]))
        elif kind == "M":
            doc = prods[doc_id]
            raw_cost = 0.0
            for l in m_lines.get(doc_id, ()):
                st = state.setdefault(l["raw_id"], [0.0, 0.0])
                rate, cost = _take(st, float(l["qty"]))
                raw_cost += cost
                if _changed(l["cost_rate"], rate) or _changed(l["cost"], cost):
                    upd_ml.append((rate, cost, l["id"]))
            raw_cost = money(raw_cost)
            total = money(raw_cost + float(doc["other_cost"] or 0))
            out = state.setdefault(doc["product_id"], [0.0, 0.0])
            out[0] = qty(out[0] + float(doc["qty_out"]))
            out[1] = money(out[1] + total)
            if _changed(doc["raw_cost"], raw_cost) or _changed(doc["total_cost"], total):
                upd_m.append((raw_cost, total, doc_id))
        else:
            cogs = 0.0
            for l in s_lines.get(doc_id, ()):
                st = state.setdefault(l["item_id"], [0.0, 0.0])
                rate, cost = _take(st, float(l["qty"]))
                cogs += cost
                if _changed(l["cost_rate"], rate) or _changed(l.get("cost"), cost):
                    upd_sl.append((rate, cost, l["id"]))
            cogs = money(cogs)
            if _changed(sales[doc_id]["cogs"], cogs):
                upd_s.append((cogs, doc_id))

    conn.executemany("UPDATE sale_lines SET cost_rate = ?, cost = ? WHERE id = ?", upd_sl)
    conn.executemany("UPDATE sales SET cogs = ? WHERE id = ?", upd_s)
    conn.executemany("UPDATE production_lines SET cost_rate = ?, cost = ? WHERE id = ?", upd_ml)
    conn.executemany("UPDATE productions SET raw_cost = ?, total_cost = ? WHERE id = ?", upd_m)
    current = {it["id"]: it for it in items}
    upd_i = []
    for item_id, (q, v) in sorted(state.items()):
        it = current.get(item_id)
        if it is None:
            continue
        if _changed(it["stock_qty"], qty(q), 0.0005) or _changed(it["stock_value"], money(v)):
            upd_i.append((qty(q), money(v), item_id))
    conn.executemany("UPDATE items SET stock_qty = ?, stock_value = ? WHERE id = ?", upd_i)


def _changed(old, new, tol=0.0049):
    try:
        return old is None or abs(float(old) - float(new)) > tol
    except (TypeError, ValueError):
        return True


def _take(st, q):
    """Remove q units from a [qty, value] pair at its weighted-average cost and
    return (cost_rate, cost). Shared by sales and production.

    The line that empties a shelf takes whatever value is left on it - the
    pennies weighted-average rounding leaves behind, or, when more is sold than
    is on hand, the value of what really was there. So value only ever moves,
    it is never created or lost, and the inventory in the accounts always
    equals the stock valuation."""
    cost_rate = money(st[1] / st[0]) if st[0] > 0.0001 else 0.0
    line_cost = money(cost_rate * q)
    remaining = qty(st[0] - q)
    if remaining <= 0.0001:
        line_cost = money(st[1])
        st[0] = 0.0 if abs(remaining) < 0.0001 else remaining
        st[1] = 0.0
    else:
        line_cost = min(line_cost, money(st[1]))
        st[0] = remaining
        st[1] = money(st[1] - line_cost)
    return cost_rate, line_cost


# --------------------------------------------------- raw materials & recipes

def recipe_for(conn, product_id):
    return conn.query(
        "SELECT r.raw_id, r.qty_per_unit, i.name, i.unit, i.stock_qty FROM recipe_lines r "
        "JOIN items i ON i.id = r.raw_id WHERE r.product_id = ? ORDER BY i.name", (product_id,))


def recipes_json(conn):
    """{product_id: [{raw_id, qty_per_unit}, ...]} for the production form."""
    out = {}
    for r in conn.query("SELECT product_id, raw_id, qty_per_unit FROM recipe_lines ORDER BY id"):
        out.setdefault(str(r["product_id"]), []).append(
            dict(raw_id=r["raw_id"], qty_per_unit=float(r["qty_per_unit"])))
    return out


def can_make(conn, product_id):
    """How much of a product today's raw stock is enough for, and which
    ingredient runs out first. None when the product has no recipe."""
    lines = recipe_for(conn, product_id)
    lines = [l for l in lines if float(l["qty_per_unit"]) > 0]
    if not lines:
        return None
    best, limit = None, None
    for l in lines:
        n = max(0.0, float(l["stock_qty"] or 0)) / float(l["qty_per_unit"])
        if best is None or n < best:
            best, limit = n, l["name"]
    return dict(qty=qty(best), limited_by=limit)


def raw_material_rows(conn):
    """For every raw material: bought, used (in total and per product), left."""
    raws = conn.query(
        "SELECT id, name, unit, low_stock_qty, opening_qty, stock_qty, stock_value "
        "FROM items WHERE kind = 'raw' AND active = 1 ORDER BY name")
    bought_by = {r["item_id"]: r for r in conn.query(
        "SELECT item_id, COALESCE(SUM(qty),0) AS q, COALESCE(SUM(taxable),0) AS v "
        "FROM purchase_lines GROUP BY item_id")}
    used_by = {}
    for u in conn.query(
            "SELECT l.raw_id, p.product_id, p.product_name, COALESCE(SUM(l.qty),0) AS q, "
            "COALESCE(SUM(l.cost),0) AS v, COUNT(DISTINCT p.id) AS batches "
            "FROM production_lines l JOIN productions p ON p.id = l.production_id "
            "GROUP BY l.raw_id, p.product_id, p.product_name ORDER BY p.product_name"):
        used_by.setdefault(u["raw_id"], []).append(u)
    out = []
    for r in raws:
        bought = bought_by.get(r["id"]) or dict(q=0, v=0)
        per_product = [dict(product_id=u["product_id"], product_name=u["product_name"],
                            q=qty(u["q"]), v=money(u["v"]), batches=u["batches"])
                       for u in used_by.get(r["id"], [])]
        used = sum(float(u["q"]) for u in per_product)
        opening = float(r["opening_qty"] or 0)
        received = opening + float(bought["q"])
        left = float(r["stock_qty"] or 0)
        out.append(dict(
            id=r["id"], name=r["name"], unit=r["unit"],
            opening=qty(opening), bought=qty(bought["q"]), bought_value=money(bought["v"]),
            received=qty(received), used=qty(used), left=qty(left),
            left_value=money(r["stock_value"]),
            avg_cost=money(float(r["stock_value"]) / left) if left > 0.0001 else 0.0,
            used_pct=round(used / received * 100, 1) if received > 0.0001 else 0.0,
            low=left <= float(r["low_stock_qty"] or 0),
            per_product=per_product))
    return out


def stock_rows(conn):
    rows = conn.query(
        "SELECT id, name, hsn, unit, gst_rate, sale_rate, low_stock_qty, stock_qty, stock_value, kind "
        "FROM items WHERE active = 1 ORDER BY name")
    for r in rows:
        q = float(r["stock_qty"] or 0)
        v = float(r["stock_value"] or 0)
        r["avg_cost"] = money(v / q) if q > 0.0001 else 0.0
        r["low"] = q <= float(r["low_stock_qty"] or 0)
        r["stock_qty"] = qty(q)
        r["stock_value"] = money(v)
        r["kind"] = r.get("kind") or "product"
    return rows


# ------------------------------------------------------ invoice numbering

def next_invoice_no(conn, invoice_date, prefix):
    """Next number in the financial year the date falls in.

    Numbering restarts each April and must not repeat, so this also skips over
    any number already taken - two tills saving at the same moment, or a
    back-dated entry, must never produce a duplicate.
    """
    fy = fy_of(invoice_date)
    pattern = "{}/{}/".format(prefix, fy)
    taken = {r["invoice_no"] for r in conn.query(
        "SELECT invoice_no FROM sales WHERE invoice_no LIKE ?", (pattern + "%",))}
    highest = 0
    for no in taken:
        if no and no.startswith(pattern):
            tail = no[len(pattern):]
            if tail.isdigit():
                highest = max(highest, int(tail))
    nxt = highest + 1
    while "{}{:04d}".format(pattern, nxt) in taken:
        nxt += 1
    return "{}{:04d}".format(pattern, nxt)


# ----------------------------------------------------- amount in words (INR)

_ONES = ["", "One", "Two", "Three", "Four", "Five", "Six", "Seven", "Eight", "Nine",
         "Ten", "Eleven", "Twelve", "Thirteen", "Fourteen", "Fifteen", "Sixteen",
         "Seventeen", "Eighteen", "Nineteen"]
_TENS = ["", "", "Twenty", "Thirty", "Forty", "Fifty", "Sixty", "Seventy", "Eighty", "Ninety"]


def _two(n):
    if n < 20:
        return _ONES[n]
    return (_TENS[n // 10] + (" " + _ONES[n % 10] if n % 10 else "")).strip()


def _three(n):
    if n < 100:
        return _two(n)
    return (_ONES[n // 100] + " Hundred" + (" " + _two(n % 100) if n % 100 else "")).strip()


def amount_in_words(amount):
    """Indian numbering: crore / lakh / thousand."""
    amount = money(amount)
    rupees = int(amount)
    paise = int(round((amount - rupees) * 100))
    if rupees == 0:
        words = "Zero"
    else:
        parts = []
        crore, rupees_r = divmod(rupees, 10000000)
        lakh, rupees_r = divmod(rupees_r, 100000)
        thousand, rupees_r = divmod(rupees_r, 1000)
        if crore:
            parts.append(_three(crore) + " Crore")
        if lakh:
            parts.append(_two(lakh) + " Lakh")
        if thousand:
            parts.append(_two(thousand) + " Thousand")
        if rupees_r:
            parts.append(_three(rupees_r))
        words = " ".join(parts)
    out = "Rupees " + words
    if paise:
        out += " and " + _two(paise) + " Paise"
    return out + " Only"


# ------------------------------------------------------------ admin access

PIN_ROUNDS = 240000


def make_pin(pin):
    """Hash an admin PIN. The PIN itself is never stored."""
    salt = os.urandom(16).hex()
    return hash_pin(pin, salt), salt


def hash_pin(pin, salt):
    return hashlib.pbkdf2_hmac("sha256", pin.encode("utf-8"),
                               bytes.fromhex(salt), PIN_ROUNDS).hex()


def check_pin(pin, stored_hash, salt):
    """Constant-time check, so a wrong PIN cannot be found by timing."""
    if not pin or not stored_hash or not salt:
        return False
    try:
        candidate = hash_pin(pin, salt)
    except ValueError:
        return False
    return hmac.compare_digest(candidate, stored_hash)


# ------------------------------------------------------------ sign-in

def normalize_email(email):
    return (email or "").strip().lower()


def valid_email(email):
    return bool(re.match(r"^[^@\s]+@[^@\s]+\.[^@\s]+$", email or ""))


def new_token():
    """A one-time link token. Only its SHA-256 is stored."""
    token = secrets.token_urlsafe(32)
    return token, token_hash(token)


def token_hash(token):
    return hashlib.sha256((token or "").encode("utf-8")).hexdigest()


def now_iso():
    return datetime.utcnow().replace(microsecond=0).isoformat()


def iso_after(**delta):
    return (datetime.utcnow() + timedelta(**delta)).replace(microsecond=0).isoformat()
