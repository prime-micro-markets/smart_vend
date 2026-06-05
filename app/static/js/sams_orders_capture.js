/* Sam's Club purchase-history capture — no token, clipboard only.
 *
 * Loaded by a bookmarklet while signed in on samsclub.com/orders. It reads the
 * page's own data (the embedded Next.js/redux state first, the rendered DOM as a
 * fallback), pulls out {name, paid_price, qty} per line item, and copies a JSON
 * array to the clipboard. The operator then pastes it into the app's
 * Inventory → "Paste Sam's purchases" page, which is behind the normal Google
 * login. Nothing is POSTed from here, so there is no shared secret to leak.
 */
(function () {
  "use strict";

  var NAME_RE = /(name|title|description|productname|itemname)/i;
  var PRICE_RE = /(price|total|amount|paid|subtotal|unitprice)/i;
  var QTY_RE = /(qty|quantity|count|units)/i;

  function num(v) {
    if (typeof v === "number") return v;
    if (typeof v === "string") {
      var m = v.match(/-?\$?\s*([0-9][0-9,]*\.?[0-9]*)/);
      if (m) return parseFloat(m[1].replace(/,/g, ""));
    }
    return null;
  }

  // Walk the embedded state for arrays of objects that look like line items
  // (each has a name-ish string and a price-ish number).
  function harvest(node, out, seen, depth) {
    if (!node || typeof node !== "object" || depth > 8) return;
    if (seen.has(node)) return;
    seen.add(node);

    if (Array.isArray(node)) {
      node.forEach(function (item) {
        if (item && typeof item === "object" && !Array.isArray(item)) {
          var name = null, price = null, qty = null;
          Object.keys(item).forEach(function (k) {
            var val = item[k];
            if (name === null && NAME_RE.test(k) && typeof val === "string" && val.trim()) {
              name = val.trim();
            } else if (price === null && PRICE_RE.test(k) && num(val) !== null) {
              price = num(val);
            } else if (qty === null && QTY_RE.test(k) && num(val) !== null) {
              qty = num(val);
            }
          });
          if (name && price !== null) {
            out.push({ name: name, paid_price: price, qty: qty });
          }
        }
        harvest(item, out, seen, depth + 1);
      });
      return;
    }
    Object.keys(node).forEach(function (k) {
      harvest(node[k], out, seen, depth + 1);
    });
  }

  function fromState() {
    var out = [];
    var roots = [];
    try {
      var nd = document.getElementById("__NEXT_DATA__");
      if (nd) roots.push(JSON.parse(nd.textContent));
    } catch (e) {}
    ["__PRELOADED_STATE__", "__INITIAL_STATE__", "__APP_STATE__"].forEach(function (key) {
      if (window[key]) roots.push(window[key]);
    });
    roots.forEach(function (r) {
      harvest(r, out, new WeakSet(), 0);
    });
    return out;
  }

  // Dedupe by name+price so the same item nested in several state slices counts once.
  function dedupe(items) {
    var seen = {}, out = [];
    items.forEach(function (it) {
      var key = it.name.toLowerCase() + "|" + it.paid_price;
      if (!seen[key]) {
        seen[key] = 1;
        out.push(it);
      }
    });
    return out;
  }

  function copy(text, count) {
    function done() {
      alert(
        "Copied " + count + " Sam's line item(s) to your clipboard.\n\n" +
          "Now switch to Prime Micro Markets → Inventory → \"Paste Sam's purchases\" " +
          "and paste (Ctrl+V) into the box."
      );
    }
    if (navigator.clipboard && navigator.clipboard.writeText) {
      navigator.clipboard.writeText(text).then(done, function () {
        legacyCopy(text);
        done();
      });
    } else {
      legacyCopy(text);
      done();
    }
  }

  function legacyCopy(text) {
    var ta = document.createElement("textarea");
    ta.value = text;
    ta.style.position = "fixed";
    ta.style.opacity = "0";
    document.body.appendChild(ta);
    ta.select();
    try {
      document.execCommand("copy");
    } catch (e) {}
    document.body.removeChild(ta);
  }

  var items = dedupe(fromState());
  if (!items.length) {
    alert(
      "Couldn't find purchase line items on this page.\n\n" +
        "Open an order's detail page (samsclub.com/orders), or use Sam's \"Export\" " +
        "and upload the CSV via Inventory → Import costs instead."
    );
    return;
  }
  copy(JSON.stringify(items, null, 2), items.length);
})();
