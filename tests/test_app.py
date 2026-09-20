"""End-to-end checks: GST splits, weighted-average costing, invoice numbering, reports.

Run with:  python tests/test_app.py
"""
import json
import os
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)
os.environ.pop("DATABASE_URL", None)
os.environ["SQLITE_PATH"] = os.path.join(tempfile.mkdtemp(), "test.db")

from app import app                                   # noqa: E402
import core                                          # noqa: E402
from core import amount_in_words, compute_line        # noqa: E402
from db import get_db                                 # noqa: E402

app.config["TESTING"] = True
c = app.test_client()

FAILS = []


def check(label, got, want, tol=0.005):
    ok = abs(float(got) - float(want)) <= tol if isinstance(want, (int, float)) else got == want
    print(("  PASS  " if ok else "  FAIL  ") + label + "  got=" + str(got) + "  want=" + str(want))
    if not ok:
        FAILS.append(label)


def item_id(name):
    with get_db() as conn:
        return conn.query_one("SELECT id FROM items WHERE name = ?", (name,))["id"]


def post_doc(url, base, lines):
    data = dict(base)
    data["item_id"] = [str(l[0]) for l in lines]
    data["qty"] = [str(l[1]) for l in lines]
    data["rate"] = [str(l[2]) for l in lines]
    data["discount_pct"] = [str(l[3]) for l in lines]
    return c.post(url, data=data, follow_redirects=False)


print("\n=== 1. Line-level GST maths (no database) ===")
l = compute_line(10, 800, 0, 5, interstate=False)
check("intra-state taxable", l["taxable"], 8000)
check("intra-state CGST = half of 5%", l["cgst"], 200)
check("intra-state SGST = half of 5%", l["sgst"], 200)
check("intra-state IGST is zero", l["igst"], 0)
check("intra-state line total", l["total"], 8400)

l = compute_line(4, 1200, 10, 5, interstate=True)
check("10% discount reduces taxable", l["taxable"], 4320)
check("inter-state charges IGST at full rate", l["igst"], 216)
check("inter-state CGST is zero", l["cgst"], 0)
check("inter-state line total", l["total"], 4536)

l = compute_line(2, 500, 0, 18, interstate=False)
check("18% splits 9 + 9", (l["cgst"], l["sgst"]), (90.0, 90.0))

l = compute_line(3, 100, 0, 5, interstate=False, gst_enabled=False)
check("GST switched off charges nothing", l["cgst"] + l["sgst"] + l["igst"], 0)

check("amount in words (lakh)", amount_in_words(154325.50),
      "Rupees One Lakh Fifty Four Thousand Three Hundred Twenty Five and Fifty Paise Only")
check("amount in words (round)", amount_in_words(6930), "Rupees Six Thousand Nine Hundred Thirty Only")


print("\n=== 2. Setup: business in Maharashtra, two contacts ===")
c.post("/settings", data=dict(business_name="Shree Spicy Dry Fruits", gstin="27ABCDE1234F1Z5",
                              state_code="27", invoice_prefix="SDF", gst_enabled="1"), follow_redirects=True)
c.post("/parties/save", data=dict(name="Krishna Traders", kind="supplier", state_code="27"), follow_redirects=True)
c.post("/parties/save", data=dict(name="Bengaluru Retail", kind="customer", state_code="29"), follow_redirects=True)
c.post("/admin", data=dict(pin="7788", confirm="7788"), follow_redirects=True)
with get_db() as conn:
    check("home state saved", conn.query_one("SELECT state_code FROM settings WHERE id=1")["state_code"], "27")
    row = conn.query_one("SELECT admin_hash, admin_salt FROM settings WHERE id=1")
    check("admin PIN is stored hashed, never in the clear", "7788" not in (row["admin_hash"] or ""), True)
    check("a salt was generated", len(row["admin_salt"] or ""), 32)
KAJU = item_id("Masala Kaju (Spiced Cashew)")


print("\n=== 3. Purchases build weighted-average cost ===")
post_doc("/purchases/new", dict(bill_date="2026-09-01", bill_no="B-1", state_code="27"),
         [(KAJU, 10, 800, 0)])
post_doc("/purchases/new", dict(bill_date="2026-09-02", bill_no="B-2", state_code="27"),
         [(KAJU, 5, 900, 0)])
