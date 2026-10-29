"""Double-entry books, derived from the documents.

Nothing is typed twice. Every sale, purchase, production batch, expense,
receipt, payment and hand-written journal is turned into balanced debit and
credit postings when the books are read. The journal, ledgers, trial balance,
profit & loss and balance sheet are therefore always exactly in step with the
registers: add, edit or delete any document and every statement moves with it,
with no second copy that could drift.

How each document posts
-----------------------
Sale        Dr Cash / Bank / Debtors (invoice total)
            Cr Sales (taxable)  Cr Output CGST/SGST/IGST  Dr/Cr Round off
            Dr Cost of goods sold   Cr Stock (value that left stock)
Purchase    Dr Stock (taxable)  Dr Input CGST/SGST/IGST  Dr/Cr Round off
            Cr Cash / Bank / Creditors (bill total)
Production  Dr Finished goods (batch cost)  Cr Raw materials (cost used)
            Cr Cash / Bank / Creditors (packing, labour and other costs)
Expense     Dr expense head  Dr Input GST   Cr Cash / Bank / Creditors
Receipt     Dr Cash / Bank   Cr Debtors (or capital, loan, other income...)
Payment     Dr Creditors (or drawings, rent, GST...)   Cr Cash / Bank
Opening     Dr Stock   Cr Opening balance equity (opening stock on Items)
"""
from datetime import date, datetime, timedelta

from core import fy_bounds, fy_of, money, today_str

TYPES = [("asset", "Assets"), ("liability", "Liabilities"), ("equity", "Capital"),
         ("income", "Income"), ("expense", "Expenses")]
TYPE_NAME = dict(TYPES)
DEBIT_NORMAL = {"asset", "expense"}

# Groups, in the order statements print them.
GROUPS = {
    "asset": ["Fixed Assets", "Cash & Bank", "Receivables", "Inventory",
              "Duties & Taxes (Input)", "Loans & Advances", "Other Current Assets"],
    "liability": ["Loans", "Payables", "Duties & Taxes", "Other Current Liabilities"],
    "equity": ["Capital"],
    "income": ["Sales", "Indirect Income"],
    "expense": ["Direct Expenses", "Indirect Expenses"],
}

# Accounts the app posts to on its own. They can be renamed, never removed.
SYSTEM_ACCOUNTS = [
    # sys_key, code, name, type, group
    ("cash", "1001", "Cash in Hand", "asset", "Cash & Bank"),
    ("bank", "1002", "Bank Account", "asset", "Cash & Bank"),
    ("debtors", "1101", "Sundry Debtors (Customers)", "asset", "Receivables"),
    ("inv_fg", "1201", "Stock - Finished Goods", "asset", "Inventory"),
    ("inv_rm", "1202", "Stock - Raw Materials", "asset", "Inventory"),
    ("itc_cgst", "1301", "Input CGST", "asset", "Duties & Taxes (Input)"),
    ("itc_sgst", "1302", "Input SGST", "asset", "Duties & Taxes (Input)"),
    ("itc_igst", "1303", "Input IGST", "asset", "Duties & Taxes (Input)"),
    ("creditors", "2001", "Sundry Creditors (Suppliers)", "liability", "Payables"),
    ("out_cgst", "2101", "Output CGST", "liability", "Duties & Taxes"),
    ("out_sgst", "2102", "Output SGST", "liability", "Duties & Taxes"),
    ("out_igst", "2103", "Output IGST", "liability", "Duties & Taxes"),
    ("capital", "3001", "Owner's Capital", "equity", "Capital"),
    ("drawings", "3002", "Drawings", "equity", "Capital"),
    ("opening", "3003", "Opening Balance Equity", "equity", "Capital"),
    ("sales", "4001", "Sales", "income", "Sales"),
    ("other_income", "4101", "Other Income", "income", "Indirect Income"),
    ("cogs", "5001", "Cost of Goods Sold", "expense", "Direct Expenses"),
    ("round_off", "5901", "Round Off", "expense", "Indirect Expenses"),
]

