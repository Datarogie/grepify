// grepify digests index - client-side daily/weekly filter (GRP-43).
// Vanilla, no framework (PRD §5: no Node toolchain, interactivity stays small).
// Toggles list rows by their data-kind attribute; an empty value shows all.
(function () {
  "use strict";

  var list = document.getElementById("digest-list");
  if (!list) return;
  var rows = Array.prototype.slice.call(list.querySelectorAll(".digest"));
  var kindSel = document.getElementById("filter-digest-kind");
  var empty = document.getElementById("digest-empty");
  if (!kindSel) return;

  function apply() {
    var kind = kindSel.value;
    var visible = 0;
    rows.forEach(function (row) {
      var show = !kind || row.getAttribute("data-kind") === kind;
      row.hidden = !show;
      if (show) visible++;
    });
    if (empty) empty.hidden = visible !== 0;
  }

  kindSel.addEventListener("change", apply);
  apply();
})();