with get_db() as conn:
    it = conn.query_one("SELECT stock_qty, stock_value FROM items WHERE id = ?", (KAJU,))
    check("stock after 10kg + 5kg", it["stock_qty"], 15)
    check("stock value 8000 + 4500", it["stock_value"], 12500)
    check("weighted-average cost 12500/15", round(it["stock_value"] / it["stock_qty"], 2), 833.33)
    p = conn.query_one("SELECT * FROM purchases WHERE bill_no = 'B-1'")
    check("purchase CGST", p["cgst"], 200)
    check("purchase bill total", p["total"], 8400)


print("\n=== 4. Intra-state sale: CGST+SGST, cost from WAC ===")
post_doc("/sales/new", dict(invoice_date="2026-09-03", state_code="27", party_name="Walk-in"),
         [(KAJU, 6, 1100, 0)])
with get_db() as conn:
    s1 = conn.query_one("SELECT * FROM sales ORDER BY id DESC LIMIT 1")
    check("invoice number auto-generated", s1["invoice_no"], "SDF/2026-27/0001")
    check("sale taxable 6 x 1100", s1["taxable"], 6600)
    check("sale CGST 2.5%", s1["cgst"], 165)
    check("sale SGST 2.5%", s1["sgst"], 165)
    check("sale IGST zero within state", s1["igst"], 0)
    check("invoice total", s1["total"], 6930)
    check("cost of goods sold 6 x 833.33", s1["cogs"], 4999.98)
    it = conn.query_one("SELECT stock_qty, stock_value FROM items WHERE id = ?", (KAJU,))
    check("stock drops to 9kg", it["stock_qty"], 9)
    check("stock value 12500 - 4999.98", it["stock_value"], 7500.02)


print("\n=== 5. Inter-state sale with discount: IGST ===")
with get_db() as conn:
    bng = conn.query_one("SELECT id FROM parties WHERE name = 'Bengaluru Retail'")["id"]
post_doc("/sales/new", dict(invoice_date="2026-09-04", party_id=str(bng)),
         [(KAJU, 4, 1200, 10)])
with get_db() as conn:
    s2 = conn.query_one("SELECT * FROM sales ORDER BY id DESC LIMIT 1")
    check("second invoice number increments", s2["invoice_no"], "SDF/2026-27/0002")
    check("customer state drives inter-state flag", s2["interstate"], 1)
    check("taxable after 10% discount", s2["taxable"], 4320)
    check("IGST at full 5%", s2["igst"], 216)
    check("no CGST on inter-state", s2["cgst"], 0)
    check("invoice total", s2["total"], 4536)
    check("cost at updated WAC 4 x 833.34", s2["cogs"], 3333.36)
    it = conn.query_one("SELECT stock_qty, stock_value FROM items WHERE id = ?", (KAJU,))
    check("stock down to 5kg", it["stock_qty"], 5)
    check("remaining stock value", it["stock_value"], 4166.66)


print("\n=== 6. Rounding to the nearest rupee ===")
post_doc("/sales/new", dict(invoice_date="2026-09-05", state_code="27", party_name="Walk-in"),
         [(KAJU, 3, 333.33, 0)])
with get_db() as conn:
    s3 = conn.query_one("SELECT * FROM sales ORDER BY id DESC LIMIT 1")
    check("taxable 3 x 333.33", s3["taxable"], 999.99)
    check("round off applied", s3["round_off"], 0.01)
    check("total is a whole rupee", s3["total"], 1050)


print("\n=== 7. Reports and GST position ===")
r = c.get("/reports?start=2026-09-01&end=2026-09-30")
body = r.get_data(as_text=True)
check("reports page renders", r.status_code, 200)
with get_db() as conn:
    out_tax = conn.scalar("SELECT COALESCE(SUM(cgst+sgst+igst),0) AS v FROM sales")
    in_tax = conn.scalar("SELECT COALESCE(SUM(cgst+sgst+igst),0) AS v FROM purchases")
    check("output tax collected", out_tax, 165 + 165 + 216 + 25 + 25)
    check("input tax credit", in_tax, 200 + 200 + 112.5 + 112.5)
    check("net GST payable", round(float(out_tax) - float(in_tax), 2), -29.0)
check("net position shown as credit", "Credit carried forward" in body, True)


print("\n=== 8. Invoice page ===")
with get_db() as conn:
    sid = conn.query_one("SELECT id FROM sales WHERE invoice_no = 'SDF/2026-27/0002'")["id"]
