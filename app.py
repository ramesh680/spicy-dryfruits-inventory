"""Spicy Dry Fruits - daily inventory, GST and profit tracker."""
import csv
import io
import json
import os
from datetime import date, datetime, timedelta

from functools import wraps

from flask import (Flask, Response, flash, redirect, render_template, request,
                   session, url_for)

import charts
import core
from core import (GST_RATES, PAYMENT_MODES, STATE_CODES, STATE_NAME, UNITS,
                  amount_in_words, compute_line, money, qty, rate_wise_breakup,
                  today_str, totals_from_lines)
from db import get_db, init_db

app = Flask(__name__)
app.secret_key = os.environ.get("SECRET_KEY", "dev-secret-change-me")


# --------------------------------------------------------------- helpers

def get_settings(conn):
    s = conn.query_one("SELECT * FROM settings WHERE id = 1")
    if not s:
        conn.execute("INSERT INTO settings (id) VALUES (1)")
        s = conn.query_one("SELECT * FROM settings WHERE id = 1")
    s["state_name"] = STATE_NAME.get(s["state_code"], "")
    return s


def admin_configured(s):
    return bool(s.get("admin_hash") and s.get("admin_salt"))


def is_admin():
    return bool(session.get("admin"))


def admin_required(view):
    """Editing and deleting are gated; recording new entries is not."""
    @wraps(view)
    def guard(*args, **kwargs):
        if not is_admin():
            flash("Unlock admin to change or delete an entry.", "warn")
            return redirect(url_for("admin", next=request.full_path))
        return view(*args, **kwargs)
    return guard


def lines_json(rows):
    return json.dumps([
        dict(item_id=r["item_id"], qty=float(r["qty"]), rate=float(r["rate"]),
             discount_pct=float(r["discount_pct"] or 0))
        for r in rows])


def f(name, default=""):
    return (request.form.get(name) or default).strip()


def fnum(name, default=0.0):
    try:
        return float(request.form.get(name) or default)
    except ValueError:
        return float(default)


def items_json(items):
    return json.dumps([
        dict(id=i["id"], name=i["name"], unit=i["unit"],
             gst_rate=float(i["gst_rate"]), sale_rate=float(i.get("sale_rate") or 0),
             stock_qty=float(i.get("stock_qty") or 0),
             kind=i.get("kind") or "product")
        for i in items])


def party_states_json(parties):
    return json.dumps({str(p["id"]): p["state_code"] for p in parties})


def month_bounds(d=None):
    d = d or date.today()
    start = d.replace(day=1)
    nxt = (start + timedelta(days=32)).replace(day=1)
    return start.isoformat(), (nxt - timedelta(days=1)).isoformat()


PRESETS = [("today", "Today"), ("week", "This week"), ("month", "This month"),
           ("fy", "This FY"), ("all", "All time")]


def preset_range(name):
    """Turn a preset name into a date range. Unknown names fall back to this month."""
    t = date.today()
    if name == "today":
        return t.isoformat(), t.isoformat()
    if name == "week":
        start = t - timedelta(days=t.weekday())
        return start.isoformat(), (start + timedelta(days=6)).isoformat()
    if name == "fy":
        return core.fy_bounds(core.fy_of(t.isoformat()))
    if name == "all":
        return "1900-01-01", "2999-12-31"
    return month_bounds(t)


def resolve_range():
    """Date range for a register or report page: an explicit start/end wins,
    otherwise the named preset, otherwise this month."""
    preset = (request.args.get("preset") or "").strip()
    start, end = request.args.get("start"), request.args.get("end")
    if start and end and not preset:
        return start, end, ""
    if preset:
        s, e = preset_range(preset)
        return s, e, preset
    s, e = month_bounds()
    return s, e, "month"


def search_clause(term, id_col, table, lines_table, fk):
    """SQL fragment matching a document number, party name, payment mode or any
    item on the document. Returns (sql, params) or ("", [])."""
    if not term:
        return "", []
    like = "%" + term.lower() + "%"
    sql = (" AND (LOWER({num}) LIKE ? OR LOWER(party_name) LIKE ? OR LOWER(payment_mode) LIKE ?"
           " OR EXISTS (SELECT 1 FROM {lt} WHERE {lt}.{fk} = {t}.id"
           " AND LOWER({lt}.item_name) LIKE ?))").format(num=id_col, lt=lines_table, fk=fk, t=table)
    return sql, [like, like, like, like]


def parse_lines(conn, gst_enabled, interstate):
    """Build invoice lines from the repeating form fields."""
    item_ids = request.form.getlist("item_id")
    qtys = request.form.getlist("qty")
    rates = request.form.getlist("rate")
    discs = request.form.getlist("discount_pct")
    items = {i["id"]: i for i in conn.query("SELECT * FROM items")}
    lines = []
    for idx, raw_id in enumerate(item_ids):
        if not raw_id:
            continue
        try:
            item = items[int(raw_id)]
        except (KeyError, ValueError):
            continue
        try:
            q = float(qtys[idx] or 0)
            r = float(rates[idx] or 0)
            d = float(discs[idx] or 0) if idx < len(discs) else 0.0
        except (IndexError, ValueError):
            continue
        if q <= 0:
            continue
        line = compute_line(q, r, d, item["gst_rate"], interstate, gst_enabled)
        line.update(item_id=item["id"], item_name=item["name"], hsn=item["hsn"] or "")
        lines.append(line)
    return lines


@app.context_processor
def inject_globals():
    return dict(STATE_CODES=STATE_CODES, STATE_NAME=STATE_NAME,
                GST_RATES=GST_RATES, UNITS=UNITS, PAYMENT_MODES=PAYMENT_MODES,
                today=today_str(), is_admin=is_admin())


@app.template_filter("inr")
def inr(value):
    """Indian digit grouping: 12,34,567.89"""
    try:
        v = float(value or 0)
    except (TypeError, ValueError):
        return "0.00"
    neg = v < 0
    whole, frac = divmod(round(abs(v) * 100), 100)
    s = str(whole)
    if len(s) > 3:
        head, tail = s[:-3], s[-3:]
        parts = []
        while len(head) > 2:
            parts.insert(0, head[-2:])
            head = head[:-2]
        if head:
            parts.insert(0, head)
        s = ",".join(parts) + "," + tail
    return ("-" if neg else "") + s + ".{:02d}".format(frac)


@app.template_filter("qty")
def qty_filter(value):
    try:
        v = float(value or 0)
    except (TypeError, ValueError):
        return "0"
    return ("%.3f" % v).rstrip("0").rstrip(".") or "0"


