"""Spicy Dry Fruits - daily inventory, GST and profit tracker."""
import csv
import io
import json
import os
import zipfile
from datetime import date, datetime, timedelta

from functools import wraps

from flask import (Flask, Response, abort, flash, g, redirect, render_template,
                   request, session, url_for)
from werkzeug.middleware.proxy_fix import ProxyFix

import books
import charts
import core
from core import (GST_RATES, MONEY_MODES, PAYMENT_MODES, STATE_CODES, STATE_NAME, UNITS,
                  amount_in_words, compute_line, money, qty, rate_wise_breakup,
                  today_str, totals_from_lines)
from db import IS_PG, get_db, init_db

app = Flask(__name__)
app.secret_key = os.environ.get("SECRET_KEY", "dev-secret-change-me")
# Render sits behind a proxy; trust its scheme/host so invite links are https.
app.wsgi_app = ProxyFix(app.wsgi_app, x_proto=1, x_host=1)
app.config.update(
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SAMESITE="Lax",
    SESSION_COOKIE_SECURE=bool(os.environ.get("RENDER")),
    PERMANENT_SESSION_LIFETIME=timedelta(days=30),
    # stylesheet and scripts are versioned with ?v=, so browsers may keep them
    SEND_FILE_MAX_AGE_DEFAULT=timedelta(days=7),
)

MIN_PASSWORD = 8
MAX_FAILED_LOGINS = 5
LOCKOUT_MINUTES = 15
LINK_DAYS = 7
# Pages anyone can open without signing in.
PUBLIC_ENDPOINTS = {"login", "logout", "set_password", "owner_setup", "static", "healthz"}


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


def resolve_range(default="month"):
    """Date range for a register or report page: an explicit start/end wins,
    otherwise the named preset, otherwise the default (this month)."""
    preset = (request.args.get("preset") or "").strip()
    start, end = request.args.get("start"), request.args.get("end")
    if start and end and not preset:
        return start, end, ""
    if preset:
        s, e = preset_range(preset)
        return s, e, preset
    s, e = preset_range(default)
    return s, e, default


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
                MONEY_MODES=MONEY_MODES,
                today=today_str(), is_admin=is_admin(),
                me=current_user() if request.endpoint not in ("static", "healthz") else None,
                is_owner=is_owner() if request.endpoint not in ("static", "healthz") else False)


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

        # last 14 days trend - two grouped queries rather than one per day
        first = (date.today() - timedelta(days=13)).isoformat()
        sv = {r["d"]: float(r["v"]) for r in conn.query(
            "SELECT invoice_date AS d, COALESCE(SUM(taxable),0) AS v FROM sales "
            "WHERE invoice_date >= ? AND invoice_date <= ? GROUP BY invoice_date", (first, t))}
        pv = {r["d"]: float(r["v"]) for r in conn.query(
            "SELECT bill_date AS d, COALESCE(SUM(taxable),0) AS v FROM purchases "
            "WHERE bill_date >= ? AND bill_date <= ? GROUP BY bill_date", (first, t))}
        trend = []
        for i in range(13, -1, -1):
            d = (date.today() - timedelta(days=i)).isoformat()
            trend.append(dict(day=d, sales=money(sv.get(d, 0.0)), purchases=money(pv.get(d, 0.0))))

        # the books: where the money is, and this month's net profit after expenses
        acc = books.dashboard_figures(conn, m_start, m_end, t)

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
            month_label=date.today().strftime("%B %Y"), acc=acc)


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
    mode = f("other_cost_mode", "Cash")
    header = dict(prod_date=f("prod_date", today_str()), product_id=product["id"],
                  product_name=product["name"], qty_out=core.qty(out_qty),
                  other_cost=money(fnum("other_cost")), notes=f("notes"),
                  other_cost_mode=mode if mode in PAYMENT_MODES else "Cash")
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
        lines = {}
        for l in conn.query(
                "SELECT l.production_id, l.raw_name, l.qty FROM production_lines l "
                "JOIN productions p ON p.id = l.production_id "
                "WHERE p.prod_date >= ? AND p.prod_date <= ? ORDER BY l.id", (start, end)):
            lines.setdefault(l["production_id"], []).append(l)
        for r in rows:
            r["lines"] = lines.get(r["id"], [])
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
    exp_tax = money(conn.scalar(
        "SELECT COALESCE(SUM(cgst + sgst + igst),0) AS v FROM expenses WHERE exp_date >= ? AND exp_date <= ?",
        (start, end)))
    expenses_total = money(conn.scalar(
        "SELECT COALESCE(SUM(amount),0) AS v FROM expenses WHERE exp_date >= ? AND exp_date <= ?",
        (start, end)))

    by_item = {}
    for l in sale_lines:
        b = by_item.setdefault(l["item_name"], dict(name=l["item_name"], qty=0.0, revenue=0.0, cost=0.0))
        b["qty"] += float(l["qty"])
        b["revenue"] += float(l["taxable"])
        b["cost"] += float(l.get("cost") or 0)
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
        out_tax=out_tax, in_tax=in_tax, exp_tax=exp_tax, net_gst=money(out_tax - in_tax - exp_tax),
        expenses_total=expenses_total,
        net_profit=money(sale_tot["taxable"] - cogs - expenses_total),
        sale_breakup=rate_wise_breakup(sale_lines),
        purch_breakup=rate_wise_breakup(purch_lines),
        items_sold=items_sold, days=days)


@app.route("/reports")
def reports():
    start, end, preset = resolve_range()
    with get_db() as conn:
        return render_template("reports.html", s=get_settings(conn),
                               r=_report(conn, start, end), preset=preset, presets=PRESETS)


# ------------------------------------------------------------- accounting
# Everything below reads from books.build(), which turns the documents into
# double entries when asked. There is no second copy of any figure, so a new,
# edited or deleted sale, purchase, batch, expense or voucher shows up in every
# ledger and statement the moment it is saved.

def _valid_date(v):
    try:
        return datetime.strptime(v, "%Y-%m-%d").date().isoformat()
    except (TypeError, ValueError):
        return ""


def _party_from_form(conn, prefix=""):
    """(party_id, party_name, state_code) from a saved contact or a typed name."""
    pid = f(prefix + "party_id")
    if pid:
        try:
            p = conn.query_one("SELECT * FROM parties WHERE id = ?", (int(pid),))
        except ValueError:
            p = None
        if p:
            return p["id"], p["name"], p["state_code"]
    return None, f(prefix + "party_name"), ""


def _accounts_by_type(conn, *types, keep=()):
    return [a for a in books.all_accounts(conn)
            if a["type"] in types and (a["active"] or a["id"] in keep)]


def _account(conn, acc_id):
    try:
        return conn.query_one("SELECT * FROM accounts WHERE id = ?", (int(acc_id),))
    except (TypeError, ValueError):
        return None


STATEMENT_TABS = [("profit_loss", "Profit & loss"), ("balance_sheet", "Balance sheet"),
                  ("trial_balance", "Trial balance"), ("outstanding", "Outstanding"),
                  ("day_book", "Journal"), ("ledger", "Ledgers")]