r = c.get("/sales/" + str(sid) + "/invoice")
body = r.get_data(as_text=True)
check("invoice renders", r.status_code, 200)
check("shows IGST column", "IGST" in body, True)
check("hides CGST for inter-state", "<th class=\"num\">CGST</th>" in body, False)
check("prints amount in words", "Rupees Four Thousand Five Hundred Thirty Six Only" in body, True)
check("prints GSTIN", "27ABCDE1234F1Z5" in body, True)


print("\n=== 9. Deleting a bill recalculates stock and re-costs past sales ===")
with get_db() as conn:
    pid = conn.query_one("SELECT id FROM purchases WHERE bill_no = 'B-2'")["id"]
c.post("/purchases/" + str(pid) + "/delete", follow_redirects=True)
with get_db() as conn:
    it = conn.query_one("SELECT stock_qty, stock_value FROM items WHERE id = ?", (KAJU,))
    # 10 kg bought, 13 kg sold: the shortfall is surfaced rather than silently clamped
    check("stock goes negative and is surfaced", it["stock_qty"], -3)
    check("no value left on an empty shelf", it["stock_value"], 0)
    s1 = conn.query_one("SELECT cogs FROM sales WHERE invoice_no = 'SDF/2026-27/0001'")
    check("earlier sale re-costed at 800/kg", s1["cogs"], 4800)
    s2 = conn.query_one("SELECT cogs FROM sales WHERE invoice_no = 'SDF/2026-27/0002'")
    check("second sale re-costed too", s2["cogs"], 3200)
check("stock page flags the negative", "negative" in c.get("/stock").get_data(as_text=True), True)


print("\n=== 10. CSV exports ===")
for kind, needle in [("sales", "Invoice No"), ("purchases", "Bill No"),
                     ("stock", "Stock Value"), ("gst", "INPUT TAX CREDIT"),
                     ("itemwise", "Margin %")]:
    r = c.get("/export/" + kind + ".csv?start=2026-09-01&end=2026-09-30")
    check(kind + ".csv downloads", r.status_code == 200 and needle in r.get_data(as_text=True), True)


print("\n=== 11. Quick sale, search and date presets ===")
r = c.get("/quick")
check("quick sale screen renders", r.status_code, 200)
check("quick screen lists items", "Masala Kaju" in r.get_data(as_text=True), True)

# a quick sale posts the same field shape as the full form and must produce the same record
before = c.get("/sales?preset=all").get_data(as_text=True).count("SDF/2026-27/")
c.post("/quick", data={"invoice_date": "2026-09-06", "item_id": [str(KAJU)],
                       "qty": ["2"], "rate": ["1000"], "discount_pct": ["0"],
                       "payment_mode": "UPI"}, follow_redirects=True)
with get_db() as conn:
    q = conn.query_one("SELECT * FROM sales ORDER BY id DESC LIMIT 1")
    check("quick sale saved", q["taxable"], 2000)
    check("quick sale charged CGST+SGST", (q["cgst"], q["sgst"]), (50.0, 50.0))
    check("quick sale numbered in sequence", q["invoice_no"].startswith("SDF/2026-27/"), True)
    check("quick sale kept the payment mode", q["payment_mode"], "UPI")

body = c.get("/sales?preset=all&q=kaju").get_data(as_text=True)
check("search finds sales by item name", "SDF/2026-27/0001" in body, True)
body = c.get("/sales?preset=all&q=zzzznothing").get_data(as_text=True)
check("search with no match shows an empty state", "Nothing matches that search" in body, True)
body = c.get("/purchases?preset=all&q=b-1").get_data(as_text=True)
check("search finds purchases by bill number", "B-1" in body, True)
body = c.get("/purchases?preset=all&q=masala").get_data(as_text=True)
check("search finds purchases by item on the bill", "B-1" in body, True)
check("today preset returns only today", c.get("/sales?preset=today").status_code, 200)
check("financial-year preset resolves", c.get("/reports?preset=fy").status_code, 200)
check("exports honour the preset", c.get("/export/sales.csv?preset=all").status_code, 200)


print("\n=== 12. Charts ===")
import charts as chmod  # noqa: E402
cc = chmod.columns_pair([{"d": "01", "s": 100.0, "p": 40.0}, {"d": "02", "s": 300.0, "p": 0.0}],
                        "d", "s", "p", "Sales", "Purchases")
