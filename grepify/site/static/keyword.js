// grepify keyword page - tabbed "latest content" by kind (GRP-44).
// Vanilla, no framework (PRD §5). Progressive enhancement: with JS off, every
// panel is visible (only the first is hidden at render, so worst case one panel
// hides - acceptable, and the links still work). With JS on, tabs switch panels.
(function () {
  "use strict";

  var tabs = Array.prototype.slice.call(document.querySelectorAll("[data-kind-tab]"));
  var panels = Array.prototype.slice.call(document.querySelectorAll("[data-kind-panel]"));
  if (!tabs.length) return;

  function select(kind) {
    tabs.forEach(function (tab) {
      var on = tab.getAttribute("data-kind-tab") === kind;
      if (on) {
        tab.setAttribute("aria-selected", "true");
      } else {
        tab.removeAttribute("aria-selected");
      }
    });
    panels.forEach(function (panel) {
      panel.hidden = panel.getAttribute("data-kind-panel") !== kind;
    });
  }

  tabs.forEach(function (tab) {
    tab.addEventListener("click", function () {
      select(tab.getAttribute("data-kind-tab"));
    });
  });
})();