@app.template_filter("dmy")
def dmy(value):
    try:
        return datetime.strptime(str(value), "%Y-%m-%d").strftime("%d %b %Y")
    except (ValueError, TypeError):
        return value


# ------------------------------------------------------------- dashboard

@app.route("/")
def dashboard():
    with get_db() as conn:
        s = get_settings(conn)
        t = today_str()
        m_start, m_end = month_bounds()

        def agg(table, datecol, start, end):
            row = conn.query_one(
                "SELECT COALESCE(SUM(taxable),0) AS taxable, COALESCE(SUM(cgst),0) AS cgst, "
                "COALESCE(SUM(sgst),0) AS sgst, COALESCE(SUM(igst),0) AS igst, "
                "COALESCE(SUM(total),0) AS total, COUNT(*) AS n "
                "FROM {} WHERE {} >= ? AND {} <= ?".format(table, datecol, datecol),
                (start, end))
            return {k: (float(v) if k != "n" else int(v)) for k, v in row.items()}

        today_sales = agg("sales", "invoice_date", t, t)
        today_purch = agg("purchases", "bill_date", t, t)
        month_sales = agg("sales", "invoice_date", m_start, m_end)
        month_purch = agg("purchases", "bill_date", m_start, m_end)

        today_cogs = float(conn.scalar(
            "SELECT COALESCE(SUM(cogs),0) AS v FROM sales WHERE invoice_date = ?", (t,)))
        month_cogs = float(conn.scalar(
            "SELECT COALESCE(SUM(cogs),0) AS v FROM sales WHERE invoice_date >= ? AND invoice_date <= ?",
            (m_start, m_end)))

        stock = core.stock_rows(conn)
        stock_value = money(sum(r["stock_value"] for r in stock))
        low = [r for r in stock if r["low"]]

        # last 14 days trend
        trend = []
        for i in range(13, -1, -1):
            d = (date.today() - timedelta(days=i)).isoformat()
            sv = float(conn.scalar("SELECT COALESCE(SUM(taxable),0) AS v FROM sales WHERE invoice_date = ?", (d,)))
            pv = float(conn.scalar("SELECT COALESCE(SUM(taxable),0) AS v FROM purchases WHERE bill_date = ?", (d,)))
            trend.append(dict(day=d, sales=money(sv), purchases=money(pv)))

        recent_sales = conn.query(
            "SELECT * FROM sales ORDER BY invoice_date DESC, id DESC LIMIT 6")
        recent_purchases = conn.query(
            "SELECT * FROM purchases ORDER BY bill_date DESC, id DESC LIMIT 6")

        out_tax = money(month_sales["cgst"] + month_sales["sgst"] + month_sales["igst"])
        in_tax = money(month_purch["cgst"] + month_purch["sgst"] + month_purch["igst"])

        for row in trend:
            row["dd"] = row["day"][8:10]
        trend_chart = charts.columns_pair(trend, "dd", "sales", "purchases",
                                          "Sales", "Purchases")

        return render_template(
            "dashboard.html", s=s,
            today_sales=today_sales, today_purch=today_purch,
            today_profit=money(today_sales["taxable"] - today_cogs),
            month_sales=month_sales, month_purch=month_purch,
            month_profit=money(month_sales["taxable"] - month_cogs),
            stock_value=stock_value, low=low, trend=trend, trend_chart=trend_chart,
            recent_sales=recent_sales, recent_purchases=recent_purchases,
            out_tax=out_tax, in_tax=in_tax, net_gst=money(out_tax - in_tax),
            month_label=date.today().strftime("%B %Y"))


# ----------------------------------------------------------------- items

@app.route("/items")
def items():
    with get_db() as conn:
        return render_template("items.html", s=get_settings(conn),
                               rows=core.stock_rows(conn),
                               inactive=conn.query("SELECT * FROM items WHERE active = 0 ORDER BY name"))


@app.route("/items/save", methods=["POST"])
def items_save():
    with get_db() as conn:
        data = dict(
            name=f("name"), hsn=f("hsn"), gst_rate=fnum("gst_rate", 5),
            unit=f("unit", "kg"), low_stock_qty=fnum("low_stock_qty"),
            sale_rate=fnum("sale_rate"), opening_qty=fnum("opening_qty"),
            opening_rate=fnum("opening_rate"),
            kind="raw" if f("kind") == "raw" else "product",
        )
        if not data["name"]:
            flash("Item name is required.", "error")
            return redirect(url_for("items"))
        item_id = f("id")
        if item_id:
            conn.execute(
                "UPDATE items SET name=?, hsn=?, gst_rate=?, unit=?, low_stock_qty=?, "
                "sale_rate=?, opening_qty=?, opening_rate=?, kind=? WHERE id=?",
                (data["name"], data["hsn"], data["gst_rate"], data["unit"],
                 data["low_stock_qty"], data["sale_rate"], data["opening_qty"],
                 data["opening_rate"], data["kind"], int(item_id)))
            flash("Item updated.", "ok")
        else:
            data["created_at"] = today_str()
            conn.insert("items", data)
            flash("Item added.", "ok")
        core.rebuild_stock(conn)
    return redirect(url_for("items"))


@app.route("/items/<int:item_id>/toggle", methods=["POST"])
@admin_required
def items_toggle(item_id):
    with get_db() as conn:
        row = conn.query_one("SELECT active FROM items WHERE id = ?", (item_id,))
        if row:
            conn.execute("UPDATE items SET active = ? WHERE id = ?",
                         (0 if row["active"] else 1, item_id))
            flash("Item archived." if row["active"] else "Item restored.", "ok")
    return redirect(url_for("items"))


# --------------------------------------------------------------- parties

@app.route("/parties")
def parties():
    with get_db() as conn:
        return render_template("parties.html", s=get_settings(conn),
                               rows=conn.query("SELECT * FROM parties ORDER BY name"))


@app.route("/parties/save", methods=["POST"])
def parties_save():
    with get_db() as conn:
        name = f("name")
        if not name:
            flash("Name is required.", "error")
            return redirect(url_for("parties"))
        data = dict(name=name, kind=f("kind", "customer"), gstin=f("gstin").upper(),
                    phone=f("phone"), address=f("address"),
                    state_code=f("state_code", get_settings(conn)["state_code"]))
        pid = f("id")
        if pid:
            conn.execute(
                "UPDATE parties SET name=?, kind=?, gstin=?, phone=?, address=?, state_code=? WHERE id=?",
                (data["name"], data["kind"], data["gstin"], data["phone"],
                 data["address"], data["state_code"], int(pid)))
            flash("Contact updated.", "ok")
        else:
            data["created_at"] = today_str()
            conn.insert("parties", data)
            flash("Contact added.", "ok")
    return redirect(url_for("parties"))