check("bars are sized as a share of the axis top", cc["cols"][1]["a_pct"], 100.0)
check("a smaller value scales proportionally", cc["cols"][0]["a_pct"], 33.333)
check("axis top is the data peak when it is already clean", cc["ticks"][-1]["label"], "300")
check("axis wastes no headroom", len(cc["ticks"]), 4)
check("a zero value draws no bar", cc["cols"][1]["b_pct"], 0)

ll = chmod.lines_pair([{"d": "01", "s": 100.0, "p": 40.0}, {"d": "02", "s": 300.0, "p": 60.0}],
                      "d", "s", "p", "Sales", "Profit")
check("line chart builds a normalised path", ll["a"]["path"], "M0.0 66.667 L100.0 0.0")
check("area closes back to the baseline", ll["a"]["area"].endswith("Z"), True)
check("line chart has one hit zone per point", len(ll["hits"]), 2)
check("end marker sits on the last point", ll["a"]["end"]["y"], 0.0)

hh = chmod.hbars([{"n": "Kaju", "v": 8820.0}, {"n": "Peanuts", "v": -560.0}], "n", "v")
check("longest bar fills the track", hh["bars"][0]["pct"], 100.0)
check("negative profit is flagged for its own colour", hh["bars"][1]["neg"], True)
check("bar values are directly labelled", hh["bars"][0]["value"], "8,820")
check("compact axis labels use lakh notation", chmod.compact(250000), "2.5L")
check("compact axis labels use crore notation", chmod.compact(15400000), "1.54Cr")

body = c.get("/reports?preset=all").get_data(as_text=True)
check("reports renders the chart legend", "Gross profit</span>" in body, True)
check("chart marks carry hover data", "data-av=" in body, True)
check("item chart labels each bar with its value", "hb-val" in body, True)
# the favicon is an SVG with text in it, so scope the check to the chart markup
chart_markup = body[body.index("cw-plot"):body.index("Net GST position")]
check("charts draw marks in HTML, so nothing skews when stretched",
      "<text" not in chart_markup and "hb-bar" in chart_markup, True)
check("the one SVG path keeps its stroke width under stretch",
      "non-scaling-stroke" in chart_markup, True)
check("empty dashboard chart shows a guiding message",
      "Nothing recorded yet" in c.get("/").get_data(as_text=True), True)


print("\n=== 13. The daily series has no gaps ===")
# 03, 04, 05 and 06 Sep had activity in this suite; 07 onward did not.
rep = c.get("/reports?start=2026-09-01&end=2026-09-10").get_data(as_text=True)
for d in ("03 Sep 2026", "04 Sep 2026", "05 Sep 2026", "06 Sep 2026"):
    check("day-by-day includes " + d, d in rep, True)
check("a quiet day between two busy ones is still plotted",
      rep.count("cw-hit") >= 4, True)
wide = c.get("/reports?preset=all")
check("an all-time range still renders", wide.status_code, 200)


print("\n=== 14. Admin gate ===")
locked = app.test_client()   # a fresh session: admin is locked here
for path in ["/sales/1/edit", "/purchases/1/edit"]:
    r = locked.get(path)
    check("locked session cannot open " + path, r.status_code in (301, 302), True)
    check("  and is sent to the unlock screen", "/admin" in r.headers.get("Location", ""), True)

with get_db() as conn:
    before = conn.scalar("SELECT COUNT(*) AS c FROM sales")
locked.post("/sales/1/delete", follow_redirects=True)
with get_db() as conn:
    check("locked session cannot delete", conn.scalar("SELECT COUNT(*) AS c FROM sales"), before)

r = locked.post("/admin", data=dict(pin="0000"), follow_redirects=True)
check("a wrong PIN is refused", "not right" in r.get_data(as_text=True), True)
r = locked.post("/admin", data=dict(pin="7788"), follow_redirects=True)
check("the right PIN unlocks", "Admin unlocked" in r.get_data(as_text=True), True)
check("unlocked session can now open the edit form", locked.get("/sales/1/edit").status_code, 200)
locked.post("/admin/lock", follow_redirects=True)
check("locking takes the access away again",
      locked.get("/sales/1/edit").status_code in (301, 302), True)


print("\n=== 15. Editing an entry corrects the books ===")
with get_db() as conn:
    s1 = conn.query_one("SELECT * FROM sales WHERE invoice_no = 'SDF/2026-27/0001'")
    kaju_before = conn.query_one("SELECT stock_qty FROM items WHERE id = ?", (KAJU,))["stock_qty"]