# Starter heads, seeded once on a new set of books; edit or archive freely.
STARTER_ACCOUNTS = [
    ("1501", "Furniture & Equipment", "asset", "Fixed Assets"),
    ("1401", "Advances & Deposits", "asset", "Loans & Advances"),
    ("2201", "Loans", "liability", "Loans"),
    ("5101", "Packing Materials", "expense", "Direct Expenses"),
    ("5102", "Freight Inward", "expense", "Direct Expenses"),
    ("5201", "Rent", "expense", "Indirect Expenses"),
    ("5202", "Electricity & Gas", "expense", "Indirect Expenses"),
    ("5203", "Salaries & Wages", "expense", "Indirect Expenses"),
    ("5204", "Delivery & Courier", "expense", "Indirect Expenses"),
    ("5205", "Marketing & Advertising", "expense", "Indirect Expenses"),
    ("5206", "Bank & Payment Charges", "expense", "Indirect Expenses"),
    ("5207", "Telephone & Internet", "expense", "Indirect Expenses"),
    ("5208", "Repairs & Maintenance", "expense", "Indirect Expenses"),
    ("5209", "Website & Software", "expense", "Indirect Expenses"),
    ("5299", "Miscellaneous Expenses", "expense", "Indirect Expenses"),
]

BANK_MODES = {"UPI", "Bank Transfer", "Card"}
CREDIT_MODE = "Credit (Udhaar)"
PARTY_KEYS = ("debtors", "creditors")


# ------------------------------------------------------------ chart of accounts

def ensure_accounts(conn):
    """Create any system account that is missing; seed the starter heads the
    first time only, so an archived or renamed head stays that way."""
    have = {r["sys_key"] for r in conn.query("SELECT sys_key FROM accounts WHERE sys_key <> ''")}
    first_time = not conn.scalar("SELECT COUNT(*) AS c FROM accounts")
    for key, code, name, typ, grp in SYSTEM_ACCOUNTS:
        if key not in have:
            conn.insert("accounts", dict(code=code, name=name, type=typ, grp=grp, sys_key=key,
                                         created_at=today_str()))
    if first_time:
        for code, name, typ, grp in STARTER_ACCOUNTS:
            conn.insert("accounts", dict(code=code, name=name, type=typ, grp=grp, sys_key="",
                                         created_at=today_str()))


def all_accounts(conn):
    rows = conn.query("SELECT * FROM accounts ORDER BY code, name")
    order = {t: i for i, (t, _) in enumerate(TYPES)}
    for r in rows:
        r["sys_key"] = r.get("sys_key") or ""
        r["grp"] = r.get("grp") or GROUPS[r["type"]][-1]
    rows.sort(key=lambda r: (order.get(r["type"], 9), _grp_index(r), r["code"] or "", r["name"]))
    return rows


def _grp_index(a):
    groups = GROUPS.get(a["type"], [])
    return groups.index(a["grp"]) if a["grp"] in groups else len(groups)


def key_ids(conn):
    return {r["sys_key"]: r["id"] for r in conn.query("SELECT id, sys_key FROM accounts WHERE sys_key <> ''")}


def mode_key(mode, credit_key):
    """Which account a payment mode lands in."""
    if mode == CREDIT_MODE:
        return credit_key
    if mode in BANK_MODES:
        return "bank"
    return "cash"


# ------------------------------------------------------------------ postings

def opening_date(conn):
    """The opening stock is booked on the books' start date (Settings), or else
    on the earliest date anything was recorded."""
    s = conn.query_one("SELECT books_start FROM settings WHERE id = 1") or {}
    earliest = conn.scalar(
        "SELECT MIN(d) AS d FROM ("
        " SELECT MIN(invoice_date) AS d FROM sales UNION ALL"
        " SELECT MIN(bill_date) FROM purchases UNION ALL"
        " SELECT MIN(prod_date) FROM productions UNION ALL"
        " SELECT MIN(exp_date) FROM expenses UNION ALL"
        " SELECT MIN(v_date) FROM vouchers UNION ALL"
        " SELECT MIN(j_date) FROM journals) x", default=None)
    start = (s.get("books_start") or "").strip()
    candidates = [d for d in (start, earliest) if d]
    return min(candidates) if candidates else today_str()