@app.route("/parties/<int:pid>/delete", methods=["POST"])
@admin_required
def parties_delete(pid):
    with get_db() as conn:
        conn.execute("DELETE FROM parties WHERE id = ?", (pid,))
        flash("Contact removed.", "ok")
    return redirect(url_for("parties"))


# ------------------------------------------------------------- purchases

@app.route("/purchases")
def purchases():
    start, end, preset = resolve_range()
    q = (request.args.get("q") or "").strip()
    with get_db() as conn:
        where, params = search_clause(q, "bill_no", "purchases", "purchase_lines", "purchase_id")
        rows = conn.query(
            "SELECT * FROM purchases WHERE bill_date >= ? AND bill_date <= ?" + where +
            " ORDER BY bill_date DESC, id DESC", [start, end] + params)
        tot = totals_from_lines(rows) if rows else totals_from_lines([])
        return render_template("purchases.html", s=get_settings(conn), rows=rows,
                               start=start, end=end, preset=preset, q=q, presets=PRESETS,
                               tot=tot, grand=money(sum(float(r["total"]) for r in rows)))


def save_purchase(conn, s, fallback_endpoint, purchase_id=None):
    """Write a purchase bill from the posted form. With purchase_id the existing
    bill is rewritten in place."""
    bill_date = f("bill_date", today_str())
    party_id = f("party_id")
    party = conn.query_one("SELECT * FROM parties WHERE id = ?", (int(party_id),)) if party_id else None
    state_code = (party["state_code"] if party else f("state_code", s["state_code"]))
    interstate = state_code != s["state_code"]
    lines = parse_lines(conn, bool(s["gst_enabled"]), interstate)
    if not lines:
        flash("Add at least one item with a quantity.", "error")
        return redirect(url_for(fallback_endpoint))

    t = totals_from_lines(lines)
    now = datetime.now().isoformat(timespec="seconds")
    header = dict(
        bill_no=f("bill_no"), bill_date=bill_date,
        party_id=int(party_id) if party_id else None,
        party_name=(party["name"] if party else f("party_name", "Cash purchase")),
        state_code=state_code, interstate=1 if interstate else 0,
        taxable=t["taxable"], cgst=t["cgst"], sgst=t["sgst"], igst=t["igst"],
        round_off=t["round_off"], total=t["total"],
        payment_mode=f("payment_mode", "Cash"), notes=f("notes"))
    if purchase_id:
        sets = ", ".join(k + " = ?" for k in header)
        conn.execute(
            "UPDATE purchases SET " + sets + ", updated_at = ?, edit_count = edit_count + 1 WHERE id = ?",
            list(header.values()) + [now, purchase_id])
        conn.execute("DELETE FROM purchase_lines WHERE purchase_id = ?", (purchase_id,))
        pid = purchase_id
    else:
        header["created_at"] = now
        pid = conn.insert("purchases", header)
    for l in lines:
        conn.insert("purchase_lines", dict(
            purchase_id=pid, item_id=l["item_id"], item_name=l["item_name"],
            hsn=l["hsn"], qty=l["qty"], rate=l["rate"],
            discount_pct=l["discount_pct"], taxable=l["taxable"],
            gst_rate=l["gst_rate"], cgst=l["cgst"], sgst=l["sgst"],
            igst=l["igst"], total=l["total"]))
    core.rebuild_stock(conn)
    flash("Purchase " + ("updated" if purchase_id else "recorded")
          + " - stock and costing recalculated.", "ok")
    return redirect(url_for("purchase_view", pid=pid))


def purchase_form(conn, s, doc=None, lines=None):
    items = conn.query("SELECT * FROM items WHERE active = 1 ORDER BY name")
    parties = conn.query("SELECT * FROM parties WHERE kind IN ('supplier','both') ORDER BY name")
    return render_template(
        "entry_form.html", s=s, items=items, parties=parties, doc=doc,
        items_json=items_json(items), party_states_json=party_states_json(parties),
        existing_json=lines_json(lines) if lines else "",
        heading=("Edit purchase " + (doc["bill_no"] or "#" + str(doc["id"]))) if doc else "Record a purchase",
        subhead=("Saving replays every bill and invoice, so stock and costing stay right."
                 if doc else "Stock and weighted-average cost update as soon as you save."),
        date_field="bill_date", date_label="Bill date",
        date_value=(doc["bill_date"] if doc else today_str()),
        no_field="bill_no", no_label="Supplier's bill no.",
        no_value=(doc["bill_no"] if doc else ""), no_placeholder="e.g. 1042",
        party_label="Supplier", walkin_label="Cash purchase (no saved supplier)",
        walkin_name_label="Supplier name (if not saved)",
        walkin_placeholder="e.g. Krishna Traders, APMC",
        submit_label=("Save changes" if doc else "Save purchase"),
        show_stock=False, use_sale_rate=False)


@app.route("/purchases/new", methods=["GET", "POST"])
def purchase_new():
    with get_db() as conn:
        s = get_settings(conn)
        if request.method == "POST":
            return save_purchase(conn, s, "purchase_new")
        return purchase_form(conn, s)


@app.route("/purchases/<int:pid>/edit", methods=["GET", "POST"])
@admin_required
def purchase_edit(pid):
    with get_db() as conn:
        s = get_settings(conn)
        doc = conn.query_one("SELECT * FROM purchases WHERE id = ?", (pid,))
        if not doc:
            return redirect(url_for("purchases"))
        if request.method == "POST":
            return save_purchase(conn, s, "purchases", purchase_id=pid)
        lines = conn.query("SELECT * FROM purchase_lines WHERE purchase_id = ? ORDER BY id", (pid,))
        return purchase_form(conn, s, doc=doc, lines=lines)


@app.route("/purchases/<int:pid>")
def purchase_view(pid):
    with get_db() as conn:
        doc = conn.query_one("SELECT * FROM purchases WHERE id = ?", (pid,))
        if not doc:
            return redirect(url_for("purchases"))
        lines = conn.query("SELECT * FROM purchase_lines WHERE purchase_id = ? ORDER BY id", (pid,))
        return render_template("purchase_view.html", s=get_settings(conn), doc=doc,
                               lines=lines, breakup=rate_wise_breakup(lines))


@app.route("/purchases/<int:pid>/delete", methods=["POST"])
@admin_required
def purchase_delete(pid):
    with get_db() as conn:
        conn.execute("DELETE FROM purchase_lines WHERE purchase_id = ?", (pid,))
        conn.execute("DELETE FROM purchases WHERE id = ?", (pid,))
        core.rebuild_stock(conn)
        flash("Purchase deleted - stock recalculated.", "ok")
    return redirect(url_for("purchases"))


