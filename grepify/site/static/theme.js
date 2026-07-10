// grepify theme toggle - dark is the default on first load; the viewer's
// choice is remembered in localStorage and re-applied before first paint
// (this script is loaded in <head> without defer, so a stored "light" never
// flashes dark). Vanilla, no framework (PRD §5). Progressive enhancement:
// with JS off the button stays hidden and the site is simply dark.
(function () {
  "use strict";

  var KEY = "grepify-theme";
  var root = document.documentElement;

  function stored() {
    try {
      return window.localStorage.getItem(KEY);
    } catch (e) {
      return null; // storage blocked (private mode): toggle still works per page
    }
  }

  if (stored() === "light") {
    root.setAttribute("data-theme", "light");
  }

  document.addEventListener("DOMContentLoaded", function () {
    var btn = document.getElementById("theme-toggle");
    if (!btn) return;

    function sync() {
      var light = root.getAttribute("data-theme") === "light";
      btn.textContent = "theme: " + (light ? "light" : "dark");
      btn.setAttribute("aria-pressed", light ? "true" : "false");
    }

    btn.addEventListener("click", function () {
      var light = root.getAttribute("data-theme") !== "light";
      root.setAttribute("data-theme", light ? "light" : "dark");
      try {
        window.localStorage.setItem(KEY, light ? "light" : "dark");
      } catch (e) {
        // ignore: the choice just won't persist across pages
      }
      sync();
    });

    sync();
    btn.hidden = false;
  });
})();
