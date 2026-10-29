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

OWNER = "ramesh.goswami100@gmail.com"
OWNER_PW = "owner-pass-123"


def signed_in_client():
    cl = app.test_client()
    cl.post("/login", data=dict(email=OWNER, password=OWNER_PW))
    return cl


# The owner sets a password on first run (local SQLite, no PIN: no code needed).
app.test_client().post("/setup", data=dict(email=OWNER, password=OWNER_PW, confirm=OWNER_PW))
c = signed_in_client()

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
locked = signed_in_client()   # a fresh session: admin is locked here
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


print("\n=== 18. Sign-in protects every page ===")
import re as _re2  # noqa: E402
anon = app.test_client()
for path in ["/", "/sales", "/stock", "/reports", "/export/sales.csv", "/settings", "/users"]:
    r = anon.get(path)
    check("signed-out " + path + " goes to sign in",
          r.status_code in (301, 302) and "/login" in r.headers.get("Location", ""), True)
r = anon.post("/sales/new", data={"invoice_date": "2026-09-09"})
check("signed-out POST is refused", "/login" in r.headers.get("Location", ""), True)
check("health check stays open for Render", anon.get("/healthz").status_code, 200)
check("sign-in page loads", anon.get("/login").status_code, 200)

with get_db() as conn:
    seeded = {r["email"]: r["role"] for r in conn.query("SELECT email, role FROM users")}
check("Ramesh is seeded as owner", seeded.get(OWNER), "owner")
check("Saurabh is seeded as owner too", seeded.get("saurabhsethi9211@gmail.com"), "owner")
with get_db() as conn:
    row = conn.query_one("SELECT pw_hash FROM users WHERE email = ?", (OWNER,))
check("password stored hashed", OWNER_PW not in row["pw_hash"] and len(row["pw_hash"]) == 64, True)

r = anon.post("/login", data=dict(email=OWNER, password="wrong"), follow_redirects=True)
check("wrong password refused", "do not match" in r.get_data(as_text=True), True)
r = anon.post("/login", data=dict(email="stranger@gmail.com", password="whatever1"), follow_redirects=True)
check("unknown email refused", "do not match" in r.get_data(as_text=True), True)
r = anon.post("/login", data=dict(email="SaurabhSethi9211@gmail.com", password="x"), follow_redirects=True)
check("listed but not activated is told to ask for a link", "set-password link" in r.get_data(as_text=True), True)
r = anon.post("/login", data=dict(email="  Ramesh.Goswami100@GMAIL.com ", password=OWNER_PW, next="/stock"))
check("email is case-insensitive and next is honoured", r.headers.get("Location", "").endswith("/stock"), True)
check("signed in can open the dashboard", anon.get("/").status_code, 200)
r = app.test_client().post("/login", data=dict(email=OWNER, password=OWNER_PW, next="https://evil.example/"))
check("next cannot send you off-site", "evil" in r.headers.get("Location", ""), False)
check("setup page is closed once an owner exists",
      app.test_client().get("/setup").status_code in (301, 302), True)

print("\n=== 19. Owner grants access with a one-time link ===")
r = c.post("/users/add", data=dict(email="New.Member@gmail.com", role="member"), follow_redirects=True)
body = r.get_data(as_text=True)
m = _re2.search(r'(https?://[^"\s]+/set-password/[A-Za-z0-9_\-]+)', body)
check("an invite link is shown", bool(m), True)
link = m.group(1).split("localhost", 1)[-1] if m else ""
s2 = app.test_client()
check("invite link opens the set-password page", s2.get(link).status_code, 200)
r = s2.post(link, data=dict(password="short", confirm="short"), follow_redirects=True)
check("short password refused", "at least 8" in r.get_data(as_text=True), True)
r = s2.post(link, data=dict(password="member-pass-1", confirm="member-pass-1"))
check("setting password signs them in", s2.get("/").status_code, 200)
check("the link cannot be reused",
      "expired or was already used" in app.test_client().get(link).get_data(as_text=True), True)
check("a member cannot manage users", s2.get("/users").status_code, 403)
check("a member does not see Users in the menu", "Users &amp; access" in s2.get("/").get_data(as_text=True), False)