# ----------------------------------------------------------------- sales

@app.route("/sales")
def sales():
    start, end, preset = resolve_range()
    q = (request.args.get("q") or "").strip()
    with get_db() as conn:
        where, params = search_clause(q, "invoice_no", "sales", "sale_lines", "sale_id")
        rows = conn.query(
            "SELECT * FROM sales WHERE invoice_date >= ? AND invoice_date <= ?" + where +
            " ORDER BY invoice_date DESC, id DESC", [start, end] + params)
        profit = money(sum(float(r["taxable"]) - float(r["cogs"]) for r in rows))
        return render_template("sales.html", s=get_settings(conn), rows=rows,
                               start=start, end=end, preset=preset, q=q, presets=PRESETS,
                               profit=profit, grand=money(sum(float(r["total"]) for r in rows)))


def save_sale(conn, s, fallback_endpoint, sale_id=None):
    """Write a sale from the posted form. Shared by the full invoice form, the
    quick counter screen and the edit form, so all three produce identical
    records. With sale_id the existing invoice is rewritten in place."""
    invoice_date = f("invoice_date", today_str())
    party_id = f("party_id")
    party = conn.query_one("SELECT * FROM parties WHERE id = ?", (int(party_id),)) if party_id else None
    state_code = (party["state_code"] if party else f("state_code", s["state_code"]))
    interstate = state_code != s["state_code"]
    lines = parse_lines(conn, bool(s["gst_enabled"]), interstate)
    if not lines:
        flash("Add at least one item with a quantity.", "error")
        return redirect(url_for(fallback_endpoint))

    # warn (but do not block) if stock would go negative
    shortfall = []
    for l in lines:
        it = conn.query_one("SELECT name, stock_qty, unit FROM items WHERE id = ?", (l["item_id"],))
        if it and float(it["stock_qty"]) < l["qty"]:
            shortfall.append("{} (have {} {})".format(it["name"], qty(it["stock_qty"]), it["unit"]))

    t = totals_from_lines(lines)
    now = datetime.now().isoformat(timespec="seconds")
    # Invoice numbers are generated, never typed: GST wants an unbroken series,
    # and a hand-typed number is how a stray entry gets a meaningless one. On an
    # edit the original number is kept.
    if sale_id:
        existing = conn.query_one("SELECT invoice_no FROM sales WHERE id = ?", (sale_id,))
        inv_no = existing["invoice_no"]
    else:
        inv_no = core.next_invoice_no(conn, invoice_date, s["invoice_prefix"])
    header = dict(
        invoice_no=inv_no, invoice_date=invoice_date,
        party_id=int(party_id) if party_id else None,
        party_name=(party["name"] if party else f("party_name", "Cash sale")),
        state_code=state_code, interstate=1 if interstate else 0,
        taxable=t["taxable"], cgst=t["cgst"], sgst=t["sgst"], igst=t["igst"],
        round_off=t["round_off"], total=t["total"], cogs=0,
        payment_mode=f("payment_mode", "Cash"), notes=f("notes"))
    if sale_id:
        sets = ", ".join(k + " = ?" for k in header)
        conn.execute(
            "UPDATE sales SET " + sets + ", updated_at = ?, edit_count = edit_count + 1 WHERE id = ?",
            list(header.values()) + [now, sale_id])
        conn.execute("DELETE FROM sale_lines WHERE sale_id = ?", (sale_id,))
        sid = sale_id
    else:
        header["created_at"] = now
        sid = conn.insert("sales", header)
    for l in lines:
        conn.insert("sale_lines", dict(
            sale_id=sid, item_id=l["item_id"], item_name=l["item_name"],
            hsn=l["hsn"], qty=l["qty"], rate=l["rate"],
            discount_pct=l["discount_pct"], taxable=l["taxable"],
            gst_rate=l["gst_rate"], cgst=l["cgst"], sgst=l["sgst"],
            igst=l["igst"], total=l["total"], cost_rate=0))
    core.rebuild_stock(conn)
    if shortfall:
        flash("Sale saved, but stock went negative for: " + ", ".join(shortfall)
              + ". Record the missing purchase to correct the valuation.", "warn")
    else:
        flash("Sale " + inv_no + (" updated." if sale_id else " recorded."), "ok")
    return redirect(url_for("invoice", sid=sid))


@app.route("/quick", methods=["GET", "POST"])
def quick():
    """Counter mode: tap an item, type a quantity, save."""
    with get_db() as conn:
        s = get_settings(conn)
        if request.method == "POST":
            return save_sale(conn, s, "quick")
        items = [i for i in core.stock_rows(conn) if i["kind"] != "raw"]
        return render_template(
            "quick.html", s=s, items=items, items_json=items_json(items),
            parties=conn.query("SELECT * FROM parties WHERE kind IN ('customer','both') ORDER BY name"),
            party_states_json=party_states_json(
                conn.query("SELECT * FROM parties WHERE kind IN ('customer','both')")),
            next_no=core.next_invoice_no(conn, today_str(), s["invoice_prefix"]))


def sale_form(conn, s, doc=None, lines=None):
    used = {l["item_id"] for l in (lines or [])}
    items = [i for i in core.stock_rows(conn) if i["kind"] != "raw" or i["id"] in used]
    parties = conn.query("SELECT * FROM parties WHERE kind IN ('customer','both') ORDER BY name")
    return render_template(
        "entry_form.html", s=s, items=items, parties=parties, doc=doc,
        items_json=items_json(items), party_states_json=party_states_json(parties),
        existing_json=lines_json(lines) if lines else "",
        heading=("Edit invoice " + doc["invoice_no"]) if doc else "Record a sale",
        subhead=("Saving replays every bill and invoice, so stock and costing stay right."
                 if doc else "Cost of goods sold is taken from the current weighted-average cost."),
        date_field="invoice_date", date_label="Invoice date",
        date_value=(doc["invoice_date"] if doc else today_str()),
        no_field="invoice_no", no_label="Invoice no.",
        no_value=(doc["invoice_no"] if doc
                  else core.next_invoice_no(conn, today_str(), s["invoice_prefix"])),
        no_placeholder="", no_readonly=True,
        no_hint=("Kept as it is - a GST invoice number never changes." if doc
                 else "Generated automatically. Restarts each financial year."),
        party_label="Customer", walkin_label="Walk-in / cash sale",
        walkin_name_label="Customer name (if not saved)",
        walkin_placeholder="e.g. Walk-in customer",
        submit_label=("Save changes" if doc else "Save sale and open invoice"),
        show_stock=True, use_sale_rate=True)