def _between(col, start, end):
    sql, params = [], []
    if start:
        sql.append("{} >= ?".format(col))
        params.append(start)
    if end:
        sql.append("{} <= ?".format(col))
        params.append(end)
    return (" WHERE " + " AND ".join(sql)) if sql else "", params


class Entry(dict):
    """One balanced journal entry: date, kind, ref, narration, link, lines.
    Each line is a dict(acc, dr, cr, party_id, party_name)."""

    def add(self, acc, dr=0.0, cr=0.0, party_id=None, party_name=""):
        dr, cr = money(dr), money(cr)
        if acc is None or (abs(dr) < 0.005 and abs(cr) < 0.005):
            return
        # a negative debit is a credit, and the other way round
        if dr < 0:
            dr, cr = 0.0, money(cr - dr)
        if cr < 0:
            dr, cr = money(dr - cr), 0.0
        self["lines"].append(dict(acc=acc, dr=dr, cr=cr, party_id=party_id,
                                  party_name=party_name or ""))


def _entry(day, kind, ref, narration, link, doc_id):
    return Entry(date=day, kind=kind, ref=ref, narration=narration, link=link,
                 doc_id=doc_id, lines=[])


def build(conn, start=None, end=None):
    """All journal entries dated within [start, end] (either may be None),
    generated from the documents, in date order."""
    K = key_ids(conn)
    kinds = {i["id"]: (i.get("kind") or "product") for i in conn.query("SELECT id, kind FROM items")}

    def inv(item_id):
        return K["inv_rm"] if kinds.get(item_id) == "raw" else K["inv_fg"]

    def lines_by(sql, params, key):
        out = {}
        for r in conn.query(sql, params):
            out.setdefault(r[key], []).append(r)
        return out

    out = []

    # Opening stock, as entered on the Items page.
    od = opening_date(conn)
    if (not start or od >= start) and (not end or od <= end):
        e = _entry(od, "opening", "Opening", "Opening stock brought into the books",
                   ("items", {}), None)
        by_acc = {}
        for it in conn.query("SELECT id, opening_qty, opening_rate FROM items"):
            v = money(float(it["opening_qty"] or 0) * float(it["opening_rate"] or 0))
            if v:
                by_acc[inv(it["id"])] = by_acc.get(inv(it["id"]), 0.0) + v
        total = 0.0
        for acc, v in by_acc.items():
            e.add(acc, dr=v)
            total += v
        e.add(K["opening"], cr=total)
        if e["lines"]:
            out.append(e)

    # Sales
    w, p = _between("s.invoice_date", start, end)
    sale_lines = lines_by("SELECT sl.sale_id, sl.item_id, sl.cost FROM sale_lines sl "
                          "JOIN sales s ON s.id = sl.sale_id" + w, p, "sale_id")
    w, p = _between("invoice_date", start, end)
    for s in conn.query("SELECT * FROM sales" + w, p):
        e = _entry(s["invoice_date"], "sale", s["invoice_no"],
                   "Sale to " + (s["party_name"] or "walk-in customer") + " (" + (s["payment_mode"] or "Cash") + ")",
                   ("invoice", {"sid": s["id"]}), s["id"])
        recv = K[mode_key(s["payment_mode"], "debtors")]
        party = (s["party_id"], s["party_name"]) if recv == K["debtors"] else (None, "")
        e.add(recv, dr=s["total"], party_id=party[0], party_name=party[1])
        e.add(K["sales"], cr=s["taxable"])
        e.add(K["out_cgst"], cr=s["cgst"])
        e.add(K["out_sgst"], cr=s["sgst"])
        e.add(K["out_igst"], cr=s["igst"])
        e.add(K["round_off"], cr=s["round_off"])
        cost_by = {}
        for l in sale_lines.get(s["id"], ()):
            cost_by[inv(l["item_id"])] = cost_by.get(inv(l["item_id"]), 0.0) + float(l["cost"] or 0)
        cogs = money(sum(cost_by.values()))
        e.add(K["cogs"], dr=cogs)
        for acc, v in cost_by.items():
            e.add(acc, cr=v)
        out.append(e)

    # Purchases
    w, p = _between("pu.bill_date", start, end)
    purch_lines = lines_by("SELECT pl.purchase_id, pl.item_id, pl.taxable FROM purchase_lines pl "
                           "JOIN purchases pu ON pu.id = pl.purchase_id" + w, p, "purchase_id")
    w, p = _between("bill_date", start, end)
    for b in conn.query("SELECT * FROM purchases" + w, p):
        e = _entry(b["bill_date"], "purchase", b["bill_no"] or "Bill #" + str(b["id"]),
                   "Purchase from " + (b["party_name"] or "supplier") + " (" + (b["payment_mode"] or "Cash") + ")",
                   ("purchase_view", {"pid": b["id"]}), b["id"])
        by_acc = {}
        for l in purch_lines.get(b["id"], ()):
            by_acc[inv(l["item_id"])] = by_acc.get(inv(l["item_id"]), 0.0) + float(l["taxable"] or 0)
        for acc, v in by_acc.items():
            e.add(acc, dr=v)
        e.add(K["itc_cgst"], dr=b["cgst"])
        e.add(K["itc_sgst"], dr=b["sgst"])
        e.add(K["itc_igst"], dr=b["igst"])
        e.add(K["round_off"], dr=b["round_off"])
        pay = K[mode_key(b["payment_mode"], "creditors")]
        party = (b["party_id"], b["party_name"]) if pay == K["creditors"] else (None, "")
        e.add(pay, cr=b["total"], party_id=party[0], party_name=party[1])
        out.append(e)

    # Production batches
    w, p = _between("pr.prod_date", start, end)
    prod_lines = lines_by("SELECT l.production_id, l.raw_id, l.cost FROM production_lines l "
                          "JOIN productions pr ON pr.id = l.production_id" + w, p, "production_id")
    w, p = _between("prod_date", start, end)
    for m in conn.query("SELECT * FROM productions" + w, p):
        e = _entry(m["prod_date"], "production", m["batch_no"] or "Batch #" + str(m["id"]),
                   "Made {} of {}".format(_q(m["qty_out"]), m["product_name"]),
                   ("production_view", {"pid": m["id"]}), m["id"])
        e.add(inv(m["product_id"]), dr=m["total_cost"])
        by_acc = {}
        for l in prod_lines.get(m["id"], ()):
            by_acc[inv(l["raw_id"])] = by_acc.get(inv(l["raw_id"]), 0.0) + float(l["cost"] or 0)
        for acc, v in by_acc.items():
            e.add(acc, cr=v)
        e.add(K[mode_key(m.get("other_cost_mode") or "Cash", "creditors")], cr=m["other_cost"])
        out.append(e)

    # Expenses
    w, p = _between("exp_date", start, end)
    names = {r["id"]: r["name"] for r in conn.query("SELECT id, name FROM accounts")}
    for x in conn.query("SELECT * FROM expenses" + w, p):
        head = names.get(x["account_id"], "Expense")
        e = _entry(x["exp_date"], "expense", x["ref_no"] or "EXP-" + str(x["id"]),
                   head + (": " + x["description"] if x["description"] else "")
                   + (" - " + x["party_name"] if x["party_name"] else ""),
                   ("expense_view", {"eid": x["id"]}), x["id"])
        e.add(x["account_id"], dr=x["amount"])
        e.add(K["itc_cgst"], dr=x["cgst"])
        e.add(K["itc_sgst"], dr=x["sgst"])
        e.add(K["itc_igst"], dr=x["igst"])
        pay = K[mode_key(x["payment_mode"], "creditors")]
        party = (x["party_id"], x["party_name"]) if pay == K["creditors"] else (None, "")
        e.add(pay, cr=x["total"], party_id=party[0], party_name=party[1])
        out.append(e)

    # Receipts and payments
    w, p = _between("v_date", start, end)
    for v in conn.query("SELECT * FROM vouchers" + w, p):
        money_acc = K[mode_key(v["payment_mode"], "cash")]
        receipt = v["v_type"] == "receipt"
        e = _entry(v["v_date"], v["v_type"], v["v_no"] or ("RCT" if receipt else "PMT") + "-" + str(v["id"]),
                   ("Received from " if receipt else "Paid to ")
                   + (v["party_name"] or names.get(v["account_id"], "")) + " (" + (v["payment_mode"] or "Cash") + ")"
                   + (" - " + v["notes"] if v["notes"] else ""),
                   ("voucher_view", {"vid": v["id"]}), v["id"])
        party = (v["party_id"], v["party_name"])
        if receipt:
            e.add(money_acc, dr=v["amount"])
            e.add(v["account_id"], cr=v["amount"], party_id=party[0], party_name=party[1])
        else:
            e.add(v["account_id"], dr=v["amount"], party_id=party[0], party_name=party[1])
            e.add(money_acc, cr=v["amount"])
        out.append(e)

    # Hand-written journals
    w, p = _between("j.j_date", start, end)
    jl = lines_by("SELECT l.* FROM journal_lines l JOIN journals j ON j.id = l.journal_id" + w
                  + " ORDER BY l.id", p, "journal_id")
    w, p = _between("j_date", start, end)
    for j in conn.query("SELECT * FROM journals" + w, p):
        e = _entry(j["j_date"], "journal", j["j_no"] or "JV-" + str(j["id"]), j["narration"] or "Journal entry",
                   ("journal_view", {"jid": j["id"]}), j["id"])
        for l in jl.get(j["id"], ()):
            e.add(l["account_id"], dr=l["debit"], cr=l["credit"],
                  party_id=l["party_id"], party_name=l["party_name"])
        out.append(e)

    order = {"opening": 0, "purchase": 1, "production": 2, "sale": 3, "expense": 4,
             "receipt": 5, "payment": 6, "journal": 7}
    out.sort(key=lambda e: (e["date"], order.get(e["kind"], 9), e["doc_id"] or 0))
    return out