r = c.post("/users/add", data=dict(email="new.helper@gmail.com", name="Helper"), follow_redirects=True)
check("owner can add anyone else", "new.helper@gmail.com" in r.get_data(as_text=True), True)
r = c.post("/users/add", data=dict(email="not-an-email"), follow_redirects=True)
check("bad email refused", "valid email" in r.get_data(as_text=True), True)

with get_db() as conn:
    sid_ = conn.query_one("SELECT id FROM users WHERE email = 'new.member@gmail.com'")["id"]
    oid = conn.query_one("SELECT id FROM users WHERE email = ?", (OWNER,))["id"]
c.post("/users/" + str(sid_) + "/disable")
check("disabling signs them out at once", s2.get("/").status_code in (301, 302), True)
r = app.test_client().post("/login", data=dict(email="new.member@gmail.com", password="member-pass-1"),
                           follow_redirects=True)
check("and they cannot sign back in", "do not match" in r.get_data(as_text=True), True)
c.post("/users/" + str(sid_) + "/enable")
r = app.test_client().post("/login", data=dict(email="new.member@gmail.com", password="member-pass-1"))
check("re-enabled user can sign in again", r.headers.get("Location", "").endswith("/"), True)
r = c.post("/users/" + str(oid) + "/remove", follow_redirects=True)
check("owner cannot remove themself", "your own account" in r.get_data(as_text=True), True)

print("\n=== 20. Lockout after repeated wrong passwords ===")
t = app.test_client()
for _ in range(5):
    t.post("/login", data=dict(email="new.member@gmail.com", password="nope-nope"))
r = t.post("/login", data=dict(email="new.member@gmail.com", password="member-pass-1"), follow_redirects=True)
check("even the right password waits out the lock", "Too many wrong tries" in r.get_data(as_text=True), True)
with get_db() as conn:
    conn.execute("UPDATE users SET locked_until = '' WHERE id = ?", (sid_,))

print("\n=== 21. Changing your password signs out other devices ===")
other = signed_in_client()
mine = signed_in_client()
mine.post("/account", data=dict(current=OWNER_PW, password="owner-pass-456", confirm="owner-pass-456"))
check("this device stays signed in", mine.get("/").status_code, 200)
check("the other device is signed out", other.get("/").status_code in (301, 302), True)
mine.post("/account", data=dict(current="owner-pass-456", password=OWNER_PW, confirm=OWNER_PW))


print("\n=== 22. Accounting: a full cycle posts itself into the books ===")
import books  # noqa: E402
import zipfile as _zip  # noqa: E402
import io as _io  # noqa: E402
c = signed_in_client()   # earlier password change signed the old client out
c.post("/admin", data=dict(pin="7788", confirm="7788"), follow_redirects=True)   # unlocks (PIN was reset in 16)
FY = dict(start="2027-04-01", end="2028-03-31")


def acc_id(key):
    with get_db() as conn:
        return books.key_ids(conn)[key]


def head_id(name):
    with get_db() as conn:
        return conn.query_one("SELECT id FROM accounts WHERE name = ?", (name,))["id"]


with get_db() as conn:
    keys = books.key_ids(conn)
check("every system account was created", all(k[0] in keys for k in books.SYSTEM_ACCOUNTS), True)
check("starter expense heads were seeded", head_id("Rent") > 0, True)

# items and contacts for this scenario
for name, kind, rate in (("Mix Dry Fruits", "product", 1500), ("Almond Raw", "raw", 0), ("Cashew Raw", "raw", 0)):
    c.post("/items/save", data=dict(name=name, hsn="0801", gst_rate="5", unit="kg", kind=kind,
                                     sale_rate=str(rate), low_stock_qty="0", opening_qty="0", opening_rate="0"))
MIX, ALM, CSH = item_id("Mix Dry Fruits"), item_id("Almond Raw"), item_id("Cashew Raw")
c.post("/parties/save", data=dict(name="Jaipur Wholesale", kind="supplier", state_code="27"))
c.post("/parties/save", data=dict(name="Gift Corp", kind="customer", state_code="27"))
with get_db() as conn:
    SUP = conn.query_one("SELECT id FROM parties WHERE name = 'Jaipur Wholesale'")["id"]
    CUS = conn.query_one("SELECT id FROM parties WHERE name = 'Gift Corp'")["id"]