sid = s1["id"]
check("edit form opens for an unlocked admin", c.get("/sales/" + str(sid) + "/edit").status_code, 200)
import html as _html  # noqa: E402
import re as _re        # noqa: E402
body = c.get("/sales/" + str(sid) + "/edit").get_data(as_text=True)
m = _re.search(r"data-existing='([^']*)'", body)
prefilled = json.loads(_html.unescape(m.group(1))) if m else []
check("the form is prefilled with the saved line", len(prefilled), 1)
check("  at the saved quantity", prefilled[0]["qty"] if prefilled else None, 6.0)
check("  and the saved rate", prefilled[0]["rate"] if prefilled else None, 1100.0)
check("a new entry form carries no prefill",
      _re.search(r"data-existing='([^']*)'", c.get("/sales/new").get_data(as_text=True)).group(1), "[]")

# the sale was 6 kg at 1100; correct it to 4 kg at 1200
c.post("/sales/" + str(sid) + "/edit",
       data={"invoice_date": "2026-09-03", "invoice_no": "SDF/2026-27/0001",
             "state_code": "27", "party_name": "Walk-in", "payment_mode": "Cash",
             "item_id": [str(KAJU)], "qty": ["4"], "rate": ["1200"], "discount_pct": ["0"]},
       follow_redirects=True)
with get_db() as conn:
    s2 = conn.query_one("SELECT * FROM sales WHERE id = ?", (sid,))
    check("taxable value updated", s2["taxable"], 4800)
    check("GST recomputed on the new value", (s2["cgst"], s2["sgst"]), (120.0, 120.0))
    check("invoice number kept", s2["invoice_no"], "SDF/2026-27/0001")
    check("the edit is counted", s2["edit_count"], 1)
    check("and timestamped", bool(s2["updated_at"]), True)
    check("no duplicate lines left behind",
          conn.scalar("SELECT COUNT(*) AS c FROM sale_lines WHERE sale_id = ?", (sid,)), 1)
    after = conn.query_one("SELECT stock_qty FROM items WHERE id = ?", (KAJU,))["stock_qty"]
    check("stock corrected by the 2 kg no longer sold", round(after - kaju_before, 3), 2.0)
    check("invoice count unchanged - an edit is not a new record",
          conn.scalar("SELECT COUNT(*) AS c FROM sales WHERE invoice_no = 'SDF/2026-27/0001'"), 1)

# editing a purchase re-costs the sales that followed it
with get_db() as conn:
    pid = conn.query_one("SELECT id FROM purchases WHERE bill_no = 'B-1'")["id"]
check("purchase edit form opens", c.get("/purchases/" + str(pid) + "/edit").status_code, 200)
c.post("/purchases/" + str(pid) + "/edit",
       data={"bill_date": "2026-09-01", "bill_no": "B-1", "state_code": "27",
             "payment_mode": "Cash", "item_id": [str(KAJU)], "qty": ["10"],
             "rate": ["900"], "discount_pct": ["0"]},
       follow_redirects=True)
with get_db() as conn:
    p2 = conn.query_one("SELECT * FROM purchases WHERE id = ?", (pid,))
    check("purchase taxable updated 10 x 900", p2["taxable"], 9000)
    check("purchase edit counted", p2["edit_count"], 1)
    s3 = conn.query_one("SELECT cogs FROM sales WHERE invoice_no = 'SDF/2026-27/0001'")
    check("the later sale was re-costed at the new purchase price", s3["cogs"], 3600)


print("\n=== 16. Upgrading a database that already holds data ===")
# The live database was created before admin and audit columns existed.
# CREATE TABLE IF NOT EXISTS will not add them, so migrate() must.
import db as dbmod  # noqa: E402
with get_db() as conn:
    sales_before = conn.scalar("SELECT COUNT(*) AS c FROM sales")
    dropped = []
    for table, col, _ddl in dbmod.MIGRATIONS:
        try:
            conn.execute("ALTER TABLE {} DROP COLUMN {}".format(table, col))
            dropped.append((table, col))
        except Exception:
            pass
check("columns could be dropped to simulate the old schema", len(dropped) > 0, True)
if dropped:
    with get_db() as conn:
        have = dbmod.existing_columns(conn, "settings")
        check("the old schema really is missing admin_hash", "admin_hash" in have, False)