def _q(x):
    return ("%.3f" % float(x or 0)).rstrip("0").rstrip(".")


def entry_totals(e):
    return money(sum(l["dr"] for l in e["lines"])), money(sum(l["cr"] for l in e["lines"]))


# -------------------------------------------------------------- aggregation

def _sign(acc_type):
    return 1 if acc_type in DEBIT_NORMAL else -1


def balances(entries, accounts):
    """{account_id: balance in the account's own direction} - an asset or
    expense is positive when it has a debit balance, the rest when credit."""
    types = {a["id"]: a["type"] for a in accounts}
    raw = {}
    for e in entries:
        for l in e["lines"]:
            raw[l["acc"]] = raw.get(l["acc"], 0.0) + l["dr"] - l["cr"]
    return {acc: money(v * _sign(types.get(acc, "asset"))) for acc, v in raw.items()}


def net_profit(entries, accounts):
    types = {a["id"]: a["type"] for a in accounts}
    inc = exp = 0.0
    for e in entries:
        for l in e["lines"]:
            t = types.get(l["acc"])
            if t == "income":
                inc += l["cr"] - l["dr"]
            elif t == "expense":
                exp += l["dr"] - l["cr"]
    return money(inc - exp)


def _split(entries, start):
    before, within = [], []
    for e in entries:
        (before if e["date"] < start else within).append(e)
    return before, within