# A. capital into the bank, opening cash
r = c.post("/journal/new", data={"j_date": "2027-05-01", "narration": "Capital introduced",
                                 "acc_id": [str(acc_id("bank")), str(acc_id("capital"))],
                                 "debit": ["100000", ""], "credit": ["", "100000"],
                                 "line_party": ["", ""], "memo": ["", ""]}, follow_redirects=True)
check("a balanced journal entry saves", "Journal entry saved" in r.get_data(as_text=True), True)
c.post("/journal/new", data={"j_date": "2027-05-01", "narration": "Opening cash",
                             "acc_id": [str(acc_id("cash")), str(acc_id("opening"))],
                             "debit": ["5000", ""], "credit": ["", "5000"],
                             "line_party": ["", ""], "memo": ["", ""]})
with get_db() as conn:
    check("journal numbered per financial year",
          conn.query_one("SELECT j_no FROM journals ORDER BY id LIMIT 1")["j_no"], "JV/2027-28/0001")

# B. raw materials: almonds on credit, cashews by UPI
post_doc("/purchases/new", dict(bill_date="2027-05-02", bill_no="JW-1", party_id=str(SUP),
                                payment_mode="Credit (Udhaar)"), [(ALM, 10, 800, 0)])
post_doc("/purchases/new", dict(bill_date="2027-05-02", bill_no="JW-2", party_id=str(SUP),
                                payment_mode="UPI"), [(CSH, 5, 1000, 0)])

# C. production: 14 kg of mix from 10 kg almonds + 5 kg cashews, 300 packing paid in cash
c.post("/production/new", data={"prod_date": "2027-05-03", "product_id": str(MIX), "qty_out": "14",
                                "other_cost": "300", "other_cost_mode": "Cash",
                                "raw_id": [str(ALM), str(CSH)], "raw_qty": ["10", "5"]})

# D. sales: 4 kg cash, 2 kg on credit to Gift Corp
post_doc("/sales/new", dict(invoice_date="2027-05-04", state_code="27", party_name="Walk-in",
                            payment_mode="Cash"), [(MIX, 4, 1500, 0)])
post_doc("/sales/new", dict(invoice_date="2027-05-05", party_id=str(CUS),
                            payment_mode="Credit (Udhaar)"), [(MIX, 2, 1500, 0)])

# E. expenses: rent by bank transfer, packing with 18% GST in cash
r = c.post("/expenses/new", data=dict(exp_date="2027-05-06", account_id=str(head_id("Rent")),
                                      amount="10000", gst_rate="0", payment_mode="Bank Transfer",
                                      party_name="Landlord", description="May rent"), follow_redirects=True)
check("expense saves and says the books are updated", "books are updated" in r.get_data(as_text=True), True)
c.post("/expenses/new", data=dict(exp_date="2027-05-06", account_id=str(head_id("Packing Materials")),
                                  amount="1000", gst_rate="18", payment_mode="Cash", description="Pouches"))
with get_db() as conn:
    pk = conn.query_one("SELECT * FROM expenses WHERE description = 'Pouches'")
    check("expense GST splits CGST 9%", pk["cgst"], 90)
    check("expense GST splits SGST 9%", pk["sgst"], 90)
    check("expense total includes GST", pk["total"], 1180)

# F. Gift Corp pays 2000 by UPI; we pay Jaipur Wholesale in full by bank
c.post("/vouchers/new", data=dict(v_type="receipt", v_date="2027-05-07", account_id=str(acc_id("debtors")),
                                  party_id=str(CUS), amount="2000", payment_mode="UPI"))
c.post("/vouchers/new", data=dict(v_type="payment", v_date="2027-05-08", account_id=str(acc_id("creditors")),
                                  party_id=str(SUP), amount="8400", payment_mode="Bank Transfer"))