@app.route("/sales/new", methods=["GET", "POST"])
def sale_new():
    with get_db() as conn:
        s = get_settings(conn)
        if request.method == "POST":
            return save_sale(conn, s, "sale_new")
        return sale_form(conn, s)


@app.route("/sales/<int:sid>/edit", methods=["GET", "POST"])
@admin_required
def sale_edit(sid):
    with get_db() as conn:
        s = get_settings(conn)
        doc = conn.query_one("SELECT * FROM sales WHERE id = ?", (sid,))
        if not doc:
            return redirect(url_for("sales"))
        if request.method == "POST":
            return save_sale(conn, s, "sales", sale_id=sid)
        lines = conn.query("SELECT * FROM sale_lines WHERE sale_id = ? ORDER BY id", (sid,))
        return sale_form(conn, s, doc=doc, lines=lines)


@app.route("/sales/<int:sid>/invoice")
def invoice(sid):
    with get_db() as conn:
        doc = conn.query_one("SELECT * FROM sales WHERE id = ?", (sid,))
        if not doc:
            return redirect(url_for("sales"))
        lines = conn.query("SELECT * FROM sale_lines WHERE sale_id = ? ORDER BY id", (sid,))
        party = conn.query_one("SELECT * FROM parties WHERE id = ?", (doc["party_id"],)) if doc["party_id"] else None
        return render_template("invoice.html", s=get_settings(conn), doc=doc, lines=lines,
                               party=party, breakup=rate_wise_breakup(lines),
                               words=amount_in_words(doc["total"]),
                               profit=money(float(doc["taxable"]) - float(doc["cogs"])))


@app.route("/sales/<int:sid>/delete", methods=["POST"])
@admin_required
def sale_delete(sid):
    with get_db() as conn:
        conn.execute("DELETE FROM sale_lines WHERE sale_id = ?", (sid,))
        conn.execute("DELETE FROM sales WHERE id = ?", (sid,))
        core.rebuild_stock(conn)
        flash("Sale deleted - stock recalculated.", "ok")
    return redirect(url_for("sales"))


# ----------------------------------------------------------------- stock

@app.route("/stock")
def stock():
    with get_db() as conn:
        rows = core.stock_rows(conn)
        return render_template("stock.html", s=get_settings(conn), rows=rows,
                               total_value=money(sum(r["stock_value"] for r in rows)),
                               low_count=len([r for r in rows if r["low"]]))


# ------------------------------------------------- production & raw material

def next_batch_no(conn):
    highest = 0
    for r in conn.query("SELECT batch_no FROM productions"):
        tail = (r["batch_no"] or "").rsplit("-", 1)[-1]
        if tail.isdigit():
            highest = max(highest, int(tail))
    return "BATCH-{:04d}".format(highest + 1)


def _raws_and_products(conn, keep_ids=()):
    rows = core.stock_rows(conn)
    raws = [r for r in rows if r["kind"] == "raw" or r["id"] in keep_ids]
    products = [r for r in rows if r["kind"] != "raw" or r["id"] in keep_ids]
    return raws, products


def save_production(conn, prod_id=None):
    product_id = f("product_id")
    out_qty = fnum("qty_out")
    items = {i["id"]: i for i in conn.query("SELECT * FROM items")}
    try:
        product = items[int(product_id)]
    except (KeyError, ValueError):
        product = None
    back = url_for("production_edit", pid=prod_id) if prod_id else url_for("production_new")
    if not product:
        flash("Choose the product you made.", "error")
        return redirect(back)
    if out_qty <= 0:
        flash("Enter how much " + product["name"] + " this batch produced.", "error")
        return redirect(back)

    lines, seen = [], set()
    for raw_id, q in zip(request.form.getlist("raw_id"), request.form.getlist("raw_qty")):
        try:
            rid, q = int(raw_id), float(q or 0)
        except ValueError:
            continue
        if q <= 0 or rid not in items or rid == product["id"]:
            continue
        if rid in seen:   # the same ingredient twice is one line
            for l in lines:
                if l["raw_id"] == rid:
                    l["qty"] = core.qty(l["qty"] + q)
            continue
        seen.add(rid)
        lines.append(dict(raw_id=rid, raw_name=items[rid]["name"], qty=core.qty(q)))
    if not lines:
        flash("Add the raw materials this batch used.", "error")
        return redirect(back)

    # Stock as it stands before this batch: on an edit, add back what the old
    # version of it took, so a batch is never short of its own ingredients.
    already = {}
    if prod_id:
        for l in conn.query("SELECT raw_id, qty FROM production_lines WHERE production_id = ?", (prod_id,)):
            already[l["raw_id"]] = already.get(l["raw_id"], 0.0) + float(l["qty"])
    short = []
    for l in lines:
        it = items[l["raw_id"]]
        have = float(it["stock_qty"] or 0) + already.get(l["raw_id"], 0.0)
        if have + 0.0001 < l["qty"]:
            short.append("{} (have {} {})".format(it["name"], core.qty(have), it["unit"]))

    now = datetime.now().isoformat(timespec="seconds")
    header = dict(prod_date=f("prod_date", today_str()), product_id=product["id"],
                  product_name=product["name"], qty_out=core.qty(out_qty),
                  other_cost=money(fnum("other_cost")), notes=f("notes"))
    if prod_id:
        sets = ", ".join(k + " = ?" for k in header)
        conn.execute("UPDATE productions SET " + sets + ", updated_at = ?, edit_count = edit_count + 1 WHERE id = ?",
                     list(header.values()) + [now, prod_id])
        conn.execute("DELETE FROM production_lines WHERE production_id = ?", (prod_id,))
        pid = prod_id
    else:
        header.update(batch_no=next_batch_no(conn), created_at=now)
        pid = conn.insert("productions", header)
    for l in lines:
        conn.insert("production_lines", dict(production_id=pid, raw_id=l["raw_id"],
                                             raw_name=l["raw_name"], qty=l["qty"]))

    if f("save_recipe"):
        conn.execute("DELETE FROM recipe_lines WHERE product_id = ?", (product["id"],))
        for l in lines:
            conn.insert("recipe_lines", dict(product_id=product["id"], raw_id=l["raw_id"],
                                             qty_per_unit=round(l["qty"] / out_qty, 6)))

    core.rebuild_stock(conn)
    if short:
        flash("Batch saved, but these raw materials went negative: " + ", ".join(short)
              + ". Record the missing purchase to correct stock and cost.", "warn")
    else:
        flash("Batch " + ("updated" if prod_id else "recorded") + " - raw stock used up and "
              + product["name"] + " added to stock.", "ok")
    return redirect(url_for("production_view", pid=pid))