def day_before(d):
    return (datetime.strptime(d, "%Y-%m-%d").date() - timedelta(days=1)).isoformat()


# ---------------------------------------------------------------- statements

def trial_balance(conn, start, end):
    """Opening, movement in the period and closing for every account. Income
    and expense from before the period are closed into one 'Profit & loss
    brought forward' line, the way books are carried into a new year."""
    accounts = all_accounts(conn)
    entries = build(conn, end=end)
    before, within = _split(entries, start)
    types = {a["id"]: a["type"] for a in accounts}

    open_raw, dr, cr = {}, {}, {}
    pl_bf = 0.0
    for e in before:
        for l in e["lines"]:
            if types.get(l["acc"]) in ("income", "expense"):
                pl_bf += l["dr"] - l["cr"]
            else:
                open_raw[l["acc"]] = open_raw.get(l["acc"], 0.0) + l["dr"] - l["cr"]
    for e in within:
        for l in e["lines"]:
            dr[l["acc"]] = dr.get(l["acc"], 0.0) + l["dr"]
            cr[l["acc"]] = cr.get(l["acc"], 0.0) + l["cr"]

    rows = []
    for a in accounts:
        o = money(open_raw.get(a["id"], 0.0))
        d = money(dr.get(a["id"], 0.0))
        c = money(cr.get(a["id"], 0.0))
        if not (o or d or c):
            continue
        rows.append(_tb_row(a, o, d, c))
    if abs(pl_bf) >= 0.005:
        rows.append(_tb_row(dict(id=None, code="", name="Profit & loss brought forward", type="equity",
                                 grp="Capital", sys_key=""), money(pl_bf), 0.0, 0.0))
    tot = {k: money(sum(r[k] for r in rows)) for k in
           ("open_dr", "open_cr", "dr", "cr", "close_dr", "close_cr")}
    tot["balanced"] = abs(tot["close_dr"] - tot["close_cr"]) < 0.01
    return dict(rows=rows, tot=tot, start=start, end=end)