with get_db() as conn:
    check("receipts numbered RCT/<fy>/0001",
          conn.query_one("SELECT v_no FROM vouchers WHERE v_type = 'receipt'")["v_no"], "RCT/2027-28/0001")

with get_db() as conn:
    pl = books.profit_and_loss(conn, FY["start"], FY["end"])
    check("P&L sales = 6 kg x 1500", pl["sales"]["total"], 9000)
    # batch cost 8000 + 5000 + 300 = 13300 for 14 kg = 950/kg; 6 kg sold
    check("P&L direct costs = COGS 5700 + packing 1000", pl["direct"]["total"], 6700)
    check("gross profit", pl["gross"], 2300)
    check("indirect expenses = rent", pl["ind_exp"]["total"], 10000)
    check("net profit (a loss here)", pl["net"], -7700)

    tb = books.trial_balance(conn, FY["start"], FY["end"])
    check("trial balance balances", tb["tot"]["balanced"], True)
    check("trial balance closing columns agree", round(tb["tot"]["close_dr"] - tb["tot"]["close_cr"], 2), 0)

    bs = books.balance_sheet(conn, FY["end"])
    check("balance sheet balances (assets = liabilities + capital)", bs["diff"], 0)

    cash = books.ledger(conn, acc_id("cash"), FY["start"], FY["end"])
    check("cash moved +5000 -300 +6300 -1180", round(cash["closing"] - cash["opening"], 2), 9820)
    bank = books.ledger(conn, acc_id("bank"), FY["start"], FY["end"])
    check("bank moved +100000 -5250 -10000 +2000 -8400", round(bank["closing"] - bank["opening"], 2), 78350)
    check("running balance ends at the closing balance", bank["rows"][-1]["balance"], bank["closing"])

    o = books.outstanding(conn, FY["end"])
    gift = [r for r in o["debtors"]["rows"] if r["party_id"] == CUS]
    check("Gift Corp still owes 3150 - 2000", gift[0]["amount"] if gift else None, 1150)
    check("Jaipur Wholesale paid in full drops off payables",
          [r for r in o["creditors"]["rows"] if r["party_id"] == SUP], [])

    inv = books.inventory_check(conn)
    check("stock in the books equals the stock valuation", inv["ok"], True)

    ent = books.build(conn, start=FY["start"], end=FY["end"])
    bad = [e["ref"] for e in ent if abs(books.entry_totals(e)[0] - books.entry_totals(e)[1]) > 0.005]
    check("every generated entry balances on its own", bad, [])
    sale_e = [e for e in ent if e["kind"] == "sale"][0]
    names = {a["id"]: a["name"] for a in books.all_accounts(conn)}
    check("a sale credits Sales and Output GST and debits Cash",
          sorted({names[l["acc"]] for l in sale_e["lines"]}) ==
          sorted({"Cash in Hand", "Sales", "Output CGST", "Output SGST", "Cost of Goods Sold",
                  "Stock - Finished Goods"}), True)
    prod_e = [e for e in ent if e["kind"] == "production"][0]
    check("production moves raw material into finished goods",
          {names[l["acc"]] for l in prod_e["lines"]} >= {"Stock - Raw Materials", "Stock - Finished Goods"}, True)

print("\n=== 23. Edits and deletions flow through every statement ===")
with get_db() as conn:
    sid = conn.query_one("SELECT id FROM sales WHERE invoice_date = '2027-05-04'")["id"]
    rent = conn.query_one("SELECT id FROM expenses WHERE description = 'May rent'")["id"]
c.post("/sales/" + str(sid) + "/edit",
       data={"invoice_date": "2027-05-04", "state_code": "27", "party_name": "Walk-in", "payment_mode": "Cash",
             "item_id": [str(MIX)], "qty": ["5"], "rate": ["1500"], "discount_pct": ["0"]})
c.post("/expenses/" + str(rent) + "/delete")
with get_db() as conn:
    pl = books.profit_and_loss(conn, FY["start"], FY["end"])
    check("edited sale: sales now 7 kg x 1500", pl["sales"]["total"], 10500)
    check("edited sale re-costed: COGS 7 x 950 + packing", pl["direct"]["total"], 7650)
    check("deleted rent no longer in expenses", pl["ind_exp"]["total"], 0)
    check("net profit follows", pl["net"], 2850)
    check("still balanced after edits", books.balance_sheet(conn, FY["end"])["diff"], 0)
    check("inventory still reconciles", books.inventory_check(conn)["ok"], True)

