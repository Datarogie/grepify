// grepify items browser — client-side filter (GRP-33).
// Vanilla, no framework (PRD §5: no Node toolchain, interactivity stays small).
// Mirrors grepify.site.pages.item_matches_filter EXACTLY — that Python function
// is the pinned contract; keep the two in sync. Operates on the server-rendered
// rows' data-* attributes (safely autoescaped at build time); the per-page
// items/page-N.json is the same data emitted as a machine-readable artifact.
(function () {
  "use strict";

  var list = document.getElementById("items-list");
  if (!list) return;
  var rows = Array.prototype.slice.call(list.querySelectorAll(".item"));
  var kindSel = document.getElementById("filter-kind");
  var sourceSel = document.getElementById("filter-source");
  var keywordInput = document.getElementById("filter-keyword");
  var clearBtn = document.getElementById("filter-clear");
  var empty = document.getElementById("items-empty");

  // Build the kind/source dropdown options from the rows present on this page.
  function facet(attr) {
    var seen = {};
    rows.forEach(function (row) {
      var v = row.getAttribute(attr) || "";
      if (v) seen[v] = true;
    });
    return Object.keys(seen).sort();
  }
  function fill(sel, values, labelFor) {
    values.forEach(function (v) {
      var opt = document.createElement("option");
      opt.value = v;
      opt.textContent = labelFor ? labelFor(v) : v;
      sel.appendChild(opt);
    });
  }
  var sourceNames = {};
  rows.forEach(function (row) {
    var id = row.getAttribute("data-source");
    var meta = row.querySelector(".meta");
    if (id && meta && !sourceNames[id]) sourceNames[id] = meta.textContent.split("·")[0].trim();
  });
  fill(kindSel, facet("data-kind"));
  fill(sourceSel, facet("data-source"), function (id) { return sourceNames[id] || id; });

  // The predicate — identical semantics to item_matches_filter (AND of active
  // filters; kind/source exact, keyword case-insensitive substring on tags).
  function matches(row, kind, source, keyword) {
    if (kind && row.getAttribute("data-kind") !== kind) return false;
    if (source && row.getAttribute("data-source") !== source) return false;
    if (keyword) {
      var needle = keyword.trim().toLowerCase();
      // data-keywords is a JSON array of (possibly multi-word) keyword phrases,
      // so a phrase like "agentic coding" survives round-trip; substring-match
      // the needle against each whole phrase — identical to item_matches_filter.
      var tags = [];
      try { tags = JSON.parse(row.getAttribute("data-keywords") || "[]"); } catch (e) { tags = []; }
      if (needle && !tags.some(function (t) { return String(t).toLowerCase().indexOf(needle) !== -1; })) {
        return false;
      }
    }
    return true;
  }

  function apply() {
    var kind = kindSel.value;
    var source = sourceSel.value;
    var keyword = keywordInput.value;
    var visible = 0;
    rows.forEach(function (row) {
      var show = matches(row, kind, source, keyword);
      row.hidden = !show;
      if (show) visible++;
    });
    if (empty) empty.hidden = visible !== 0;
  }

  // Deep-link: the keyword cloud links here with #keyword=<term>.
  function readHash() {
    var m = /[#&]keyword=([^&]+)/.exec(window.location.hash || "");
    if (m) {
      try { keywordInput.value = decodeURIComponent(m[1]); } catch (e) { keywordInput.value = m[1]; }
    }
  }

  kindSel.addEventListener("change", apply);
  sourceSel.addEventListener("change", apply);
  keywordInput.addEventListener("input", apply);
  if (clearBtn) {
    clearBtn.addEventListener("click", function () {
      kindSel.value = "";
      sourceSel.value = "";
      keywordInput.value = "";
      apply();
    });
  }
  window.addEventListener("hashchange", function () { readHash(); apply(); });

  readHash();
  apply();
})();
