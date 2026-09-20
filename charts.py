"""Chart geometry.

Everything is returned as percentages so the templates can draw the marks in
plain HTML/CSS. That keeps bar widths, corner radii and label type crisp at
every screen width - an SVG stretched to fit its container skews all three.
Only the line chart uses SVG, for the path, and it carries no text.
"""
import math


def inr(v):
    """Indian digit grouping, whole rupees."""
    try:
        n = int(round(float(v or 0)))
    except (TypeError, ValueError):
        return "0"
    neg, s = n < 0, str(abs(n))
    if len(s) > 3:
        head, tail = s[:-3], s[-3:]
        parts = []
        while len(head) > 2:
            parts.insert(0, head[-2:])
            head = head[:-2]
        if head:
            parts.insert(0, head)
        s = ",".join(parts) + "," + tail
    return ("-" if neg else "") + s


def compact(v):
    """Short axis labels: 1.2L, 45k, 800."""
    try:
        n = float(v or 0)
    except (TypeError, ValueError):
        return "0"
    a, sign = abs(n), ("-" if n < 0 else "")
    if a >= 10000000:
        return "%s%.4gCr" % (sign, a / 10000000.0)
    if a >= 100000:
        return "%s%.4gL" % (sign, a / 100000.0)
    if a >= 1000:
        return "%s%.4gk" % (sign, a / 1000.0)
    return "%s%d" % (sign, int(round(a)))


def nice_max(v, steps=4):
    """Round the axis top up to a clean number.

    Prefers the least wasted headroom above the data, then a tick count near
    four - so a peak of 300 gives 0/100/200/300 rather than a 400 ceiling.
    """
    v = float(v or 0)
    if v <= 0:
        return 1.0, [0.0]
    mag = 10 ** math.floor(math.log10(v / steps))
    best = None
    for m in (1, 2, 2.5, 5, 10, 20, 25):
        step = mag * m
        n = int(math.ceil(v / step - 1e-9))
        if 2 <= n <= 6:
            rank = (round(step * n - v, 6), abs(n - steps))
            if best is None or rank < best[0]:
                best = (rank, step, n)
    if best is None:
        step = mag * 10
        n = max(1, int(math.ceil(v / step - 1e-9)))
    else:
        _, step, n = best
    return step * n, [step * i for i in range(n + 1)]


def _pct(v, top):
    return round(max(float(v), 0) / top * 100.0, 3)


# ------------------------------------------------------- grouped columns

def columns_pair(rows, label_key, a_key, b_key, a_name, b_name):
    """Two bars per slot, drawn as HTML so the 4px cap stays 4px."""
    vals = [float(r[a_key]) for r in rows] + [float(r[b_key]) for r in rows]
    peak = max(vals + [0])
    top, ticks = nice_max(peak)
    n = max(len(rows), 1)
    cols = []
    for i, r in enumerate(rows):
        av, bv = float(r[a_key]), float(r[b_key])
        cols.append(dict(
            label=r[label_key], a_v=av, b_v=bv,
            a_pct=_pct(av, top), b_pct=_pct(bv, top),
            # on a crowded axis only every other label is drawn
            show_label=(n <= 8 or i % 2 == 0),
        ))
    return dict(
        cols=cols, a_name=a_name, b_name=b_name, empty=peak <= 0,
        ticks=[dict(pct=_pct(t, top), label=compact(t)) for t in ticks],
    )


# ------------------------------------------------------------------ lines

def lines_pair(rows, label_key, a_key, b_key, a_name, b_name):
    """Two series on one axis (same unit), as a normalised SVG path."""
    n = len(rows)
    vals = [float(r[a_key]) for r in rows] + [float(r[b_key]) for r in rows]
    peak = max(vals + [0])
    low = min(vals + [0])
    top, ticks = nice_max(peak)
    base = min(low, 0)
    span = (top - base) or 1.0
    step = 100.0 / max(n - 1, 1)

    def xy(i, v):
        return round(step * i, 3), round(100.0 - (float(v) - base) / span * 100.0, 3)

    def series(key):
        pts = [dict(zip(("x", "y"), xy(i, r[key]))) for i, r in enumerate(rows)]
        for i, p in enumerate(pts):
            p["v"] = float(rows[i][key])
        if not pts:
            return dict(pts=[], path="", area="", end=None)
        path = "M" + " L".join("%s %s" % (p["x"], p["y"]) for p in pts)
        zero = round(100.0 - (0 - base) / span * 100.0, 3)
        area = path + " L%s %s L%s %s Z" % (pts[-1]["x"], zero, pts[0]["x"], zero)
        return dict(pts=pts, path=path, area=area, end=pts[-1])

    a, b = series(a_key), series(b_key)
    hits = [dict(label=rows[i][label_key], a=float(rows[i][a_key]), b=float(rows[i][b_key]),
                 x=round(step * i, 3),
                 show_label=(n <= 8 or i % max(1, n // 7) == 0))
            for i in range(n)]
    return dict(
        a=a, b=b, a_name=a_name, b_name=b_name, hits=hits,
        empty=n == 0 or peak <= 0,
        ticks=[dict(pct=_pct(t - base, span), label=compact(t)) for t in ticks],
    )


# ------------------------------------------------------- horizontal bars

def hbars(rows, label_key, value_key):
    """Ranked magnitude, one series - so no legend, and every bar is labelled."""
    rows = list(rows)
    peak = max([abs(float(r[value_key])) for r in rows] + [0]) or 1.0
    bars = [dict(label=r[label_key], v=float(r[value_key]), value=inr(r[value_key]),
                 pct=round(abs(float(r[value_key])) / peak * 100.0, 3),
                 neg=float(r[value_key]) < 0)
            for r in rows]
    return dict(bars=bars, empty=not rows)