# ---- expenses

@app.route("/expenses")
def expenses():
    start, end, preset = resolve_range()
    q = (request.args.get("q") or "").strip()
    with get_db() as conn:
        sql = ("SELECT e.*, a.name AS head FROM expenses e LEFT JOIN accounts a ON a.id = e.account_id "
               "WHERE e.exp_date >= ? AND e.exp_date <= ?")
        params = [start, end]
        if q:
            like = "%" + q.lower() + "%"
            sql += (" AND (LOWER(e.description) LIKE ? OR LOWER(e.party_name) LIKE ? "
                    "OR LOWER(e.ref_no) LIKE ? OR LOWER(a.name) LIKE ?)")
            params += [like, like, like, like]
        rows = conn.query(sql + " ORDER BY e.exp_date DESC, e.id DESC", params)
        by_head = {}
        for r in rows:
            h = by_head.setdefault(r["head"] or "?", dict(name=r["head"] or "?", amount=0.0, n=0))
            h["amount"] = money(h["amount"] + float(r["amount"]))
            h["n"] += 1
        heads = sorted(by_head.values(), key=lambda h: -h["amount"])
        return render_template("expenses.html", s=get_settings(conn), rows=rows, heads=heads,
                               start=start, end=end, preset=preset, q=q, presets=PRESETS,
                               total=money(sum(float(r["total"]) for r in rows)),
                               amount=money(sum(float(r["amount"]) for r in rows)),
                               gst=money(sum(float(r["cgst"]) + float(r["sgst"]) + float(r["igst"]) for r in rows)))


def save_expense(conn, s, eid=None):
    back = url_for("expense_edit", eid=eid) if eid else url_for("expense_new")
    acc = _account(conn, f("account_id"))
    amount = money(fnum("amount"))
    if not acc or acc["type"] not in ("expense", "asset"):
        flash("Choose what the money was spent on.", "error")
        return redirect(back)
    if amount <= 0:
        flash("Enter the amount spent.", "error")
        return redirect(back)
    party_id, party_name, state = _party_from_form(conn)
    interstate = bool(state and state != s["state_code"]) or (not state and bool(f("interstate")))
    rate = fnum("gst_rate", 0)
    line = compute_line(1, amount, 0, rate if rate in GST_RATES else 0, interstate, bool(s["gst_enabled"]))
    mode = f("payment_mode", "Cash")
    if mode not in PAYMENT_MODES:
        mode = "Cash"
    if mode == books.CREDIT_MODE and not (party_id or party_name):
        flash("An expense on credit needs the supplier's name, so you can see what you owe them.", "error")
        return redirect(back)
    now = datetime.now().isoformat(timespec="seconds")
    data = dict(exp_date=_valid_date(f("exp_date")) or today_str(), account_id=acc["id"],
                party_id=party_id, party_name=party_name, ref_no=f("ref_no"),
                description=f("description"), amount=line["taxable"], gst_rate=line["gst_rate"],
                interstate=1 if interstate else 0, cgst=line["cgst"], sgst=line["sgst"],
                igst=line["igst"], total=line["total"], payment_mode=mode)
    if eid:
        sets = ", ".join(k + " = ?" for k in data)
        conn.execute("UPDATE expenses SET " + sets + ", updated_at = ?, edit_count = edit_count + 1 WHERE id = ?",
                     list(data.values()) + [now, eid])
    else:
        data["created_at"] = now
        eid = conn.insert("expenses", data)
    flash("Expense {} - \u20b9{} on {}. The books are updated.".format(
        "updated" if request.endpoint == "expense_edit" else "recorded", inr(data["total"]), acc["name"]), "ok")
    if f("again"):
        return redirect(url_for("expense_new"))
    return redirect(url_for("expense_view", eid=eid))


def expense_form(conn, s, doc=None):
    heads = _accounts_by_type(conn, "expense", "asset", keep=(doc["account_id"],) if doc else ())
    heads = [a for a in heads if a["type"] == "expense" or a["grp"] in ("Fixed Assets", "Loans & Advances")]
    parties = conn.query("SELECT * FROM parties ORDER BY name")
    return render_template("expense_form.html", s=s, doc=doc, heads=heads, parties=parties,
                           party_states_json=party_states_json(parties))


@app.route("/expenses/new", methods=["GET", "POST"])
def expense_new():
    with get_db() as conn:
        s = get_settings(conn)
        if request.method == "POST":
            return save_expense(conn, s)
        return expense_form(conn, s)


@app.route("/expenses/<int:eid>")
def expense_view(eid):
    with get_db() as conn:
        doc = conn.query_one("SELECT e.*, a.name AS head FROM expenses e LEFT JOIN accounts a "
                             "ON a.id = e.account_id WHERE e.id = ?", (eid,))
        if not doc:
            return redirect(url_for("expenses"))
        return render_template("expense_view.html", s=get_settings(conn), doc=doc,
                               entry=_entry_for(conn, "expense", eid, doc["exp_date"]))


@app.route("/expenses/<int:eid>/edit", methods=["GET", "POST"])
@admin_required
def expense_edit(eid):
    with get_db() as conn:
        s = get_settings(conn)
        doc = conn.query_one("SELECT * FROM expenses WHERE id = ?", (eid,))
        if not doc:
            return redirect(url_for("expenses"))
        if request.method == "POST":
            return save_expense(conn, s, eid=eid)
        return expense_form(conn, s, doc=doc)


@app.route("/expenses/<int:eid>/delete", methods=["POST"])
@admin_required
def expense_delete(eid):
    with get_db() as conn:
        conn.execute("DELETE FROM expenses WHERE id = ?", (eid,))
        flash("Expense deleted - the books are updated.", "ok")
    return redirect(url_for("expenses"))


def _entry_for(conn, kind, doc_id, day):
    """The double entry a document produced, with account names, for its page."""
    names = {a["id"]: a for a in books.all_accounts(conn)}
    for e in books.build(conn, start=day, end=day):
        if e["kind"] == kind and e["doc_id"] == doc_id:
            for l in e["lines"]:
                a = names.get(l["acc"], {})
                l["name"] = a.get("name", "?")
                l["code"] = a.get("code", "")
            return e
    return None


# ---- receipts and payments

@app.route("/vouchers")
def vouchers():
    start, end, preset = resolve_range()
    q = (request.args.get("q") or "").strip()
    vtype = request.args.get("type") or ""
    with get_db() as conn:
        sql = ("SELECT v.*, a.name AS account_name FROM vouchers v LEFT JOIN accounts a ON a.id = v.account_id "
               "WHERE v.v_date >= ? AND v.v_date <= ?")
        params = [start, end]
        if vtype in ("receipt", "payment"):
            sql += " AND v.v_type = ?"
            params.append(vtype)
        if q:
            like = "%" + q.lower() + "%"
            sql += (" AND (LOWER(v.party_name) LIKE ? OR LOWER(v.v_no) LIKE ? OR LOWER(v.notes) LIKE ? "
                    "OR LOWER(v.ref_no) LIKE ? OR LOWER(a.name) LIKE ?)")
            params += [like] * 5
        rows = conn.query(sql + " ORDER BY v.v_date DESC, v.id DESC", params)
        return render_template("vouchers.html", s=get_settings(conn), rows=rows, vtype=vtype,
                               start=start, end=end, preset=preset, q=q, presets=PRESETS,
                               received=money(sum(float(r["amount"]) for r in rows if r["v_type"] == "receipt")),
                               paid=money(sum(float(r["amount"]) for r in rows if r["v_type"] == "payment")))