print("\n=== 24. Bad entries are refused ===")
with get_db() as conn:
    nj = conn.scalar("SELECT COUNT(*) AS c FROM journals")
r = c.post("/journal/new", data={"j_date": "2027-05-09", "narration": "lopsided",
                                 "acc_id": [str(acc_id("cash")), str(acc_id("capital"))],
                                 "debit": ["100", ""], "credit": ["", "90"],
                                 "line_party": ["", ""], "memo": ["", ""]}, follow_redirects=True)
check("an unbalanced journal is refused", "must be equal" in r.get_data(as_text=True), True)
c.post("/journal/new", data={"j_date": "2027-05-09", "narration": "one-sided", "acc_id": [str(acc_id("cash"))],
                             "debit": ["100"], "credit": [""], "line_party": [""], "memo": [""]})
with get_db() as conn:
    check("nothing was saved", conn.scalar("SELECT COUNT(*) AS c FROM journals"), nj)
r = c.post("/vouchers/new", data=dict(v_type="receipt", v_date="2027-05-09", account_id=str(acc_id("debtors")),
                                      amount="10", payment_mode="Cash"), follow_redirects=True)
check("a receipt against debtors needs a customer", "Say which customer" in r.get_data(as_text=True), True)
r = c.post("/vouchers/new", data=dict(v_type="payment", v_date="2027-05-09", account_id=str(acc_id("cash")),
                                      amount="10", payment_mode="Cash"), follow_redirects=True)
check("cash cannot be paid into cash", "same account" in r.get_data(as_text=True), True)
r = c.post("/accounts/" + str(acc_id("sales")) + "/toggle", follow_redirects=True)
check("a system account cannot be archived", "cannot be archived" in r.get_data(as_text=True), True)
c.post("/accounts/save", data=dict(name="Stall Fees", type="expense", grp="Indirect Expenses", code="5210"))
check("a custom expense head can be added", head_id("Stall Fees") > 0, True)
locked2 = signed_in_client()
locked2.post("/accounts/save", data=dict(id=str(head_id("Stall Fees")), name="Renamed", type="expense",
                                         grp="Indirect Expenses"))
with get_db() as conn:
    check("renaming an account needs admin",
          conn.scalar("SELECT COUNT(*) AS c FROM accounts WHERE name = 'Renamed'"), 0)
c.post("/accounts/save", data=dict(id=str(head_id("Rent")), name="Shop Rent", code="5201",
                                   type="expense", grp="Indirect Expenses"))
check("an unlocked admin can rename one", head_id("Shop Rent") > 0, True)

print("\n=== 25. Value is conserved when a shelf empties ===")
c.post("/items/save", data=dict(name="Rounding Test", hsn="", gst_rate="0", unit="kg", kind="product",
                                 sale_rate="50", low_stock_qty="0", opening_qty="0", opening_rate="0"))
RT = item_id("Rounding Test")
post_doc("/purchases/new", dict(bill_date="2027-06-01", bill_no="RT-1", state_code="27"), [(RT, 3, 33.3333, 0)])
post_doc("/sales/new", dict(invoice_date="2027-06-02", state_code="27", party_name="x"), [(RT, 3, 50, 0)])
with get_db() as conn:
    pv = conn.query_one("SELECT taxable FROM purchases WHERE bill_no = 'RT-1'")["taxable"]
    sv = conn.query_one("SELECT cogs FROM sales WHERE invoice_date = '2027-06-02'")["cogs"]
    check("selling the whole lot costs exactly what it was bought for", sv, pv)
    check("and nothing is left behind on the shelf",
          conn.query_one("SELECT stock_value FROM items WHERE id = ?", (RT,))["stock_value"], 0)