def _tb_row(a, opening, dr, cr):
    close = money(opening + dr - cr)
    return dict(id=a["id"], code=a["code"], name=a["name"], type=a["type"], grp=a["grp"],
                open_dr=opening if opening > 0 else 0.0, open_cr=-opening if opening < 0 else 0.0,
                dr=dr, cr=cr,
                close_dr=close if close > 0 else 0.0, close_cr=-close if close < 0 else 0.0)


def profit_and_loss(conn, start, end):
    accounts = all_accounts(conn)
    entries = build(conn, start=start, end=end)
    bal = balances(entries, accounts)

    def section(typ, grp):
        rows = [dict(id=a["id"], name=a["name"], code=a["code"], amount=bal.get(a["id"], 0.0))
                for a in accounts if a["type"] == typ and a["grp"] == grp and bal.get(a["id"])]
        return dict(rows=rows, total=money(sum(r["amount"] for r in rows)))

    sales = section("income", "Sales")
    direct = section("expense", "Direct Expenses")
    ind_inc = section("income", "Indirect Income")
    ind_exp = section("expense", "Indirect Expenses")
    # an income or expense head in a group the statement does not know about
    known = {("income", "Sales"), ("expense", "Direct Expenses"),
             ("income", "Indirect Income"), ("expense", "Indirect Expenses")}
    for a in accounts:
        if a["type"] in ("income", "expense") and (a["type"], a["grp"]) not in known and bal.get(a["id"]):
            target = ind_inc if a["type"] == "income" else ind_exp
            target["rows"].append(dict(id=a["id"], name=a["name"], code=a["code"], amount=bal[a["id"]]))
            target["total"] = money(target["total"] + bal[a["id"]])
    gross = money(sales["total"] - direct["total"])
    net = money(gross + ind_inc["total"] - ind_exp["total"])
    return dict(start=start, end=end, sales=sales, direct=direct, gross=gross,
                ind_inc=ind_inc, ind_exp=ind_exp, net=net,
                gross_margin=money(gross / sales["total"] * 100) if sales["total"] else 0.0,
                net_margin=money(net / sales["total"] * 100) if sales["total"] else 0.0)