def production_form(conn, doc=None, lines=None):
    keep = {doc["product_id"]} | {l["raw_id"] for l in lines} if doc else set()
    raws, products = _raws_and_products(conn, keep)
    preset_product = request.args.get("product", type=int)
    return render_template(
        "production_form.html", s=get_settings(conn), doc=doc, raws=raws, products=products,
        raws_json=json.dumps([dict(id=r["id"], name=r["name"], unit=r["unit"],
                                   stock_qty=float(r["stock_qty"]), avg_cost=float(r["avg_cost"]))
                              for r in raws]),
        recipes_json=json.dumps(core.recipes_json(conn)),
        existing_json=json.dumps([dict(raw_id=l["raw_id"], qty=float(l["qty"])) for l in lines])
        if lines else "",
        preset_product=preset_product,
        next_no=doc["batch_no"] if doc else next_batch_no(conn))


@app.route("/production")
def production():
    start, end, preset = resolve_range()
    with get_db() as conn:
        rows = conn.query(
            "SELECT * FROM productions WHERE prod_date >= ? AND prod_date <= ? "
            "ORDER BY prod_date DESC, id DESC", (start, end))
        for r in rows:
            r["lines"] = conn.query(
                "SELECT raw_name, qty FROM production_lines WHERE production_id = ? ORDER BY id", (r["id"],))
            q = float(r["qty_out"])
            r["unit_cost"] = money(float(r["total_cost"]) / q) if q > 0 else 0.0
        units = {i["id"]: i["unit"] for i in conn.query("SELECT id, unit FROM items")}
        return render_template("production.html", s=get_settings(conn), rows=rows, units=units,
                               start=start, end=end, preset=preset, presets=PRESETS, q="",
                               total_out_cost=money(sum(float(r["total_cost"]) for r in rows)))


@app.route("/production/new", methods=["GET", "POST"])
def production_new():
    with get_db() as conn:
        if request.method == "POST":
            return save_production(conn)
        return production_form(conn)


@app.route("/production/<int:pid>")
def production_view(pid):
    with get_db() as conn:
        doc = conn.query_one("SELECT * FROM productions WHERE id = ?", (pid,))
        if not doc:
            return redirect(url_for("production"))
        lines = conn.query(
            "SELECT l.*, i.unit FROM production_lines l LEFT JOIN items i ON i.id = l.raw_id "
            "WHERE l.production_id = ? ORDER BY l.id", (pid,))
        unit = conn.scalar("SELECT unit FROM items WHERE id = ?", (doc["product_id"],), "kg")
        total_in = sum(float(l["qty"]) for l in lines)
        q = float(doc["qty_out"])
        return render_template("production_view.html", s=get_settings(conn), doc=doc, lines=lines,
                               unit=unit, total_in=core.qty(total_in),
                               unit_cost=money(float(doc["total_cost"]) / q) if q > 0 else 0.0,
                               yield_pct=round(q / total_in * 100, 1) if total_in > 0 else 0.0)


@app.route("/production/<int:pid>/edit", methods=["GET", "POST"])
@admin_required
def production_edit(pid):
    with get_db() as conn:
        doc = conn.query_one("SELECT * FROM productions WHERE id = ?", (pid,))
        if not doc:
            return redirect(url_for("production"))
        if request.method == "POST":
            return save_production(conn, prod_id=pid)
        lines = conn.query("SELECT * FROM production_lines WHERE production_id = ? ORDER BY id", (pid,))
        return production_form(conn, doc=doc, lines=lines)


@app.route("/production/<int:pid>/delete", methods=["POST"])
@admin_required
def production_delete(pid):
    with get_db() as conn:
        conn.execute("DELETE FROM production_lines WHERE production_id = ?", (pid,))
        conn.execute("DELETE FROM productions WHERE id = ?", (pid,))
        core.rebuild_stock(conn)
        flash("Batch deleted - raw materials returned to stock.", "ok")
    return redirect(url_for("production"))


@app.route("/raw-materials")
def raw_materials():
    with get_db() as conn:
        rows = core.raw_material_rows(conn)
        products = [p for p in core.stock_rows(conn) if p["kind"] != "raw"]
        makeable = []
        for p in products:
            cm = core.can_make(conn, p["id"])
            if cm is not None:
                makeable.append(dict(id=p["id"], name=p["name"], unit=p["unit"],
                                     recipe=core.recipe_for(conn, p["id"]), **cm))
        return render_template(
            "raw_materials.html", s=get_settings(conn), rows=rows, makeable=makeable,
            products_without_recipe=[p for p in products if not any(m["id"] == p["id"] for m in makeable)],
            total_left=money(sum(r["left_value"] for r in rows)),
            total_bought=money(sum(r["bought_value"] for r in rows)))


@app.route("/recipes/<int:product_id>", methods=["GET", "POST"])
def recipe_edit(product_id):
    with get_db() as conn:
        product = conn.query_one("SELECT * FROM items WHERE id = ?", (product_id,))
        if not product or (product.get("kind") or "product") == "raw":
            return redirect(url_for("raw_materials"))
        if request.method == "POST":
            per = fnum("per_qty", 1) or 1
            conn.execute("DELETE FROM recipe_lines WHERE product_id = ?", (product_id,))
            n = 0
            for raw_id, q in zip(request.form.getlist("raw_id"), request.form.getlist("raw_qty")):
                try:
                    rid, q = int(raw_id), float(q or 0)
                except ValueError:
                    continue
                if q > 0 and rid != product_id:
                    conn.insert("recipe_lines", dict(product_id=product_id, raw_id=rid,
                                                     qty_per_unit=round(q / per, 6)))
                    n += 1
            flash("Recipe for " + product["name"] + (" saved." if n else " cleared."), "ok")
            return redirect(url_for("raw_materials"))
        raws, _ = _raws_and_products(conn)
        recipe = core.recipe_for(conn, product_id)
        return render_template("recipe_form.html", s=get_settings(conn), product=product,
                               raws=raws, recipe=recipe)


# --------------------------------------------------------------- reports

