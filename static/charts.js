/* Hover / focus layer for the charts.
   Labels come from the database, so every insertion uses textContent. */
(function () {
  var tip = document.getElementById('tip');
  if (!tip) return;

  function money(v) {
    var n = Math.round(Number(v) || 0), neg = n < 0, s = String(Math.abs(n));
    if (s.length > 3) {
      var tail = s.slice(-3), head = s.slice(0, -3), out = [];
      while (head.length > 2) { out.unshift(head.slice(-2)); head = head.slice(0, -2); }
      if (head) out.unshift(head);
      s = out.join(',') + ',' + tail;
    }
    return (neg ? '-₹' : '₹') + s;
  }

  function row(name, value, color) {
    var r = document.createElement('div'); r.className = 't-row';
    var k = document.createElement('span'); k.className = 't-key'; k.style.background = color;
    var n = document.createElement('span'); n.className = 't-name'; n.textContent = name;
    var v = document.createElement('span'); v.className = 't-val'; v.textContent = money(value);
    r.appendChild(k); r.appendChild(n); r.appendChild(v);
    return r;
  }

  function place(x, y) {
    var w = tip.offsetWidth, h = tip.offsetHeight;
    tip.style.left = Math.min(Math.max(8, x - w / 2), window.innerWidth - w - 8) + 'px';
    var top = y - h - 14;
    tip.style.top = (top < 8 ? y + 20 : top) + 'px';
  }

  function show(el, x, y) {
    var d = el.dataset;
    tip.textContent = '';
    var head = document.createElement('div');
    head.className = 't-date'; head.textContent = d.label || '';
    tip.appendChild(head);
    if (d.a) tip.appendChild(row(d.a, d.av, 'var(--s-sales)'));
    if (d.b) tip.appendChild(row(d.b, d.bv, 'var(--s-purchases)'));
    tip.classList.add('on');
    place(x, y);
  }

  function hide() {
    tip.classList.remove('on');
    document.querySelectorAll('.xhair').forEach(function (x) { x.hidden = true; });
  }

  function crosshair(el) {
    var plot = el.closest('.cw-plot');
    if (!plot) return;
    var xh = plot.querySelector('.xhair');
    if (!xh || el.dataset.cx == null) return;
    xh.style.left = el.dataset.cx + '%';
    xh.hidden = false;
  }

  function bind(el) {
    function at(e) {
      var r = el.getBoundingClientRect();
      var x = (e && e.clientX != null) ? e.clientX : r.left + r.width / 2;
      var y = (e && e.clientY != null) ? e.clientY : r.top;
      show(el, x, y);
      crosshair(el);
    }
    el.addEventListener('pointermove', at);
    el.addEventListener('pointerenter', at);
    el.addEventListener('focus', function () { at(null); });
    el.addEventListener('pointerleave', hide);
    el.addEventListener('blur', hide);
  }

  document.querySelectorAll('.cw-col, .cw-hit, .hb-row').forEach(bind);
  window.addEventListener('scroll', hide, { passive: true });
})();
