# Spicy Dry Fruits — daily inventory & GST tracker

A small web app for running the day-to-day books of a spicy dry fruits business:
record what you buy, record what you sell, and let it work out stock, cost, profit
and the GST position for you.

Built with Flask. Runs on SQLite locally and on PostgreSQL in production.

## What it does

**Daily buy and sell**
Record a purchase bill or a sale in one screen. Pick the item, type the quantity
and rate, and the tax, line totals and bill total update as you type. Each item
carries its own GST rate and HSN code, so you never enter tax by hand.

**Quick sale**
A counter screen for the common walk-in: tap an item tile, type the weight, save.
The rate comes from the item, stock on hand is shown on every tile, and the
running total sits pinned at the bottom of the screen. It writes exactly the same
record as the full invoice form, so nothing is second-class about a quick sale.

**GST, done properly**
- Intra-state supply splits into **CGST + SGST** (half the rate each).
- Inter-state supply is charged as **IGST** at the full rate.
- Which one applies is decided by the customer's or supplier's state against your
  home state — set once in Settings, then it is automatic.
- Bill totals round to the nearest rupee with the difference shown as *round off*,
  the way a printed invoice does.
- GST can be switched off entirely in Settings if you are not registered yet.

**Live stock and valuation**
Stock moves the moment a bill is saved. Cost is tracked at **weighted-average
cost**, so profit on every sale reflects what that stock actually cost you. Every
change replays the whole ledger, so deleting or correcting an old bill re-costs
the sales that came after it rather than leaving the valuation drifting. Items
below their reorder level are flagged on the dashboard and the stock page; stock
that has gone negative is flagged too, instead of being quietly clamped to zero.

**Profit and GST reports**
Pick any date range and see sales, cost of goods sold, gross profit and margin;
output tax and input tax credit broken up by rate the way a GSTR summary wants
it; the net GST payable; profit by item; and a day-by-day table. Everything
exports to CSV for your accountant — sales register, purchase register, stock,
GST summary and item-wise profit.

**Searchable registers**
Both registers take a search term - invoice or bill number, customer or supplier
name, payment mode, or any item on the document - alongside date presets for
today, this week, this month, this financial year, or all time. The CSV exports
follow whatever range you are looking at.

**Charts**
The dashboard plots the last fourteen days of sales against purchases; reports
plot sales against gross profit day by day, and rank profit by item. Marks are
drawn in HTML rather than stretched SVG, so bar caps and label type stay crisp at
any width, and every chart has a hover and keyboard-focus readout.

**Correcting mistakes, behind an admin PIN**
A wrong entry or a test record can be edited or deleted. Both are gated by an
admin PIN so a slip at the counter cannot quietly rewrite the books: recording
new sales and purchases never asks for it, changing or removing one always does.
Editing opens the original bill with its lines intact; saving replays the whole
ledger, so stock, weighted-average cost and the cost of every later sale are all
recomputed. Each edit is counted and timestamped on the record.

Set the PIN the first time you open **Unlock admin** in the sidebar. It is stored
as a salted PBKDF2 hash - the database never holds the PIN itself - and can be
changed from Settings while unlocked. Locking is one click, for when you hand the
device to someone else.

**Printable GST invoices**
Every sale gets a numbered tax invoice with your GSTIN and address, the
customer's details, HSN codes, the per-rate tax breakup, amount in words, bank
details and a signature block. Print it, or save it as a PDF from the print
dialog. Invoice numbers run per financial year — `SDF/2026-27/0001` — and restart
each April.

## Running it locally

```bash
git clone https://github.com/ramesh680/spicy-dryfruits-inventory.git
cd spicy-dryfruits-inventory
pip install -r requirements.txt
python app.py
```

Open http://localhost:5000. A SQLite database is created at `data/inventory.db`
on first run, seeded with eight sample items you can rename, edit or archive.

The interface is dark by default with a light theme one click away (the toggle
sits at the foot of the sidebar); the choice is remembered per browser. On a
phone the sidebar collapses to a top bar with a menu, and the five screens you
reach for at the counter move to a bottom tab bar.

Run the checks with:

```bash
python tests/test_app.py
```

They cover the GST splits, discounts, rounding, weighted-average costing,
invoice numbering, the reports and every CSV export — against hand-calculated
figures. The same suite runs on PostgreSQL by setting `DATABASE_URL` first.

## Deploying to Render

`render.yaml` is a blueprint: it provisions the web service **and** a free
PostgreSQL database, and wires `DATABASE_URL` between them.

1. Push this repo to GitHub.
2. In Render, choose **New → Blueprint** and point it at the repo.
3. Render reads `render.yaml`, creates both services and deploys.

**Use PostgreSQL, not SQLite, in production.** A Render web service has an
ephemeral filesystem — a SQLite file would be wiped on every redeploy and every
restart, taking your books with it. The blueprint handles this for you. Free
Postgres instances on Render expire after a set period, so move to a paid
instance (or any other Postgres, such as Neon or Supabase) before you rely on it
for real trading data.

### Environment variables

| Variable | Purpose | Default |
| --- | --- | --- |
| `DATABASE_URL` | PostgreSQL URL. Unset means SQLite. | unset |
| `SQLITE_PATH` | Where the SQLite file lives. | `data/inventory.db` |
| `SECRET_KEY` | Signs the session cookie that carries admin unlock. Set a real one in production - the blueprint generates it. | `dev-secret-change-me` |
| `PORT` | Port to bind. | `5000` |

## First-time setup

1. **Settings** — business name, address, GSTIN, home state, invoice prefix,
   bank details and terms. The home state is what decides CGST+SGST vs IGST, so
   get it right first.
2. **Items** — your products, each with its HSN code, GST rate, unit, default
   sale rate and reorder level. Enter opening stock and its cost per unit if you
   already have stock on hand.
3. **Contacts** — suppliers and customers with their state and GSTIN.
4. Start recording. **+ Buy** and **+ Sell** are in the top nav.

## A note on GST rates

The seeded items use 5%, the slab plain and processed dry fruits and nuts fall
under following the September 2025 rate revision. Rates and HSN classifications
change, and roasted, salted or spiced preparations can sit in a different heading
from plain nuts — confirm the right rate and HSN for your exact products with
your accountant, then set them per item on the Items page.

The GST figures here are a working view for planning cash and preparing returns.
They do not apply reverse charge, blocked or ineligible input tax credit, or
e-invoicing and e-way bill rules. This is a bookkeeping tool, not a filing tool.

## How it is put together

```
app.py              routes, CSV exports, search and date presets, template filters
core.py             GST maths, weighted-average costing, invoice numbering,
                    amount-in-words, state codes, admin PIN hashing
charts.py           chart geometry as percentages, axis ticks, compact formatting
db.py               schema, migrations, and a thin layer that speaks both
                    SQLite and Postgres
templates/          Jinja templates; _icons, _charts and _filters hold the macros
static/             design tokens and stylesheet, entry-form and chart scripts
tests/test_app.py   end-to-end checks
render.yaml         Render blueprint: web service + Postgres
```