def save_voucher(conn, vid=None):
    vtype = "payment" if f("v_type") == "payment" else "receipt"
    back = url_for("voucher_edit", vid=vid) if vid else url_for("voucher_new", type=vtype)
    acc = _account(conn, f("account_id"))
    amount = money(fnum("amount"))
    if not acc:
        flash("Choose the account this money is for.", "error")
        return redirect(back)
    if acc["sys_key"] in ("cash", "bank") and books.mode_key(f("payment_mode"), "cash") == acc["sys_key"]:
        flash("The money cannot come from and go to the same account.", "error")
        return redirect(back)
    if amount <= 0:
        flash("Enter the amount.", "error")
        return redirect(back)
    party_id, party_name, _state = _party_from_form(conn)
    if acc["sys_key"] in books.PARTY_KEYS and not (party_id or party_name):
        flash("Say which " + ("customer" if acc["sys_key"] == "debtors" else "supplier")
              + " this is, so their balance is right.", "error")
        return redirect(back)
    mode = f("payment_mode", "Cash")
    if mode not in MONEY_MODES:
        mode = "Cash"
    day = _valid_date(f("v_date")) or today_str()
    now = datetime.now().isoformat(timespec="seconds")
    data = dict(v_type=vtype, v_date=day, account_id=acc["id"], party_id=party_id,
                party_name=party_name, amount=amount, payment_mode=mode,
                ref_no=f("ref_no"), notes=f("notes"))
    if vid:
        sets = ", ".join(k + " = ?" for k in data)
        conn.execute("UPDATE vouchers SET " + sets + ", updated_at = ?, edit_count = edit_count + 1 WHERE id = ?",
                     list(data.values()) + [now, vid])
    else:
        data["v_no"] = books.next_number(conn, "vouchers", "v_no", "RCT" if vtype == "receipt" else "PMT", day)
        data["created_at"] = now
        vid = conn.insert("vouchers", data)
    flash(("Receipt" if vtype == "receipt" else "Payment") + " saved - balances updated.", "ok")
    return redirect(url_for("voucher_view", vid=vid))


def voucher_form(conn, vtype, doc=None):
    accounts = [a for a in books.all_accounts(conn) if a["active"] or (doc and a["id"] == doc["account_id"])]
    K = books.key_ids(conn)
    parties = conn.query("SELECT * FROM parties ORDER BY name")
    pre = dict(party_id=request.args.get("party_id", type=int),
               party_name=request.args.get("party_name") or "",
               amount=request.args.get("amount", type=float))
    return render_template("voucher_form.html", s=get_settings(conn), doc=doc, vtype=vtype,
                           accounts=accounts, parties=parties, K=K, pre=pre,
                           default_acc=K["debtors"] if vtype == "receipt" else K["creditors"],
                           party_accounts=json.dumps([K[k] for k in books.PARTY_KEYS]))


@app.route("/vouchers/new", methods=["GET", "POST"])
def voucher_new():
    with get_db() as conn:
        if request.method == "POST":
            return save_voucher(conn)
        vtype = "payment" if request.args.get("type") == "payment" else "receipt"
        return voucher_form(conn, vtype)


@app.route("/vouchers/<int:vid>")
def voucher_view(vid):
    with get_db() as conn:
        doc = conn.query_one("SELECT v.*, a.name AS account_name FROM vouchers v LEFT JOIN accounts a "
                             "ON a.id = v.account_id WHERE v.id = ?", (vid,))
        if not doc:
            return redirect(url_for("vouchers"))
        party = conn.query_one("SELECT * FROM parties WHERE id = ?", (doc["party_id"],)) if doc["party_id"] else None
        return render_template("voucher_view.html", s=get_settings(conn), doc=doc, party=party,
                               words=amount_in_words(doc["amount"]),
                               entry=_entry_for(conn, doc["v_type"], vid, doc["v_date"]))


@app.route("/vouchers/<int:vid>/edit", methods=["GET", "POST"])
@admin_required
def voucher_edit(vid):
    with get_db() as conn:
        doc = conn.query_one("SELECT * FROM vouchers WHERE id = ?", (vid,))
        if not doc:
            return redirect(url_for("vouchers"))
        if request.method == "POST":
            return save_voucher(conn, vid=vid)
        return voucher_form(conn, doc["v_type"], doc=doc)


@app.route("/vouchers/<int:vid>/delete", methods=["POST"])
@admin_required
def voucher_delete(vid):
    with get_db() as conn:
        conn.execute("DELETE FROM vouchers WHERE id = ?", (vid,))
        flash("Voucher deleted - balances updated.", "ok")
    return redirect(url_for("vouchers"))


# ---- journal

@app.route("/journal")
def day_book():
    start, end, preset = resolve_range()
    q = (request.args.get("q") or "").strip().lower()
    kind = request.args.get("kind") or ""
    with get_db() as conn:
        names = {a["id"]: a for a in books.all_accounts(conn)}
        entries = books.build(conn, start=start, end=end)
        if kind:
            entries = [e for e in entries if e["kind"] == kind]
        if q:
            entries = [e for e in entries if q in (e["ref"] or "").lower() or q in (e["narration"] or "").lower()
                       or any(q in names.get(l["acc"], {}).get("name", "").lower() for l in e["lines"])]
        dr = money(sum(l["dr"] for e in entries for l in e["lines"]))
        cr = money(sum(l["cr"] for e in entries for l in e["lines"]))
        shown = list(reversed(entries))[:400]
        for e in shown:
            for l in e["lines"]:
                a = names.get(l["acc"], {})
                l["name"], l["code"] = a.get("name", "?"), a.get("code", "")
        return render_template("journal.html", s=get_settings(conn), entries=shown, count=len(entries),
                               start=start, end=end, preset=preset, q=request.args.get("q") or "",
                               presets=PRESETS, kind=kind, dr=dr, cr=cr, tabs=STATEMENT_TABS)


def parse_journal_lines(conn):
    accounts = {a["id"]: a for a in books.all_accounts(conn)}
    parties = {p["id"]: p for p in conn.query("SELECT id, name FROM parties")}
    lines = []
    fields = zip(request.form.getlist("acc_id"), request.form.getlist("debit"),
                 request.form.getlist("credit"), request.form.getlist("line_party"),
                 request.form.getlist("memo"))
    for acc_id, d, c, party, memo in fields:
        try:
            acc_id = int(acc_id)
        except ValueError:
            continue
        try:
            d, c = money(float(d or 0)), money(float(c or 0))
        except ValueError:
            continue
        if acc_id not in accounts or (d <= 0 and c <= 0):
            continue
        net = money(d - c)
        pid = None
        try:
            pid = int(party) if party else None
        except ValueError:
            pid = None
        lines.append(dict(account_id=acc_id, debit=net if net > 0 else 0.0, credit=-net if net < 0 else 0.0,
                          party_id=pid if pid in parties else None,
                          party_name=parties[pid]["name"] if pid in parties else "",
                          memo=(memo or "").strip()))
    return [l for l in lines if l["debit"] or l["credit"]]