def balance_sheet(conn, as_at):
    accounts = all_accounts(conn)
    entries = build(conn, end=as_at)
    fy_start = fy_bounds(fy_of(as_at))[0]
    before, within = _split(entries, fy_start)
    bal = balances(entries, accounts)

    def side(typ):
        groups = []
        for g in GROUPS[typ] + sorted({a["grp"] for a in accounts if a["type"] == typ} - set(GROUPS[typ])):
            rows = [dict(id=a["id"], name=a["name"], code=a["code"], amount=bal.get(a["id"], 0.0))
                    for a in accounts if a["type"] == typ and a["grp"] == g and abs(bal.get(a["id"], 0.0)) >= 0.005]
            if rows:
                groups.append(dict(name=g, rows=rows, total=money(sum(r["amount"] for r in rows))))
        return groups

    assets, liabilities, equity = side("asset"), side("liability"), side("equity")
    retained = net_profit(before, accounts)
    current = net_profit(within, accounts)
    eq_rows = [dict(id=None, name="Profit & loss - earlier years", amount=retained)] if retained else []
    eq_rows.append(dict(id=None, name="Profit & loss - this year (from " + fy_start + ")", amount=current))
    equity.append(dict(name="Reserves & Surplus", rows=eq_rows, total=money(retained + current)))

    total_assets = money(sum(g["total"] for g in assets))
    total_liab = money(sum(g["total"] for g in liabilities))
    total_equity = money(sum(g["total"] for g in equity))
    return dict(as_at=as_at, fy_start=fy_start, assets=assets, liabilities=liabilities, equity=equity,
                total_assets=total_assets, total_liab=total_liab, total_equity=total_equity,
                total_le=money(total_liab + total_equity),
                diff=money(total_assets - total_liab - total_equity),
                current_profit=current)


def ledger(conn, account_id, start, end, party=None):
    """Every posting to one account, with opening, running and closing
    balances. party narrows Debtors/Creditors to one customer or supplier."""
    accounts = all_accounts(conn)
    acc = next((a for a in accounts if a["id"] == account_id), None)
    if not acc:
        return None
    names = {a["id"]: a["name"] for a in accounts}
    sign = _sign(acc["type"])
    entries = build(conn, end=end)
    opening, rows = 0.0, []
    for e in entries:
        mine = [l for l in e["lines"] if l["acc"] == account_id and _party_match(l, party)]
        if not mine:
            continue
        d = money(sum(l["dr"] for l in mine))
        c = money(sum(l["cr"] for l in mine))
        if e["date"] < start:
            opening += (d - c) * sign
            continue
        others = []
        for l in e["lines"]:
            if l["acc"] != account_id and names.get(l["acc"]) not in others:
                others.append(names.get(l["acc"], "?"))
        rows.append(dict(date=e["date"], kind=e["kind"], ref=e["ref"], narration=e["narration"],
                         link=e["link"], against=", ".join(others[:3]) + (" +" if len(others) > 3 else ""),
                         party_name=mine[0]["party_name"], dr=d, cr=c))
    opening = money(opening)
    run = opening
    for r in rows:
        run = money(run + (r["dr"] - r["cr"]) * sign)
        r["balance"] = run
    return dict(account=acc, start=start, end=end, opening=opening, rows=rows, closing=run,
                tot_dr=money(sum(r["dr"] for r in rows)), tot_cr=money(sum(r["cr"] for r in rows)),
                debit_normal=acc["type"] in DEBIT_NORMAL, party=party)


def _party_key(party_id, party_name):
    if party_id:
        return "id:" + str(party_id)
    return "name:" + (party_name or "").strip().lower()


def _party_match(line, party):
    return party is None or _party_key(line["party_id"], line["party_name"]) == party


