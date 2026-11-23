// Client-side search over window.LL_SEARCH: [ditemid, date, subject, text, commentText, tags]
(function () {
  var q = document.getElementById("ll-q"), cb = document.getElementById("ll-c"),
      out = document.getElementById("ll-results"), data = window.LL_SEARCH || [];
  function esc(s) { return s.replace(/[&<>"]/g, function (c) { return {"&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;"}[c]; }); }
  function snippet(text, terms) {
    var low = text.toLowerCase(), i = -1;
    for (var t = 0; t < terms.length && i < 0; t++) i = low.indexOf(terms[t]);
    var start = Math.max(0, i - 80), s = (start ? "…" : "") + text.substr(start, 240) + (text.length > start + 240 ? "…" : "");
    s = esc(s);
    terms.forEach(function (t) {
      if (!t) return;
      s = s.replace(new RegExp("(" + t.replace(/[.*+?^${}()|[\]\\]/g, "\\$&") + ")", "gi"), "<mark>$1</mark>");
    });
    return s;
  }
  function run() {
    var terms = q.value.toLowerCase().split(/\s+/).filter(Boolean), withC = cb.checked;
    try { history.replaceState(null, "", "#" + encodeURIComponent(q.value) + (withC ? "&c" : "")); } catch (e) {}
    if (!terms.length) { out.innerHTML = ""; return; }
    var hits = [];
    for (var i = data.length - 1; i >= 0; i--) {
      var d = data[i], hay = (d[2] + " " + d[3] + " " + d[5].join(" ") + (withC ? " " + d[4] : "")).toLowerCase();
      if (terms.every(function (t) { return hay.indexOf(t) >= 0; })) hits.push(d);
      if (hits.length >= 300) break;
    }
    out.innerHTML = "<p>" + hits.length + (hits.length >= 300 ? "+" : "") + " matching entries</p>" + hits.map(function (d) {
      var inBody = terms.some(function (t) { return (d[2] + d[3]).toLowerCase().indexOf(t) >= 0; });
      return '<div class="ll-hit"><a href="' + d[0] + '.html">' + esc(d[2] || "(no subject)") + "</a> <span class=\"ll-dim\">" +
        d[1] + "</span><br><small>" + snippet(inBody ? d[3] : d[4], terms) + "</small></div>";
    }).join("");
  }
  var h = decodeURIComponent(location.hash.slice(1));
  if (h) { if (/&c$/.test(h)) { cb.checked = true; h = h.slice(0, -2); } q.value = h; }
  q.addEventListener("input", run); cb.addEventListener("change", run); run();
})();