def save_journal(conn, jid=None):
    back = url_for("journal_edit", jid=jid) if jid else url_for("journal_new")
    lines = parse_journal_lines(conn)
    dr = money(sum(l["debit"] for l in lines))
    cr = money(sum(l["credit"] for l in lines))
    if len(lines) < 2:
        flash("A journal entry needs at least one debit and one credit line.", "error")
        return redirect(back)
    if abs(dr - cr) >= 0.005:
        flash("Debits (\u20b9{}) and credits (\u20b9{}) must be equal. Nothing was saved.".format(inr(dr), inr(cr)),
              "error")
        return redirect(back)
    day = _valid_date(f("j_date")) or today_str()
    now = datetime.now().isoformat(timespec="seconds")
    if jid:
        conn.execute("UPDATE journals SET j_date = ?, narration = ?, updated_at = ?, "
                     "edit_count = edit_count + 1 WHERE id = ?", (day, f("narration"), now, jid))
        conn.execute("DELETE FROM journal_lines WHERE journal_id = ?", (jid,))
    else:
        jid = conn.insert("journals", dict(j_no=books.next_number(conn, "journals", "j_no", "JV", day),
                                           j_date=day, narration=f("narration"), created_at=now))
    for l in lines:
        l["journal_id"] = jid
        conn.insert("journal_lines", l)
    flash("Journal entry saved - \u20b9{} posted.".format(inr(dr)), "ok")
    return redirect(url_for("journal_view", jid=jid))


def journal_form(conn, doc=None, lines=None):
    accounts = [a for a in books.all_accounts(conn)
                if a["active"] or (lines and a["id"] in {l["account_id"] for l in lines})]
    K = books.key_ids(conn)
    return render_template(
        "journal_form.html", s=get_settings(conn), doc=doc, accounts=accounts,
        parties=conn.query("SELECT id, name FROM parties ORDER BY name"), K=K,
        party_accounts=json.dumps([K[k] for k in books.PARTY_KEYS]),
        existing_json=json.dumps([dict(acc_id=l["account_id"], debit=float(l["debit"]), credit=float(l["credit"]),
                                       party_id=l["party_id"] or "", memo=l["memo"] or "")
                                  for l in (lines or [])]))


@app.route("/journal/new", methods=["GET", "POST"])
def journal_new():
    with get_db() as conn:
        if request.method == "POST":
            return save_journal(conn)
        return journal_form(conn)


@app.route("/journal/<int:jid>")
def journal_view(jid):
    with get_db() as conn:
        doc = conn.query_one("SELECT * FROM journals WHERE id = ?", (jid,))
        if not doc:
            return redirect(url_for("day_book"))
        lines = conn.query("SELECT l.*, a.name, a.code FROM journal_lines l LEFT JOIN accounts a "
                           "ON a.id = l.account_id WHERE l.journal_id = ? ORDER BY l.id", (jid,))
        return render_template("journal_view.html", s=get_settings(conn), doc=doc, lines=lines,
                               total=money(sum(float(l["debit"]) for l in lines)))


@app.route("/journal/<int:jid>/edit", methods=["GET", "POST"])
@admin_required
def journal_edit(jid):
    with get_db() as conn:
        doc = conn.query_one("SELECT * FROM journals WHERE id = ?", (jid,))
        if not doc:
            return redirect(url_for("day_book"))
        if request.method == "POST":
            return save_journal(conn, jid=jid)
        lines = conn.query("SELECT * FROM journal_lines WHERE journal_id = ? ORDER BY id", (jid,))
        return journal_form(conn, doc=doc, lines=lines)


@app.route("/journal/<int:jid>/delete", methods=["POST"])
@admin_required
def journal_delete(jid):
    with get_db() as conn:
        conn.execute("DELETE FROM journal_lines WHERE journal_id = ?", (jid,))
        conn.execute("DELETE FROM journals WHERE id = ?", (jid,))
        flash("Journal entry deleted.", "ok")
    return redirect(url_for("day_book"))


# ---- chart of accounts and ledgers

@app.route("/accounts")
def accounts():
    with get_db() as conn:
        rows = books.all_accounts(conn)
        bal = books.balances(books.build(conn, end=today_str()), rows)
        for r in rows:
            r["balance"] = bal.get(r["id"], 0.0)
        groups = []
        for typ, label in books.TYPES:
            mine = [r for r in rows if r["type"] == typ]
            groups.append(dict(type=typ, label=label, rows=mine,
                               total=money(sum(r["balance"] for r in mine))))
        return render_template("accounts.html", s=get_settings(conn), groups=groups,
                               group_options=json.dumps(books.GROUPS), types=books.TYPES,
                               check=books.inventory_check(conn))


@app.route("/accounts/save", methods=["POST"])
def accounts_save():
    with get_db() as conn:
        name = f("name")
        typ = f("type")
        grp = f("grp")
        if not name:
            flash("Give the account a name.", "error")
            return redirect(url_for("accounts"))
        acc_id = f("id")
        if acc_id and not is_admin():
            flash("Unlock admin to change an existing account.", "warn")
            return redirect(url_for("admin", next=url_for("accounts")))
        if acc_id:
            acc = _account(conn, acc_id)
            if not acc:
                return redirect(url_for("accounts"))
            if acc["sys_key"]:
                # the app posts to these by role: the name and code may change, the nature may not
                typ, grp = acc["type"], acc["grp"]
            if typ not in books.TYPE_NAME:
                typ = acc["type"]
            if grp not in books.GROUPS[typ]:
                grp = books.GROUPS[typ][-1]
            conn.execute("UPDATE accounts SET name = ?, code = ?, type = ?, grp = ?, notes = ? WHERE id = ?",
                         (name, f("code"), typ, grp, f("notes"), acc["id"]))
            flash("Account updated.", "ok")
        else:
            if typ not in books.TYPE_NAME:
                flash("Choose what kind of account this is.", "error")
                return redirect(url_for("accounts"))
            if grp not in books.GROUPS[typ]:
                grp = books.GROUPS[typ][-1]
            conn.insert("accounts", dict(name=name, code=f("code"), type=typ, grp=grp, sys_key="",
                                         notes=f("notes"), created_at=today_str()))
            flash("Account \u201c{}\u201d added.".format(name), "ok")
    return redirect(url_for("accounts"))


