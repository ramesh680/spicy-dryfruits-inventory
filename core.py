"""Business logic: GST computation, stock valuation, invoice numbering."""
from datetime import date, datetime

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
    """Replay every purchase and sale in date order to recompute weighted-average
    cost, stock on hand and cost of goods sold. Called after any change so that
    edits and deletions can never leave the valuation drifting."""
    items = conn.query("SELECT id, opening_qty, opening_rate FROM items")
    state = {}
    for it in items:
        q = float(it["opening_qty"] or 0)
        r = float(it["opening_rate"] or 0)
        state[it["id"]] = [q, money(q * r)]

    events = []
    for p in conn.query("SELECT id, bill_date FROM purchases"):
        events.append((p["bill_date"], 0, p["id"], "P"))
    for s in conn.query("SELECT id, invoice_date FROM sales"):
        events.append((s["invoice_date"], 1, s["id"], "S"))
    events.sort(key=lambda e: (e[0], e[1], e[2]))

    for _, _, doc_id, kind in events:
        if kind == "P":
            for l in conn.query("SELECT item_id, qty, taxable FROM purchase_lines WHERE purchase_id = ?", (doc_id,)):
                st = state.setdefault(l["item_id"], [0.0, 0.0])
                st[0] = qty(st[0] + float(l["qty"]))
                st[1] = money(st[1] + float(l["taxable"]))
        else:
            cogs = 0.0
            for l in conn.query("SELECT id, item_id, qty, taxable FROM sale_lines WHERE sale_id = ?", (doc_id,)):
                st = state.setdefault(l["item_id"], [0.0, 0.0])
                cost_rate = money(st[1] / st[0]) if st[0] > 0.0001 else 0.0
                q = float(l["qty"])
                line_cost = money(cost_rate * q)
                st[0] = qty(st[0] - q)
                st[1] = money(st[1] - line_cost)
                if st[0] <= 0.0001:
                    # Nothing left on the shelf, so nothing left to value. A negative
                    # quantity is kept as-is so the Stock page can flag it.
                    if abs(st[0]) < 0.0001:
                        st[0] = 0.0
                    st[1] = 0.0
                cogs += line_cost
                conn.execute("UPDATE sale_lines SET cost_rate = ? WHERE id = ?", (cost_rate, l["id"]))
            conn.execute("UPDATE sales SET cogs = ? WHERE id = ?", (money(cogs), doc_id))

    for item_id, (q, v) in state.items():
        conn.execute("UPDATE items SET stock_qty = ?, stock_value = ? WHERE id = ?", (qty(q), money(v), item_id))


def stock_rows(conn):
    rows = conn.query(
        "SELECT id, name, hsn, unit, gst_rate, sale_rate, low_stock_qty, stock_qty, stock_value "
        "FROM items WHERE active = 1 ORDER BY name")
    for r in rows:
        q = float(r["stock_qty"] or 0)
        v = float(r["stock_value"] or 0)
        r["avg_cost"] = money(v / q) if q > 0.0001 else 0.0
        r["low"] = q <= float(r["low_stock_qty"] or 0)
        r["stock_qty"] = qty(q)
        r["stock_value"] = money(v)
    return rows


# ------------------------------------------------------ invoice numbering

def next_invoice_no(conn, invoice_date, prefix):
    fy = fy_of(invoice_date)
    start, end = fy_bounds(fy)
    pattern = "{}/{}/".format(prefix, fy)
    rows = conn.query(
        "SELECT invoice_no FROM sales WHERE invoice_date >= ? AND invoice_date <= ?", (start, end))
    highest = 0
    for r in rows:
        no = r["invoice_no"] or ""
        if no.startswith(pattern):
            tail = no[len(pattern):]
            if tail.isdigit():
                highest = max(highest, int(tail))
    return "{}{:04d}".format(pattern, highest + 1)


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