def _report(conn, start, end):
    sales_rows = conn.query(
        "SELECT * FROM sales WHERE invoice_date >= ? AND invoice_date <= ? ORDER BY invoice_date, id",
        (start, end))
    purch_rows = conn.query(
        "SELECT * FROM purchases WHERE bill_date >= ? AND bill_date <= ? ORDER BY bill_date, id",
        (start, end))
    sale_lines = conn.query(
        "SELECT sl.* FROM sale_lines sl JOIN sales s ON s.id = sl.sale_id "
        "WHERE s.invoice_date >= ? AND s.invoice_date <= ?", (start, end))
    purch_lines = conn.query(
        "SELECT pl.* FROM purchase_lines pl JOIN purchases p ON p.id = pl.purchase_id "
        "WHERE p.bill_date >= ? AND p.bill_date <= ?", (start, end))

    sale_tot = totals_from_lines(sale_lines)
    purch_tot = totals_from_lines(purch_lines)
    cogs = money(sum(float(r["cogs"]) for r in sales_rows))
    out_tax = money(sale_tot["cgst"] + sale_tot["sgst"] + sale_tot["igst"])
    in_tax = money(purch_tot["cgst"] + purch_tot["sgst"] + purch_tot["igst"])

    by_item = {}
    for l in sale_lines:
        b = by_item.setdefault(l["item_name"], dict(name=l["item_name"], qty=0.0, revenue=0.0, cost=0.0))
        b["qty"] += float(l["qty"])
        b["revenue"] += float(l["taxable"])
        b["cost"] += float(l["qty"]) * float(l["cost_rate"])
    items_sold = []
    for b in by_item.values():
        b["qty"] = qty(b["qty"])
        b["revenue"] = money(b["revenue"])
        b["cost"] = money(b["cost"])
        b["profit"] = money(b["revenue"] - b["cost"])
        b["margin"] = money(b["profit"] / b["revenue"] * 100) if b["revenue"] else 0.0
        items_sold.append(b)
    items_sold.sort(key=lambda x: -x["profit"])

    daily = {}
    for r in sales_rows:
        d = daily.setdefault(r["invoice_date"], dict(day=r["invoice_date"], sales=0.0, purchases=0.0, cogs=0.0))
        d["sales"] += float(r["taxable"])
        d["cogs"] += float(r["cogs"])
    for r in purch_rows:
        d = daily.setdefault(r["bill_date"], dict(day=r["bill_date"], sales=0.0, purchases=0.0, cogs=0.0))
        d["purchases"] += float(r["taxable"])
    # Fill the gaps so the time axis is evenly spaced. Only worth doing over a
    # span a chart can show; a wide range keeps just the days that had activity.
    if daily:
        first = max(start, min(daily))
        last = min(end, max(daily))
        try:
            d0 = datetime.strptime(first, "%Y-%m-%d").date()
            d1 = datetime.strptime(last, "%Y-%m-%d").date()
        except ValueError:
            d0 = d1 = None
        if d0 and d1 and 0 <= (d1 - d0).days <= 92:
            step = d0
            while step <= d1:
                daily.setdefault(step.isoformat(),
                                 dict(day=step.isoformat(), sales=0.0, purchases=0.0, cogs=0.0))
                step += timedelta(days=1)

    days = []
    for d in sorted(daily.values(), key=lambda x: x["day"]):
        d["sales"] = money(d["sales"])
        d["purchases"] = money(d["purchases"])
        d["profit"] = money(d["sales"] - d["cogs"])
        days.append(d)

    for d in days:
        d["dd"] = d["day"][8:10] + "/" + d["day"][5:7]

    return dict(
        trend_chart=charts.lines_pair(days, "dd", "sales", "profit", "Sales", "Gross profit"),
        item_chart=charts.hbars(items_sold[:10], "name", "profit"),
        start=start, end=end, sales_rows=sales_rows, purch_rows=purch_rows,
        sale_tot=sale_tot, purch_tot=purch_tot, cogs=cogs,
        gross_profit=money(sale_tot["taxable"] - cogs),
        margin=money((sale_tot["taxable"] - cogs) / sale_tot["taxable"] * 100) if sale_tot["taxable"] else 0.0,
        out_tax=out_tax, in_tax=in_tax, net_gst=money(out_tax - in_tax),
        sale_breakup=rate_wise_breakup(sale_lines),
        purch_breakup=rate_wise_breakup(purch_lines),
        items_sold=items_sold, days=days)


@app.route("/reports")
def reports():
    start, end, preset = resolve_range()
    with get_db() as conn:
        return render_template("reports.html", s=get_settings(conn),
                               r=_report(conn, start, end), preset=preset, presets=PRESETS)


# --------------------------------------------------------------- exports

def _csv(filename, header, rows):
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(header)
    w.writerows(rows)
    return Response(buf.getvalue(), mimetype="text/csv",
                    headers={"Content-Disposition": 'attachment; filename="{}"'.format(filename)})