@app.route("/accounts/<int:acc_id>/toggle", methods=["POST"])
@admin_required
def accounts_toggle(acc_id):
    with get_db() as conn:
        acc = _account(conn, acc_id)
        if acc and acc["sys_key"]:
            flash("The app posts to \u201c{}\u201d on its own, so it cannot be archived.".format(acc["name"]), "error")
        elif acc:
            conn.execute("UPDATE accounts SET active = ? WHERE id = ?", (0 if acc["active"] else 1, acc_id))
            flash("Account archived - it keeps its history." if acc["active"] else "Account restored.", "ok")
    return redirect(url_for("accounts"))


@app.route("/ledger")
def ledger():
    start, end, preset = resolve_range("fy")
    party = request.args.get("party") or None
    with get_db() as conn:
        accs = books.all_accounts(conn)
        K = books.key_ids(conn)
        acc_id = request.args.get("account", type=int) or K["cash"]
        lg = books.ledger(conn, acc_id, start, end, party=party)
        if lg is None:
            return redirect(url_for("ledger"))
        parties = []
        if lg["account"]["sys_key"] in books.PARTY_KEYS:
            parties = books.outstanding(conn, max(end, today_str()))[lg["account"]["sys_key"]]["rows"]
        return render_template("ledger.html", s=get_settings(conn), lg=lg, accounts=accs,
                               parties=parties, party=party or "", start=start, end=end,
                               preset=preset, presets=PRESETS, tabs=STATEMENT_TABS)


# ---- statements

@app.route("/profit-loss")
def profit_loss():
    start, end, preset = resolve_range("fy")
    with get_db() as conn:
        return render_template("profit_loss.html", s=get_settings(conn), pl=books.profit_and_loss(conn, start, end),
                               start=start, end=end, preset=preset, presets=PRESETS, tabs=STATEMENT_TABS)


@app.route("/balance-sheet")
def balance_sheet():
    as_at = _valid_date(request.args.get("as_at") or "") or today_str()
    with get_db() as conn:
        return render_template("balance_sheet.html", s=get_settings(conn), bs=books.balance_sheet(conn, as_at),
                               check=books.inventory_check(conn) if as_at >= today_str() else None,
                               tabs=STATEMENT_TABS)


@app.route("/trial-balance")
def trial_balance():
    start, end, preset = resolve_range("fy")
    with get_db() as conn:
        return render_template("trial_balance.html", s=get_settings(conn), tb=books.trial_balance(conn, start, end),
                               start=start, end=end, preset=preset, presets=PRESETS, tabs=STATEMENT_TABS)


@app.route("/outstanding")
def outstanding():
    as_at = _valid_date(request.args.get("as_at") or "") or today_str()
    with get_db() as conn:
        K = books.key_ids(conn)
        return render_template("outstanding.html", s=get_settings(conn), o=books.outstanding(conn, as_at),
                               as_at=as_at, K=K, tabs=STATEMENT_TABS)


# --------------------------------------------------------------- exports

def _csv(filename, header, rows):
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(header)
    w.writerows(rows)
    return Response(buf.getvalue(), mimetype="text/csv",
                    headers={"Content-Disposition": 'attachment; filename="{}"'.format(filename)})


EXPORT_KINDS = ["sales", "purchases", "expenses", "vouchers", "production", "journal", "trial-balance",
                "profit-loss", "balance-sheet", "outstanding", "gst", "itemwise", "stock", "raw", "accounts"]


