/* Repeating invoice lines + live GST totals, shared by the buy and sell forms. */
(function () {
  var root = document.getElementById('entry');
  if (!root) return;

  var ITEMS = JSON.parse(root.dataset.items || '[]');
  var BY_ID = {};
  ITEMS.forEach(function (i) { BY_ID[String(i.id)] = i; });
  var HOME_STATE = root.dataset.homeState;
  var GST_ON = root.dataset.gstEnabled === '1';
  var PARTY_STATES = JSON.parse(root.dataset.partyStates || '{}');
  var USE_SALE_RATE = root.dataset.useSaleRate === '1';

  var body = document.getElementById('lines');
  var tpl = document.getElementById('line-tpl');

  function inr(n) {
    var v = (Math.round((n + Number.EPSILON) * 100) / 100).toFixed(2);
    var p = v.split('.'), s = p[0], neg = s[0] === '-';
    if (neg) s = s.slice(1);
    if (s.length > 3) {
      var tail = s.slice(-3), head = s.slice(0, -3), out = [];
      while (head.length > 2) { out.unshift(head.slice(-2)); head = head.slice(0, -2); }
      if (head) out.unshift(head);
      s = out.join(',') + ',' + tail;
    }
    return (neg ? '-' : '') + s + '.' + p[1];
  }

  function isInterstate() {
    var sel = document.getElementById('party_id');
    var manual = document.getElementById('state_code');
    var code = HOME_STATE;
    if (sel && sel.value && PARTY_STATES[sel.value]) code = PARTY_STATES[sel.value];
    else if (manual) code = manual.value;
    return code !== HOME_STATE;
  }

  function addRow(preselect) {
    var node = tpl.content.cloneNode(true);
    body.appendChild(node);
    var row = body.lastElementChild;
    var sel = row.querySelector('.f-item');
    if (preselect) sel.value = preselect;
    row.querySelector('.f-del').addEventListener('click', function () {
      row.remove();
      if (!body.children.length) addRow();
      recalc();
    });
    sel.addEventListener('change', function () {
      var it = BY_ID[sel.value];
      if (it) {
        var rate = row.querySelector('.f-rate');
        if (!parseFloat(rate.value)) rate.value = USE_SALE_RATE ? (it.sale_rate || '') : '';
        row.querySelector('.f-unit').textContent = it.unit;
        row.querySelector('.f-gst').textContent = GST_ON ? it.gst_rate + '%' : '--';
        var av = row.querySelector('.f-avail');
        if (av) av.textContent = it.stock_qty !== undefined ? ('in stock ' + it.stock_qty + ' ' + it.unit) : '';
      }
      recalc();
    });
    row.querySelectorAll('input').forEach(function (i) { i.addEventListener('input', recalc); });
    return row;
  }

  function recalc() {
    var inter = isInterstate();
    var tax = 0, cg = 0, sg = 0, ig = 0;
    var buckets = {};
    Array.prototype.forEach.call(body.children, function (row) {
      var sel = row.querySelector('.f-item');
      var it = BY_ID[sel.value];
      var q = parseFloat(row.querySelector('.f-qty').value) || 0;
      var r = parseFloat(row.querySelector('.f-rate').value) || 0;
      var d = parseFloat(row.querySelector('.f-disc').value) || 0;
      var t = Math.round((q * r * (1 - d / 100)) * 100) / 100;
      var gr = (it && GST_ON) ? parseFloat(it.gst_rate) : 0;
      var c = 0, s2 = 0, i2 = 0;
      if (gr > 0) {
        if (inter) i2 = Math.round(t * gr) / 100;
        else { c = Math.round(t * gr / 2) / 100; s2 = c; }
      }
      row.querySelector('.f-total').textContent = inr(t + c + s2 + i2);
      row.querySelector('.f-tax').textContent = inr(c + s2 + i2);
      tax += t; cg += c; sg += s2; ig += i2;
      if (gr > 0) { buckets[gr] = (buckets[gr] || 0) + t; }

      var av = row.querySelector('.f-avail');
      if (av && it && it.stock_qty !== undefined) {
        var short = q > parseFloat(it.stock_qty);
        av.textContent = (short ? 'only ' : 'in stock ') + it.stock_qty + ' ' + it.unit;
        av.className = 'f-avail small ' + (short ? 'badge b-red' : 'muted');
      }
    });
    var raw = tax + cg + sg + ig;
    var grand = Math.round(raw);
    document.getElementById('t-taxable').textContent = inr(tax);
    document.getElementById('t-cgst').textContent = inr(cg);
    document.getElementById('t-sgst').textContent = inr(sg);
    document.getElementById('t-igst').textContent = inr(ig);
    document.getElementById('t-round').textContent = inr(grand - raw);
    document.getElementById('t-grand').textContent = inr(grand);

    document.querySelectorAll('.intra-only').forEach(function (e) { e.style.display = inter ? 'none' : ''; });
    document.querySelectorAll('.inter-only').forEach(function (e) { e.style.display = inter ? '' : 'none'; });
    var tag = document.getElementById('supply-tag');
    if (tag) {
      tag.textContent = GST_ON ? (inter ? 'Inter-state supply - IGST' : 'Intra-state supply - CGST + SGST') : 'GST disabled in Settings';
      tag.className = 'badge ' + (GST_ON ? (inter ? 'b-amber' : 'b-green') : 'b-grey');
    }
    var br = document.getElementById('t-breakup');
    if (br) {
      var keys = Object.keys(buckets).sort(function (a, b) { return a - b; });
      br.textContent = keys.length
        ? keys.map(function (k) { return k + '% on ' + inr(buckets[k]); }).join('  |  ')
        : 'No GST on this bill.';
    }
  }

  document.getElementById('add-line').addEventListener('click', function () { addRow(); recalc(); });
  var ps = document.getElementById('party_id');
  if (ps) ps.addEventListener('change', function () {
    var manual = document.getElementById('manual-state');
    if (manual) manual.style.display = ps.value ? 'none' : '';
    recalc();
  });
  var sc = document.getElementById('state_code');
  if (sc) sc.addEventListener('change', recalc);

  addRow(); addRow();
  recalc();
})();
