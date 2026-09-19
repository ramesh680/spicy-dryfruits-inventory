"""End-to-end checks: GST splits, weighted-average costing, invoice numbering, reports.

Run with:  python tests/test_app.py
"""
import os
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)
os.environ.pop("DATABASE_URL", None)
os.environ["SQLITE_PATH"] = os.path.join(tempfile.mkdtemp(), "test.db")

from app import app                                   # noqa: E402
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
with get_db() as conn:
    check("home state saved", conn.query_one("SELECT state_code FROM settings WHERE id=1")["state_code"], "27")
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


print("\n" + ("=" * 46))
if FAILS:
    print("FAILED: " + str(len(FAILS)))
    for f in FAILS:
        print("  - " + f)
    sys.exit(1)
print("ALL CHECKS PASSED")