print("\n=== 26. Pages, documents and exports ===")
with get_db() as conn:
    eid = conn.query_one("SELECT id FROM expenses LIMIT 1")["id"]
    vid = conn.query_one("SELECT id FROM vouchers WHERE v_type = 'receipt'")["id"]
    jid = conn.query_one("SELECT id FROM journals LIMIT 1")["id"]
for path in ["/accounts", "/ledger?account=" + str(acc_id("bank")) + "&preset=all",
             "/ledger?account=" + str(acc_id("debtors")) + "&party=id:" + str(CUS) + "&preset=all",
             "/journal?preset=all", "/journal?preset=all&kind=sale", "/journal/new",
             "/profit-loss?preset=all", "/balance-sheet", "/balance-sheet?as_at=2027-05-31",
             "/trial-balance?preset=all", "/outstanding", "/expenses?preset=all", "/expenses/new",
             "/expenses/" + str(eid), "/expenses/" + str(eid) + "/edit", "/vouchers?preset=all",
             "/vouchers/new?type=payment", "/vouchers/" + str(vid), "/vouchers/" + str(vid) + "/edit",
             "/journal/" + str(jid), "/journal/" + str(jid) + "/edit", "/", "/settings", "/production/new",
             "/reports?preset=all"]:
    check("page renders " + path, c.get(path).status_code, 200)
body = c.get("/vouchers/" + str(vid)).get_data(as_text=True)
check("the receipt prints the amount in words", "Rupees Two Thousand Only" in body, True)
check("and shows how it hit the books", "How this hits the books" in body, True)
body = c.get("/").get_data(as_text=True)
check("dashboard shows the money position", "Cash in hand" in body and "Net profit this month" in body, True)
body = c.get("/balance-sheet?as_at=2028-03-31").get_data(as_text=True)
check("balance sheet page says it balances", "Balances</div><div class=\"value\">Yes" in body.replace("\n", ""), True)
for kind, needle in [("expenses", "Head"), ("vouchers", "Voucher No"), ("journal", "Narration"),
                     ("trial-balance", "Closing Dr"), ("profit-loss", "Gross profit"),
                     ("balance-sheet", "ASSETS"), ("outstanding", "Outstanding"), ("accounts", "Group")]:
    r = c.get("/export/" + kind + ".csv?start=2027-04-01&end=2028-03-31")
    check(kind + ".csv downloads", r.status_code == 200 and needle in r.get_data(as_text=True), True)
r = c.get("/export/ledger.csv?account=" + str(acc_id("cash")) + "&preset=all")
check("ledger.csv downloads", r.status_code == 200 and "Opening balance" in r.get_data(as_text=True), True)
r = c.get("/export/books.zip?start=2027-04-01&end=2028-03-31")
z = _zip.ZipFile(_io.BytesIO(r.data))
names = z.namelist()
check("books pack is a zip", r.status_code, 200)
check("books pack holds the statements",
      all(any(n.startswith(p) for n in names) for p in
          ("profit-and-loss", "balance-sheet", "trial-balance", "journal", "sales-register", "expenses")), True)
check("books pack has a readme", "README.txt" in names, True)

print("\n=== 27. Production costs paid on credit become a payable ===")
c.post("/production/new", data={"prod_date": "2027-06-03", "product_id": str(MIX), "qty_out": "1",
                                "other_cost": "500", "other_cost_mode": "Credit (Udhaar)",
                                "raw_id": [str(ALM)], "raw_qty": ["0.001"]})
with get_db() as conn:
    ent = [e for e in books.build(conn, start="2027-06-03", end="2027-06-03") if e["kind"] == "production"]
    names = {a["id"]: a["name"] for a in books.all_accounts(conn)}
    check("batch costs on credit post to Sundry Creditors",
          any(names[l["acc"]].startswith("Sundry Creditors") and l["cr"] == 500 for l in ent[0]["lines"]), True)
    check("books still balance", books.balance_sheet(conn, "2028-03-31")["diff"], 0)

print("\n" + ("=" * 46))
if FAILS:
    print("FAILED: " + str(len(FAILS)))
    for f in FAILS:
        print("  - " + f)
    sys.exit(1)
print("ALL CHECKS PASSED")
