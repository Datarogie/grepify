// grepify digest filters (GRP-43 kind filter + GRP-38 topic follow +
// GRP-47 All/Following tabs + GRP-50 unfiltered All archive). Vanilla, no
// framework (PRD §5: no Node toolchain, interactivity stays small). Drives
// the single Digests page.
//
// Two views, selected by a progressively-enhanced tablist:
//   - All (default): the COMPLETE archive, nothing hidden. Neither the
//     daily/weekly kind filter nor the follow-set hides any row, and the
//     filter controls (kind form, topic chips, Share) are hidden.
//   - Following: the filter controls are shown; BOTH the daily/weekly kind
//     filter and the topic-follow filter apply. Selections are preserved
//     when toggling back from All (follows persist in localStorage; the kind
//     <select> keeps its value across tab switches within a visit).
// Filters only ever HIDE rows, never reorder them - so the server's
// newest-first-by-period order (GRP-37) always holds.
//
// The active view is per-visit + URL-seeded (?view=following), never
// persisted to localStorage (persisting it would re-create cross-visit
// stickiness). An incoming ?topics= still seeds the follow-set. With JS off
// the tablist and controls stay hidden and the page degrades to the full All
// list (the unfiltered archive).
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
  (function seedTopicsFromUrl() {
    var match = /[?&]topics=([^&]*)/.exec(window.location.search);
    if (!match) return;
    var wanted = decodeURIComponent(match[1])
      .split(",")
      .map(function (s) { return s.trim(); })
      .filter(function (s) { return s.length > 0 && categories.indexOf(s) !== -1; });
    followStore.set(wanted);
  })();

  var kindSel = document.getElementById("filter-digest-kind");
  var kindForm = document.getElementById("digest-filters");
  var topicFollow = document.getElementById("topic-follow");
  var empty = document.getElementById("digest-empty");
  var chipBox = document.getElementById("topic-chips");
  var shareBtn = document.getElementById("share-topics");
  var tablist = document.getElementById("digest-views");
  var tabs = tablist
    ? Array.prototype.slice.call(tablist.querySelectorAll("[data-view]"))
    : [];

  // --- active view: default "all", per-visit, seeded by ?view=following ----
  // Deliberately NOT persisted (see file header): the tab resets each visit
  // unless the URL selects it, so the archive is the default landing.
  var view = "all";
  (function seedViewFromUrl() {
    var match = /[?&]view=([^&]*)/.exec(window.location.search);
    if (match && decodeURIComponent(match[1]) === "following") view = "following";
  })();

  function apply() {
    var following = view === "following";
    var followed = followStore.get();
    // In All nothing is filtered: no kind filter and no follow filter (the
    // controls are hidden too). Both only apply under Following. An empty
    // follow-set under Following still shows everything (no dead end).
    var kind = following && kindSel ? kindSel.value : "";
    var followAll = !following || followed.length === 0;
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

  // --- All / Following tab --------------------------------------------------
  // The filter controls belong to Following: reveal the kind form, the topic
  // chips, and Share there, and hide all three in All (so All is the fully
  // unfiltered archive with no controls). The chip box only shows when there
  // are chips to show. Selections survive a hide/show: follows persist in
  // localStorage and the kind <select> keeps its value.
  function syncControls(following) {
    if (kindForm) kindForm.hidden = !following;
    if (topicFollow) topicFollow.hidden = !following;
    if (chipBox) chipBox.hidden = !following || categories.length === 0;
    if (shareBtn) shareBtn.hidden = !following;
  }

  function setView(next) {
    view = next === "following" ? "following" : "all";
    tabs.forEach(function (t) {
      var on = t.getAttribute("data-view") === view;
      t.setAttribute("aria-selected", on ? "true" : "false");
    });
    syncControls(view === "following");
    apply();
  }

  if (tablist) {
    tablist.hidden = false; // reveal only when JS can drive the views
    tabs.forEach(function (t) {
      t.addEventListener("click", function () {
        setView(t.getAttribute("data-view"));
      });
    });
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
    // Visibility is owned by setView/syncControls (chips show under Following
    // only); renderChips just builds the buttons.
  }

  // --- Share: a Following deep-link carrying the current follow-set --------
  function shareUrl() {
    var followed = followStore.get();
    var base = window.location.origin + window.location.pathname;
    var params = ["view=following"];
    if (followed.length > 0) params.push("topics=" + encodeURIComponent(followed.join(",")));
    return base + "?" + params.join("&");
  }

  if (shareBtn) {
    // Visibility is owned by setView/syncControls (Share shows under Following
    // only); here we just wire up the click.
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
  setView(view); // seed tab aria-selected + run the initial filter pass
})();