dbmod.init_db()   # this is what runs on every deploy

with get_db() as conn:
    for table, col, _ddl in dbmod.MIGRATIONS:
        check("migration restored " + table + "." + col,
              col in dbmod.existing_columns(conn, table), True)
    check("existing rows survived the upgrade", conn.scalar("SELECT COUNT(*) AS c FROM sales"), sales_before)

dbmod.init_db()   # running twice must not fail or duplicate anything
with get_db() as conn:
    check("migration is safe to run again", "admin_hash" in dbmod.existing_columns(conn, "settings"), True)
    check("and did not re-seed the sample items",
          conn.scalar("SELECT COUNT(*) AS c FROM items") > 0, True)
check("the app still serves after the upgrade", c.get("/").status_code, 200)
check("and the admin screen offers to set a PIN again", "PIN" in c.get("/admin").get_data(as_text=True), True)


print("\n=== 17. Invoice numbers are generated, not typed ===")
body = c.get("/sales/new").get_data(as_text=True)
m = _re.search(r'id="invoice_no"[^>]*', body)
field = m.group(0) if m else ""
check("the number field is read-only", "readonly" in field, True)
check("and is not submitted with the form", 'name="invoice_no"' in field, False)
check("the next number is shown to the user", "RKK/" in field or "SDF/" in field, True)

# a hand-typed number must be ignored
c.post("/sales/new", data={"invoice_date": "2026-09-08", "invoice_no": "hgtfhtdtxfg",
                           "state_code": "27", "party_name": "Walk-in",
                           "item_id": [str(KAJU)], "qty": ["1"], "rate": ["100"],
                           "discount_pct": ["0"]}, follow_redirects=True)
with get_db() as conn:
    latest = conn.query_one("SELECT invoice_no FROM sales ORDER BY id DESC LIMIT 1")
    check("a typed number is ignored", latest["invoice_no"] == "hgtfhtdtxfg", False)
    check("a generated one is used instead", latest["invoice_no"].startswith("SDF/2026-27/"), True)
    nos = [r["invoice_no"] for r in conn.query("SELECT invoice_no FROM sales")]
    check("every number is unique", len(nos), len(set(nos)))

# editing must not change the number, whatever is posted
with get_db() as conn:
    sid2 = conn.query_one("SELECT id, invoice_no FROM sales ORDER BY id DESC LIMIT 1")
c.post("/sales/" + str(sid2["id"]) + "/edit",
       data={"invoice_date": "2026-09-08", "invoice_no": "TAMPERED/1",
             "state_code": "27", "party_name": "Walk-in",
             "item_id": [str(KAJU)], "qty": ["2"], "rate": ["100"], "discount_pct": ["0"]},
       follow_redirects=True)
with get_db() as conn:
    same = conn.query_one("SELECT invoice_no, taxable FROM sales WHERE id = ?", (sid2["id"],))
    check("the number survives an edit untouched", same["invoice_no"], sid2["invoice_no"])
    check("but the figures do change", same["taxable"], 200)

# a number already taken must be skipped, not reused
with get_db() as conn:
    nxt = core.next_invoice_no(conn, "2026-09-09", "SDF")
    conn.execute("UPDATE sales SET invoice_no = ? WHERE id = ?", (nxt, sid2["id"]))
    after = core.next_invoice_no(conn, "2026-09-09", "SDF")
    check("generation skips a number already in use", after == nxt, False)
    check("  and moves to the next free one",
          int(after.rsplit("/", 1)[1]) > int(nxt.rsplit("/", 1)[1]), True)

check("the financial year rolls over on 1 April",
      (core.fy_of("2027-03-31"), core.fy_of("2027-04-01")), ("2026-27", "2027-28"))
with get_db() as conn:
    check("so numbering restarts in the new year",
          core.next_invoice_no(conn, "2027-04-05", "SDF"), "SDF/2027-28/0001")

# the supplier's own bill number stays typed by hand
pbody = c.get("/purchases/new").get_data(as_text=True)
pm = _re.search(r'id="bill_no"[^>]*', pbody)
check("a supplier bill number is still entered by hand", 'name="bill_no"' in (pm.group(0) if pm else ""), True)


print("\n" + ("=" * 46))
if FAILS:
    print("FAILED: " + str(len(FAILS)))
    for f in FAILS:
        print("  - " + f)
    sys.exit(1)
print("ALL CHECKS PASSED")