def export_table(conn, kind, start, end):
    """(filename, header, rows) for one register or statement, or None."""
    if kind == "sales":
        rows = conn.query(
            "SELECT * FROM sales WHERE invoice_date >= ? AND invoice_date <= ? ORDER BY invoice_date, id",
            (start, end))
        return ("sales-register-{}-to-{}.csv".format(start, end),
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
        return ("purchase-register-{}-to-{}.csv".format(start, end),
                ["Bill No", "Date", "Supplier", "State", "Supply Type", "Taxable",
                 "CGST", "SGST", "IGST", "Round Off", "Bill Total", "Payment"],
                [[r["bill_no"], r["bill_date"], r["party_name"],
                  STATE_NAME.get(r["state_code"], r["state_code"]),
                  "Inter-state" if r["interstate"] else "Intra-state",
                  r["taxable"], r["cgst"], r["sgst"], r["igst"], r["round_off"],
                  r["total"], r["payment_mode"]] for r in rows])

    if kind == "expenses":
        rows = conn.query(
            "SELECT e.*, a.name AS head FROM expenses e LEFT JOIN accounts a ON a.id = e.account_id "
            "WHERE e.exp_date >= ? AND e.exp_date <= ? ORDER BY e.exp_date, e.id", (start, end))
        return ("expenses-{}-to-{}.csv".format(start, end),
                ["Date", "Ref", "Head", "Description", "Paid to", "Amount", "GST %", "CGST", "SGST",
                 "IGST", "Total", "Payment"],
                [[r["exp_date"], r["ref_no"], r["head"], r["description"], r["party_name"], r["amount"],
                  r["gst_rate"], r["cgst"], r["sgst"], r["igst"], r["total"], r["payment_mode"]] for r in rows])

    if kind == "vouchers":
        rows = conn.query(
            "SELECT v.*, a.name AS account_name FROM vouchers v LEFT JOIN accounts a ON a.id = v.account_id "
            "WHERE v.v_date >= ? AND v.v_date <= ? ORDER BY v.v_date, v.id", (start, end))
        return ("receipts-payments-{}-to-{}.csv".format(start, end),
                ["Voucher No", "Date", "Type", "Account", "Party", "Amount", "Mode", "Ref", "Notes"],
                [[r["v_no"], r["v_date"], r["v_type"].title(), r["account_name"], r["party_name"],
                  r["amount"], r["payment_mode"], r["ref_no"], r["notes"]] for r in rows])

    if kind == "journal":
        names = {a["id"]: a for a in books.all_accounts(conn)}
        out = []
        for e in books.build(conn, start=start, end=end):
            for l in e["lines"]:
                a = names.get(l["acc"], {})
                out.append([e["date"], e["kind"].title(), e["ref"], e["narration"], a.get("code", ""),
                            a.get("name", "?"), l["party_name"], l["dr"] or "", l["cr"] or ""])
        return ("journal-{}-to-{}.csv".format(start, end),
                ["Date", "Type", "Ref", "Narration", "Code", "Account", "Party", "Debit", "Credit"], out)

    if kind == "ledger":
        lg = books.ledger(conn, request.args.get("account", type=int) or 0, start, end,
                          party=request.args.get("party") or None)
        if not lg:
            return None
        out = [["", "Opening balance", "", "", "", "", lg["opening"]]]
        out += [[r["date"], r["narration"], r["ref"], r["against"], r["dr"] or "", r["cr"] or "", r["balance"]]
                for r in lg["rows"]]
        out.append(["", "Closing balance", "", "", lg["tot_dr"], lg["tot_cr"], lg["closing"]])
        slug = "".join(ch if ch.isalnum() else "-" for ch in lg["account"]["name"].lower()).strip("-")
        return ("ledger-{}-{}-to-{}.csv".format(slug, start, end),
                ["Date", "Particulars", "Ref", "Against", "Debit", "Credit", "Balance"], out)

    if kind == "trial-balance":
        tb = books.trial_balance(conn, start, end)
        out = [[r["code"], r["name"], books.TYPE_NAME.get(r["type"], r["type"]), r["grp"],
                r["open_dr"], r["open_cr"], r["dr"], r["cr"], r["close_dr"], r["close_cr"]] for r in tb["rows"]]
        t = tb["tot"]
        out.append(["", "Total", "", "", t["open_dr"], t["open_cr"], t["dr"], t["cr"], t["close_dr"], t["close_cr"]])
        return ("trial-balance-{}-to-{}.csv".format(start, end),
                ["Code", "Account", "Type", "Group", "Opening Dr", "Opening Cr", "Debit", "Credit",
                 "Closing Dr", "Closing Cr"], out)

    if kind == "profit-loss":
        pl = books.profit_and_loss(conn, start, end)
        out = []
        for title, sec in (("Sales", pl["sales"]), ("Less: direct expenses", pl["direct"])):
            out.append([title, ""])
            out += [["  " + r["name"], r["amount"]] for r in sec["rows"]]
        out.append(["Gross profit", pl["gross"]])
        for title, sec in (("Add: other income", pl["ind_inc"]), ("Less: indirect expenses", pl["ind_exp"])):
            out.append([title, ""])
            out += [["  " + r["name"], r["amount"]] for r in sec["rows"]]
        out.append(["Net profit" if pl["net"] >= 0 else "Net loss", pl["net"]])
        return ("profit-and-loss-{}-to-{}.csv".format(start, end),
                ["Profit & loss {} to {}".format(start, end), "Amount"], out)

    if kind == "balance-sheet":
        bs = books.balance_sheet(conn, end)
        out = []
        for title, groups, total in (("ASSETS", bs["assets"], bs["total_assets"]),
                                     ("LIABILITIES", bs["liabilities"], bs["total_liab"]),
                                     ("CAPITAL", bs["equity"], bs["total_equity"])):
            out.append([title, "", ""])
            for g in groups:
                out.append(["  " + g["name"], "", g["total"]])
                out += [["    " + r["name"], r["amount"], ""] for r in g["rows"]]
            out.append(["Total " + title.lower(), "", total])
            out.append([])
        out.append(["Liabilities + capital", "", bs["total_le"]])
        return ("balance-sheet-as-at-{}.csv".format(end),
                ["Balance sheet as at {}".format(end), "Amount", "Total"], out)

    if kind == "outstanding":
        o = books.outstanding(conn, end)
        out = []
        for key, label in (("debtors", "Receivable"), ("creditors", "Payable")):
            out += [[label, r["name"], r["phone"], r["billed"], r["settled"], r["amount"], r["last"], r["days"]]
                    for r in o[key]["rows"]]
        return ("outstanding-as-at-{}.csv".format(end),
                ["Side", "Party", "Phone", "Billed", "Settled", "Outstanding", "Last entry", "Days since"], out)

    if kind == "accounts":
        rows = books.all_accounts(conn)
        bal = books.balances(books.build(conn, end=end), rows)
        return ("chart-of-accounts-{}.csv".format(end),
                ["Code", "Account", "Type", "Group", "Balance as at " + end, "Active"],
                [[r["code"], r["name"], books.TYPE_NAME.get(r["type"]), r["grp"], bal.get(r["id"], 0.0),
                  "Yes" if r["active"] else "No"] for r in rows])

    if kind == "stock":
        rows = core.stock_rows(conn)
        return ("stock-{}.csv".format(today_str()),
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
        return ("raw-materials-{}.csv".format(today_str()),
                ["Raw material", "Unit", "Opening", "Bought", "Bought value", "Used",
                 "Used in (product: qty)", "Left", "Avg cost", "Left value"], out)

    if kind == "production":
        rows = conn.query(
            "SELECT * FROM productions WHERE prod_date >= ? AND prod_date <= ? ORDER BY prod_date, id",
            (start, end))
        lines = {}
        for l in conn.query("SELECT l.* FROM production_lines l JOIN productions p ON p.id = l.production_id "
                            "WHERE p.prod_date >= ? AND p.prod_date <= ? ORDER BY l.id", (start, end)):
            lines.setdefault(l["production_id"], []).append(l)
        out = []
        for r in rows:
            for l in lines.get(r["id"], []):
                out.append([r["batch_no"], r["prod_date"], r["product_name"], r["qty_out"],
                            l["raw_name"], l["qty"], l["cost_rate"], l["cost"],
                            r["other_cost"], r["total_cost"]])
        return ("production-{}-to-{}.csv".format(start, end),
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
        exp = conn.query_one(
            "SELECT COALESCE(SUM(amount),0) AS a, COALESCE(SUM(cgst),0) AS c, COALESCE(SUM(sgst),0) AS s, "
            "COALESCE(SUM(igst),0) AS i FROM expenses WHERE exp_date >= ? AND exp_date <= ?", (start, end))
        exp_tax = money(float(exp["c"]) + float(exp["s"]) + float(exp["i"]))
        out.append([])
        out.append(["INPUT TAX CREDIT (on expenses)", "", "", "", ""])
        out.append(["All rates", money(exp["a"]), money(exp["c"]), money(exp["s"]), money(exp["i"])])
        out.append([])
        out.append(["Output tax", rep["out_tax"]])
        out.append(["Input tax credit (purchases + expenses)", money(rep["in_tax"] + exp_tax)])
        out.append(["Net GST payable", money(rep["out_tax"] - rep["in_tax"] - exp_tax)])
        return ("gst-summary-{}-to-{}.csv".format(start, end),
                ["GST summary {} to {}".format(start, end), "", "", "", ""], out)

    if kind == "itemwise":
        rep = _report(conn, start, end)
        return ("item-profit-{}-to-{}.csv".format(start, end),
                ["Item", "Qty Sold", "Revenue (taxable)", "Cost", "Profit", "Margin %"],
                [[b["name"], b["qty"], b["revenue"], b["cost"], b["profit"], b["margin"]]
                 for b in rep["items_sold"]])
    return None


@app.route("/export/<kind>.csv")
def export(kind):
    start, end, _preset = resolve_range()
    with get_db() as conn:
        table = export_table(conn, kind, start, end)
    if table is None:
        return redirect(url_for("reports"))
    return _csv(*table)


@app.route("/export/books.zip")
def export_books():
    """Every register and statement for a period in one download - the pack
    to hand an accountant at year end."""
    start, end, _preset = resolve_range("fy")
    buf = io.BytesIO()
    with get_db() as conn:
        s = get_settings(conn)
        with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
            for kind in EXPORT_KINDS:
                table = export_table(conn, kind, start, end)
                if not table:
                    continue
                name, header, rows = table
                b = io.StringIO()
                w = csv.writer(b)
                w.writerow(header)
                w.writerows(rows)
                z.writestr(name, b.getvalue())
            z.writestr("README.txt",
                       "{}\nBooks pack for {} to {}, exported {}.\n\n"
                       "Every file is a CSV that opens in Excel or Google Sheets.\n"
                       "The journal, trial balance, profit & loss and balance sheet are\n"
                       "generated from the same sales, purchases, production, expenses and\n"
                       "receipts/payments listed in the registers, so they reconcile.\n"
                       "Figures are a working view for planning and for your accountant;\n"
                       "they are not a filed return.\n".format(s["business_name"], start, end,
                                                                datetime.now().strftime("%d %b %Y %H:%M")))
    return Response(buf.getvalue(), mimetype="application/zip",
                    headers={"Content-Disposition": 'attachment; filename="books-{}-to-{}.zip"'.format(start, end)})


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
                "phone=?, email=?, invoice_prefix=?, gst_enabled=?, bank_details=?, terms=?, "
                "books_start=? WHERE id=1",
                (f("business_name", "My Dry Fruits Business"), f("gstin").upper(), f("address"),
                 f("state_code", "27"), f("phone"), f("email"),
                 f("invoice_prefix", "SDF") or "SDF",
                 1 if request.form.get("gst_enabled") else 0,
                 f("bank_details"), f("terms"), _valid_date(f("books_start"))))
            flash("Settings saved.", "ok")
            return redirect(url_for("settings"))
        return render_template("settings.html", s=get_settings(conn))


# ----------------------------------------------------------- sign-in gate

def current_user():
    """The signed-in user, re-checked against the database on every request so a
    removed, disabled or password-changed account is signed out at once."""
    if "user" in g:
        return g.user
    g.user = None
    uid = session.get("uid")
    if uid:
        with get_db() as conn:
            u = conn.query_one("SELECT * FROM users WHERE id = ?", (uid,))
        if u and u["active"] and u["pw_hash"] and u["session_version"] == session.get("sv"):
            g.user = u
        else:
            session.pop("uid", None)
            session.pop("sv", None)
    return g.user


def is_owner():
    u = current_user()
    return bool(u and u["role"] == "owner")


def owner_required(view):
    @wraps(view)
    def guard(*args, **kwargs):
        if not is_owner():
            abort(403)
        return view(*args, **kwargs)
    return guard


def safe_next(target):
    """Only ever send people back to a page on this site."""
    if target and target.startswith("/") and not target.startswith("//") and "\\" not in target:
        return target
    return url_for("dashboard")


@app.before_request
def require_login():
    if request.endpoint in PUBLIC_ENDPOINTS or request.endpoint is None:
        return None
    if current_user():
        return None
    if request.method == "GET":
        return redirect(url_for("login", next=request.full_path.rstrip("?")))
    return redirect(url_for("login"))


def start_session(conn, u):
    session.clear()
    session.permanent = True
    session["uid"] = u["id"]
    session["sv"] = u["session_version"]
    conn.execute("UPDATE users SET failed_count = 0, locked_until = '', last_login = ? WHERE id = ?",
                 (core.now_iso(), u["id"]))


def setup_code_ok(conn, code):
    """Proves the first owner sign-up is really the owner: the existing admin PIN
    or a SETUP_CODE env var. A local install with neither needs no code."""
    s = get_settings(conn)
    env_code = os.environ.get("SETUP_CODE", "").strip()
    if env_code and code and core.hmac.compare_digest(code, env_code):
        return True
    if admin_configured(s) and core.check_pin(code, s["admin_hash"], s["admin_salt"]):
        return True
    return not env_code and not admin_configured(s) and not IS_PG


def needs_owner_setup(conn):
    return not conn.scalar("SELECT COUNT(*) AS c FROM users WHERE pw_hash <> '' AND active = 1")


@app.route("/login", methods=["GET", "POST"])
def login():
    nxt = safe_next(request.values.get("next"))
    if current_user():
        return redirect(nxt)
    with get_db() as conn:
        s = get_settings(conn)
        if needs_owner_setup(conn):
            return redirect(url_for("owner_setup", next=nxt))
        email = core.normalize_email(f("email"))
        if request.method == "POST":
            u = conn.query_one("SELECT * FROM users WHERE email = ?", (email,))
            now = core.now_iso()
            if u and u["locked_until"] and u["locked_until"] > now:
                flash("Too many wrong tries. Wait {} minutes and try again.".format(LOCKOUT_MINUTES), "error")
            elif u and u["active"] and u["pw_hash"] and core.check_pin(f("password"), u["pw_hash"], u["pw_salt"]):
                start_session(conn, u)
                return redirect(nxt)
            else:
                if not (u and u["pw_hash"]):
                    core.hash_pin("x", "00" * 16)   # same work either way, so timing reveals nothing
                if u and u["active"] and not u["pw_hash"]:
                    flash("Your access is ready but not activated. Ask the owner for your set-password link.", "warn")
                else:
                    if u:
                        fails = (u["failed_count"] or 0) + 1
                        lock = core.iso_after(minutes=LOCKOUT_MINUTES) if fails >= MAX_FAILED_LOGINS else ""
                        conn.execute("UPDATE users SET failed_count = ?, locked_until = ? WHERE id = ?",
                                     (0 if lock else fails, lock, u["id"]))
                    flash("That email and password do not match an account with access.", "error")
        return render_template("login.html", s=s, mode="login", next=nxt, email=email)


@app.route("/setup", methods=["GET", "POST"])
def owner_setup():
    """First run only: an owner on the access list sets their password."""
    nxt = safe_next(request.values.get("next"))
    with get_db() as conn:
        s = get_settings(conn)
        if not needs_owner_setup(conn):
            return redirect(url_for("login", next=nxt))
        need_code = not setup_code_ok(conn, "")
        email = core.normalize_email(f("email"))
        if request.method == "POST":
            u = conn.query_one("SELECT * FROM users WHERE email = ? AND role = 'owner' AND active = 1", (email,))
            pw, confirm = f("password"), f("confirm")
            if not u:
                flash("That email is not an owner on this tool's access list.", "error")
            elif need_code and not setup_code_ok(conn, f("code")):
                flash("The setup code is not right.", "error")
            elif len(pw) < MIN_PASSWORD:
                flash("Choose a password of at least {} characters.".format(MIN_PASSWORD), "error")
            elif pw != confirm:
                flash("The two passwords did not match.", "error")
            else:
                h, salt = core.make_pin(pw)
                conn.execute("UPDATE users SET pw_hash = ?, pw_salt = ?, token_hash = '', "
                             "session_version = session_version + 1 WHERE id = ?", (h, salt, u["id"]))
                u = conn.query_one("SELECT * FROM users WHERE id = ?", (u["id"],))
                start_session(conn, u)
                flash("Password set. You are signed in.", "ok")
                return redirect(nxt)
        return render_template("login.html", s=s, mode="setup", next=nxt, email=email,
                               need_code=need_code, has_pin=admin_configured(s))


@app.route("/set-password/<token>", methods=["GET", "POST"])
def set_password(token):
    """Where an invite or reset link lands."""
    with get_db() as conn:
        s = get_settings(conn)
        u = conn.query_one("SELECT * FROM users WHERE token_hash = ? AND token_hash <> ''",
                           (core.token_hash(token),))
        if not u or not u["active"] or (u["token_expires"] or "") < core.now_iso():
            flash("This link has expired or was already used. Ask the owner for a new one.", "error")
            return render_template("login.html", s=s, mode="dead_link", next="", email="")
        if request.method == "POST":
            pw, confirm = f("password"), f("confirm")
            if len(pw) < MIN_PASSWORD:
                flash("Choose a password of at least {} characters.".format(MIN_PASSWORD), "error")
            elif pw != confirm:
                flash("The two passwords did not match.", "error")
            else:
                h, salt = core.make_pin(pw)
                conn.execute("UPDATE users SET pw_hash = ?, pw_salt = ?, token_hash = '', token_expires = '', "
                             "session_version = session_version + 1 WHERE id = ?", (h, salt, u["id"]))
                u = conn.query_one("SELECT * FROM users WHERE id = ?", (u["id"],))
                start_session(conn, u)
                flash("Password set. Welcome!", "ok")
                return redirect(url_for("dashboard"))
        return render_template("login.html", s=s, mode="set", next="", email=u["email"])


@app.route("/logout", methods=["GET", "POST"])
def logout():
    session.clear()
    flash("Signed out.", "ok")
    return redirect(url_for("login"))


@app.route("/account", methods=["GET", "POST"])
def account():
    u = current_user()
    with get_db() as conn:
        s = get_settings(conn)
        if request.method == "POST":
            if not core.check_pin(f("current"), u["pw_hash"], u["pw_salt"]):
                flash("Your current password is not right.", "error")
            elif len(f("password")) < MIN_PASSWORD:
                flash("Choose a password of at least {} characters.".format(MIN_PASSWORD), "error")
            elif f("password") != f("confirm"):
                flash("The two new passwords did not match.", "error")
            else:
                h, salt = core.make_pin(f("password"))
                conn.execute("UPDATE users SET pw_hash = ?, pw_salt = ?, session_version = session_version + 1 "
                             "WHERE id = ?", (h, salt, u["id"]))
                u = conn.query_one("SELECT * FROM users WHERE id = ?", (u["id"],))
                start_session(conn, u)   # stay signed in here; every other device is signed out
                flash("Password changed. Other devices have been signed out.", "ok")
            return redirect(url_for("account"))
        return render_template("account.html", s=s, u=u)


def issue_link(conn, user_id):
    token, th = core.new_token()
    conn.execute("UPDATE users SET token_hash = ?, token_expires = ? WHERE id = ?",
                 (th, core.iso_after(days=LINK_DAYS), user_id))
    return url_for("set_password", token=token, _external=True)


@app.route("/users")
@owner_required
def users():
    with get_db() as conn:
        rows = conn.query("SELECT * FROM users ORDER BY role DESC, email")
        return render_template("users.html", s=get_settings(conn), rows=rows,
                               new_link=session.pop("new_link", None), now=core.now_iso(),
                               me=current_user(), link_days=LINK_DAYS)


@app.route("/users/add", methods=["POST"])
@owner_required
def users_add():
    email = core.normalize_email(f("email"))
    role = "owner" if f("role") == "owner" else "member"
    if not core.valid_email(email):
        flash("Enter a valid email address.", "error")
        return redirect(url_for("users"))
    with get_db() as conn:
        u = conn.query_one("SELECT * FROM users WHERE email = ?", (email,))
        if u:
            conn.execute("UPDATE users SET active = 1, role = ?, name = ? WHERE id = ?",
                         (role, f("name") or u["name"], u["id"]))
            uid = u["id"]
        else:
            uid = conn.insert("users", dict(email=email, name=f("name"), role=role, created_at=core.now_iso()))
        if (u or {}).get("pw_hash"):
            flash("{} already had an account; access is on.".format(email), "ok")
        else:
            session["new_link"] = dict(email=email, url=issue_link(conn, uid), kind="invite")
            flash("Access granted to {}. Send them the link below to set a password.".format(email), "ok")
    return redirect(url_for("users"))


def _other_owners(conn, uid):
    return conn.scalar("SELECT COUNT(*) AS c FROM users WHERE role = 'owner' AND active = 1 AND id <> ?", (uid,))


@app.route("/users/<int:uid>/<action>", methods=["POST"])
@owner_required
def users_action(uid, action):
    me = current_user()
    with get_db() as conn:
        u = conn.query_one("SELECT * FROM users WHERE id = ?", (uid,))
        if not u:
            abort(404)
        if action == "link":
            kind = "reset" if u["pw_hash"] else "invite"
            session["new_link"] = dict(email=u["email"], url=issue_link(conn, uid), kind=kind)
            flash("New link made for {}. Any earlier link for them no longer works.".format(u["email"]), "ok")
        elif action in ("disable", "remove", "member") and uid == me["id"]:
            flash("You cannot do that to your own account.", "error")
        elif action in ("disable", "remove", "member") and u["role"] == "owner" and not _other_owners(conn, uid):
            flash("Keep at least one owner who can manage access.", "error")
        elif action == "disable":
            conn.execute("UPDATE users SET active = 0, token_hash = '', session_version = session_version + 1 "
                         "WHERE id = ?", (uid,))
            flash("{} can no longer sign in.".format(u["email"]), "ok")
        elif action == "enable":
            conn.execute("UPDATE users SET active = 1, failed_count = 0, locked_until = '' WHERE id = ?", (uid,))
            flash("{} can sign in again.".format(u["email"]), "ok")
        elif action == "remove":
            conn.execute("DELETE FROM users WHERE id = ?", (uid,))
            flash("{} was removed.".format(u["email"]), "ok")
        elif action == "owner":
            conn.execute("UPDATE users SET role = 'owner' WHERE id = ?", (uid,))
            flash("{} can now manage access.".format(u["email"]), "ok")
        elif action == "member":
            conn.execute("UPDATE users SET role = 'member' WHERE id = ?", (uid,))
            flash("{} is now a regular user.".format(u["email"]), "ok")
        else:
            abort(404)
    return redirect(url_for("users"))


@app.route("/healthz")
def healthz():
    return {"ok": True}


def _boot():
    """Schema, chart of accounts, and a one-off replay when an upgrade added a
    computed column."""
    needs_rebuild = init_db()
    with get_db() as conn:
        if IS_PG:   # two workers boot together; let one seed at a time
            conn.execute("SELECT pg_advisory_xact_lock(724100)")
        books.ensure_accounts(conn)
        if needs_rebuild:
            core.rebuild_stock(conn)


_boot()


def _load_secret():
    """Without SECRET_KEY, sign sessions with a random key kept in the database
    rather than a guessable default, so nobody can forge a login cookie."""
    if os.environ.get("SECRET_KEY"):
        return
    with get_db() as conn:
        conn.execute("UPDATE settings SET app_secret = ? WHERE id = 1 AND "
                     "(app_secret IS NULL OR app_secret = '')", (os.urandom(32).hex(),))
    with get_db() as conn:   # re-read: another worker may have written first
        app.secret_key = conn.scalar("SELECT app_secret FROM settings WHERE id = 1")


_load_secret()

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5000)), debug=True)
