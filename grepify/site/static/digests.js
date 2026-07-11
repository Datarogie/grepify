// grepify digest filters (GRP-43 kind filter + GRP-38 topic follow).
// Vanilla, no framework (PRD §5: no Node toolchain, interactivity stays small).
// Shared by the Digests index and the "Your digest" page. Two filters combine
// (logical AND) and only ever HIDE rows, never reorder them - so the server's
// newest-first-by-period order (GRP-37) always holds:
//   - kind: daily/weekly, via the optional <select id="filter-digest-kind">;
//   - topics: digest category, via follow chips backed by localStorage.
// With JS off both pages degrade to the full server-rendered list.
(function () {
  "use strict";

  var list = document.getElementById("digest-list");
  if (!list) return;
  var rows = Array.prototype.slice.call(list.querySelectorAll(".digest"));

  // --- followStore: the ONE accessor over the followed-topics set ----------
  // Every read/write of the follow-set goes through here. This is deliberate:
  // Kyle's roadmap is user profiles (saved settings + notifications), so a
  // future profile layer can replace this localStorage implementation without
  // touching any caller. The value is a JSON array of category slugs.
  var STORE_KEY = "grepify.followed_topics";
  var followStore = {
    get: function () {
      try {
        var raw = window.localStorage.getItem(STORE_KEY);
        var parsed = raw ? JSON.parse(raw) : [];
        if (!Array.isArray(parsed)) return [];
        return parsed.filter(function (v) { return typeof v === "string"; });
      } catch (e) {
        return []; // private mode / disabled storage / bad JSON -> no follows
      }
    },
    set: function (topics) {
      try {
        window.localStorage.setItem(STORE_KEY, JSON.stringify(topics));
      } catch (e) {
        // storage unavailable: the in-page selection still applies this visit,
        // it just will not persist across reloads.
      }
    },
    has: function (topic) {
      return this.get().indexOf(topic) !== -1;
    },
    toggle: function (topic) {
      var topics = this.get();
      var i = topics.indexOf(topic);
      if (i === -1) topics.push(topic);
      else topics.splice(i, 1);
      this.set(topics);
      return topics;
    },
  };

  // distinct categories present on this page, in first-seen (newest-first) order
  var categories = [];
  rows.forEach(function (row) {
    var cat = row.getAttribute("data-category");
    if (cat && categories.indexOf(cat) === -1) categories.push(cat);
  });

  // --- ?topics=a,b seed / override -----------------------------------------
  // A topics query param wins for this visit and is saved, so a shared link
  // seeds the same selection on another device. Only categories that actually
  // exist on the page are kept.
  (function seedFromUrl() {
    var match = /[?&]topics=([^&]*)/.exec(window.location.search);
    if (!match) return;
    var wanted = decodeURIComponent(match[1])
      .split(",")
      .map(function (s) { return s.trim(); })
      .filter(function (s) { return s.length > 0 && categories.indexOf(s) !== -1; });
    followStore.set(wanted);
  })();

  var kindSel = document.getElementById("filter-digest-kind");
  var empty = document.getElementById("digest-empty");
  var chipBox = document.getElementById("topic-chips");
  var shareBtn = document.getElementById("share-topics");

  function apply() {
    var kind = kindSel ? kindSel.value : "";
    var followed = followStore.get();
    var followAll = followed.length === 0; // nothing followed -> show all
    var visible = 0;
    rows.forEach(function (row) {
      var kindOk = !kind || row.getAttribute("data-kind") === kind;
      var topicOk = followAll || followed.indexOf(row.getAttribute("data-category")) !== -1;
      var show = kindOk && topicOk;
      row.hidden = !show;
      if (show) visible++;
    });
    if (empty) empty.hidden = visible !== 0;
    syncChips();
  }

  // --- topic follow chips ---------------------------------------------------
  function syncChips() {
    if (!chipBox) return;
    var buttons = chipBox.querySelectorAll("button[data-topic]");
    Array.prototype.forEach.call(buttons, function (b) {
      var on = followStore.has(b.getAttribute("data-topic"));
      b.setAttribute("aria-pressed", on ? "true" : "false");
    });
  }

  function renderChips() {
    if (!chipBox || categories.length === 0) return;
    categories.forEach(function (cat) {
      var b = document.createElement("button");
      b.type = "button";
      b.className = "topic-chip";
      b.setAttribute("data-topic", cat);
      b.setAttribute("aria-pressed", "false");
      b.textContent = cat;
      b.addEventListener("click", function () {
        followStore.toggle(cat);
        apply();
      });
      chipBox.appendChild(b);
    });
    chipBox.hidden = false;
  }

  // --- Share: a ?topics= link for the current follow-set -------------------
  function shareUrl() {
    var followed = followStore.get();
    var base = window.location.origin + window.location.pathname;
    if (followed.length === 0) return base;
    return base + "?topics=" + encodeURIComponent(followed.join(","));
  }

  if (shareBtn) {
    shareBtn.hidden = false; // reveal only when JS can build the link
    shareBtn.addEventListener("click", function () {
      var url = shareUrl();
      var label = shareBtn.textContent;
      function flash(msg) {
        shareBtn.textContent = msg;
        window.setTimeout(function () { shareBtn.textContent = label; }, 1500);
      }
      if (navigator.clipboard && navigator.clipboard.writeText) {
        navigator.clipboard.writeText(url).then(
          function () { flash("link copied"); },
          function () { window.prompt("Copy this link", url); }
        );
      } else {
        window.prompt("Copy this link", url);
      }
    });
  }

  if (kindSel) kindSel.addEventListener("change", apply);
  renderChips();
  apply();
})();