@app.route("/export/<kind>.csv")
def export(kind):
    start, end, _preset = resolve_range()
    with get_db() as conn:
        if kind == "sales":
            rows = conn.query(
                "SELECT * FROM sales WHERE invoice_date >= ? AND invoice_date <= ? ORDER BY invoice_date, id",
                (start, end))
            return _csv("sales-register-{}-to-{}.csv".format(start, end),
                        ["Invoice No", "Date", "Customer", "State", "Supply Type", "Taxable",
                         "CGST", "SGST", "IGST", "Round Off", "Invoice Total", "Cost", "Profit", "Payment"],
                        [[r["invoice_no"], r["invoice_date"], r["party_name"],
                          STATE_NAME.get(r["state_code"], r["state_code"]),
                          "Inter-state" if r["interstate"] else "Intra-state",
                          r["taxable"], r["cgst"], r["sgst"], r["igst"], r["round_off"],
                          r["total"], r["cogs"], money(float(r["taxable"]) - float(r["cogs"])),
                          r["payment_mode"]] for r in rows])

        if kind == "purchases":
            rows = conn.query(
                "SELECT * FROM purchases WHERE bill_date >= ? AND bill_date <= ? ORDER BY bill_date, id",
                (start, end))
            return _csv("purchase-register-{}-to-{}.csv".format(start, end),
                        ["Bill No", "Date", "Supplier", "State", "Supply Type", "Taxable",
                         "CGST", "SGST", "IGST", "Round Off", "Bill Total", "Payment"],
                        [[r["bill_no"], r["bill_date"], r["party_name"],
                          STATE_NAME.get(r["state_code"], r["state_code"]),
                          "Inter-state" if r["interstate"] else "Intra-state",
                          r["taxable"], r["cgst"], r["sgst"], r["igst"], r["round_off"],
                          r["total"], r["payment_mode"]] for r in rows])

        if kind == "stock":
            rows = core.stock_rows(conn)
            return _csv("stock-{}.csv".format(today_str()),
                        ["Item", "HSN", "GST %", "Unit", "Stock Qty", "Avg Cost",
                         "Stock Value", "Sale Rate", "Low Stock At", "Status"],
                        [[r["name"], r["hsn"], r["gst_rate"], r["unit"], r["stock_qty"],
                          r["avg_cost"], r["stock_value"], r["sale_rate"],
                          r["low_stock_qty"], "LOW" if r["low"] else "OK"] for r in rows])

        if kind == "raw":
            out = []
            for r in core.raw_material_rows(conn):
                used_in = "; ".join("{}: {}".format(u["product_name"], u["q"]) for u in r["per_product"])
                out.append([r["name"], r["unit"], r["opening"], r["bought"], r["bought_value"],
                            r["used"], used_in, r["left"], r["avg_cost"], r["left_value"]])
            return _csv("raw-materials-{}.csv".format(today_str()),
                        ["Raw material", "Unit", "Opening", "Bought", "Bought value", "Used",
                         "Used in (product: qty)", "Left", "Avg cost", "Left value"], out)

        if kind == "production":
            rows = conn.query(
                "SELECT * FROM productions WHERE prod_date >= ? AND prod_date <= ? ORDER BY prod_date, id",
                (start, end))
            out = []
            for r in rows:
                for l in conn.query("SELECT * FROM production_lines WHERE production_id = ? ORDER BY id", (r["id"],)):
                    out.append([r["batch_no"], r["prod_date"], r["product_name"], r["qty_out"],
                                l["raw_name"], l["qty"], l["cost_rate"], l["cost"],
                                r["other_cost"], r["total_cost"]])
            return _csv("production-{}-to-{}.csv".format(start, end),
                        ["Batch", "Date", "Product", "Qty made", "Raw material", "Qty used",
                         "Cost rate", "Cost", "Other cost", "Batch total cost"], out)

        if kind == "gst":
            rep = _report(conn, start, end)
            out = [["OUTPUT TAX (on sales)", "", "", "", ""],
                   ["GST Rate %", "Taxable Value", "CGST", "SGST", "IGST"]]
            for b in rep["sale_breakup"]:
                out.append([b["gst_rate"], b["taxable"], b["cgst"], b["sgst"], b["igst"]])
            out.append(["Total", rep["sale_tot"]["taxable"], rep["sale_tot"]["cgst"],
                        rep["sale_tot"]["sgst"], rep["sale_tot"]["igst"]])
            out.append([])
            out.append(["INPUT TAX CREDIT (on purchases)", "", "", "", ""])
            out.append(["GST Rate %", "Taxable Value", "CGST", "SGST", "IGST"])
            for b in rep["purch_breakup"]:
                out.append([b["gst_rate"], b["taxable"], b["cgst"], b["sgst"], b["igst"]])
            out.append(["Total", rep["purch_tot"]["taxable"], rep["purch_tot"]["cgst"],
                        rep["purch_tot"]["sgst"], rep["purch_tot"]["igst"]])
            out.append([])
            out.append(["Output tax", rep["out_tax"]])
            out.append(["Input tax credit", rep["in_tax"]])
            out.append(["Net GST payable", rep["net_gst"]])
            return _csv("gst-summary-{}-to-{}.csv".format(start, end),
                        ["GST summary {} to {}".format(start, end), "", "", "", ""], out)

        if kind == "itemwise":
            rep = _report(conn, start, end)
            return _csv("item-profit-{}-to-{}.csv".format(start, end),
                        ["Item", "Qty Sold", "Revenue (taxable)", "Cost", "Profit", "Margin %"],
                        [[b["name"], b["qty"], b["revenue"], b["cost"], b["profit"], b["margin"]]
                         for b in rep["items_sold"]])

    return redirect(url_for("reports"))


# -------------------------------------------------------------- settings

@app.route("/admin", methods=["GET", "POST"])
def admin():
    """Unlock editing. On a fresh install this sets the PIN instead."""
    nxt = request.values.get("next") or url_for("dashboard")
    with get_db() as conn:
        s = get_settings(conn)
        first_time = not admin_configured(s)
        if request.method == "POST":
            if first_time:
                pin, confirm = f("pin"), f("confirm")
                if len(pin) < 4:
                    flash("Choose a PIN of at least 4 characters.", "error")
                elif pin != confirm:
                    flash("The two PINs did not match.", "error")
                else:
                    h, salt = core.make_pin(pin)
                    conn.execute("UPDATE settings SET admin_hash = ?, admin_salt = ? WHERE id = 1",
                                 (h, salt))
                    session["admin"] = True
                    flash("Admin PIN set. You can now edit and delete entries.", "ok")
                    return redirect(nxt)
            elif core.check_pin(f("pin"), s["admin_hash"], s["admin_salt"]):
                session["admin"] = True
                flash("Admin unlocked.", "ok")
                return redirect(nxt)
            else:
                flash("That PIN is not right.", "error")
        return render_template("admin.html", s=s, first_time=first_time, next=nxt)


@app.route("/admin/lock", methods=["POST"])
def admin_lock():
    session.pop("admin", None)
    flash("Admin locked.", "ok")
    return redirect(request.referrer or url_for("dashboard"))


@app.route("/admin/pin", methods=["POST"])
@admin_required
def admin_pin():
    """Change the PIN. The current one must be given again."""
    with get_db() as conn:
        s = get_settings(conn)
        if not core.check_pin(f("current"), s["admin_hash"], s["admin_salt"]):
            flash("The current PIN is not right.", "error")
        elif len(f("pin")) < 4:
            flash("Choose a PIN of at least 4 characters.", "error")
        elif f("pin") != f("confirm"):
            flash("The two new PINs did not match.", "error")
        else:
            h, salt = core.make_pin(f("pin"))
            conn.execute("UPDATE settings SET admin_hash = ?, admin_salt = ? WHERE id = 1", (h, salt))
            flash("Admin PIN changed.", "ok")
    return redirect(url_for("settings"))


@app.route("/settings", methods=["GET", "POST"])
def settings():
    with get_db() as conn:
        if request.method == "POST":
            conn.execute(
                "UPDATE settings SET business_name=?, gstin=?, address=?, state_code=?, "
                "phone=?, email=?, invoice_prefix=?, gst_enabled=?, bank_details=?, terms=? WHERE id=1",
                (f("business_name", "My Dry Fruits Business"), f("gstin").upper(), f("address"),
                 f("state_code", "27"), f("phone"), f("email"),
                 f("invoice_prefix", "SDF") or "SDF",
                 1 if request.form.get("gst_enabled") else 0,
                 f("bank_details"), f("terms")))
            flash("Settings saved.", "ok")
            return redirect(url_for("settings"))
        return render_template("settings.html", s=get_settings(conn))


@app.route("/healthz")
def healthz():
    return {"ok": True}


init_db()

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5000)), debug=True)