def outstanding(conn, as_at=None):
    """What each customer owes you and what you owe each supplier."""
    as_at = as_at or today_str()
    K = key_ids(conn)
    entries = build(conn, end=as_at)
    names = {r["id"]: r for r in conn.query("SELECT id, name, phone, kind FROM parties")}
    sides = {"debtors": {}, "creditors": {}}
    for e in entries:
        for l in e["lines"]:
            for key in PARTY_KEYS:
                if l["acc"] != K[key]:
                    continue
                pk = _party_key(l["party_id"], l["party_name"])
                row = sides[key].setdefault(pk, dict(key=pk, party_id=l["party_id"],
                                                     name=l["party_name"] or "Unnamed", phone="",
                                                     amount=0.0, last=e["date"], billed=0.0, settled=0.0))
                if l["party_id"] and l["party_id"] in names:
                    row["name"] = names[l["party_id"]]["name"]
                    row["phone"] = names[l["party_id"]]["phone"] or ""
                if key == "debtors":
                    row["amount"] += l["dr"] - l["cr"]
                    row["billed"] += l["dr"]
                    row["settled"] += l["cr"]
                else:
                    row["amount"] += l["cr"] - l["dr"]
                    row["billed"] += l["cr"]
                    row["settled"] += l["dr"]
                row["last"] = max(row["last"], e["date"])
    out = {}
    today = datetime.strptime(as_at, "%Y-%m-%d").date()
    for key, rows in sides.items():
        lst = []
        for r in rows.values():
            for k in ("amount", "billed", "settled"):
                r[k] = money(r[k])
            if abs(r["amount"]) < 0.005:
                continue
            r["days"] = (today - datetime.strptime(r["last"], "%Y-%m-%d").date()).days
            lst.append(r)
        lst.sort(key=lambda r: -r["amount"])
        out[key] = dict(rows=lst, total=money(sum(r["amount"] for r in lst)))
    out["as_at"] = as_at
    return out


def dashboard_figures(conn, m_start, m_end, today):
    """Balances as at today and this month's net profit, from one pass."""
    accounts = all_accounts(conn)
    K = key_ids(conn)
    entries = build(conn, end=max(today, m_end))
    upto = [e for e in entries if e["date"] <= today]
    bal = balances(upto, accounts)
    month = [e for e in entries if m_start <= e["date"] <= m_end]
    types = {a["id"]: a["type"] for a in accounts}
    expenses = 0.0
    for e in month:
        if e["kind"] == "expense":
            for l in e["lines"]:
                if types.get(l["acc"]) == "expense":
                    expenses += l["dr"] - l["cr"]
    gst_out = sum(bal.get(K[k], 0.0) for k in ("out_cgst", "out_sgst", "out_igst"))
    gst_in = sum(bal.get(K[k], 0.0) for k in ("itc_cgst", "itc_sgst", "itc_igst"))
    return dict(cash=bal.get(K["cash"], 0.0), bank=bal.get(K["bank"], 0.0),
                receivable=bal.get(K["debtors"], 0.0), payable=bal.get(K["creditors"], 0.0),
                gst_net=money(gst_out - gst_in),
                month_net=net_profit(month, accounts), month_expenses=money(expenses))


def inventory_check(conn):
    """The books' stock accounts against the stock valuation on the Items
    page. They are built from the same replay, so they must agree."""
    K = key_ids(conn)
    entries = build(conn)
    accounts = all_accounts(conn)
    bal = balances(entries, accounts)
    books = money(bal.get(K["inv_fg"], 0.0) + bal.get(K["inv_rm"], 0.0))
    stock = money(conn.scalar("SELECT COALESCE(SUM(stock_value),0) AS v FROM items"))
    return dict(books=books, stock=stock, diff=money(books - stock), ok=abs(books - stock) < 0.05)


def next_number(conn, table, col, prefix, day):
    """Voucher numbers like RCT/2026-27/0001, per financial year."""
    pattern = "{}/{}/".format(prefix, fy_of(day))
    highest = 0
    for r in conn.query("SELECT {} AS n FROM {} WHERE {} LIKE ?".format(col, table, col), (pattern + "%",)):
        tail = (r["n"] or "")[len(pattern):]
        if tail.isdigit():
            highest = max(highest, int(tail))
    return "{}{:04d}".format(pattern, highest + 1)
