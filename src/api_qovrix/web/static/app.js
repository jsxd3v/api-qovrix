// api-qovrix dashboard: auto-refresh, search, filters and collapsible services. Pages work without it.
(function () {
  "use strict";
  document.body.classList.remove("no-js");

  var state = { query: "", filter: "all", closed: {} };

  function applyFilters() {
    var rows = document.querySelectorAll("tr.monitor");
    if (!rows.length) return;
    var q = state.query.trim().toLowerCase();
    var shown = 0;
    rows.forEach(function (row) {
      var okText = !q || (row.getAttribute("data-search") || "").indexOf(q) !== -1;
      var rowState = row.getAttribute("data-state");
      var okState = state.filter === "all" || rowState === state.filter ||
        (state.filter === "up" && rowState === "up");
      var visible = okText && okState;
      row.hidden = !visible;
      var next = row.nextElementSibling;
      if (next && next.classList.contains("error-row")) next.hidden = !visible;
      if (visible) shown++;
    });
    document.querySelectorAll("details.service").forEach(function (svc) {
      var any = svc.querySelector("tr.monitor:not([hidden])");
      svc.hidden = !any;
      var name = svc.getAttribute("data-service");
      if (state.closed[name]) svc.removeAttribute("open");
    });
    var hint = document.getElementById("filter-hint");
    if (hint) {
      var filtering = q || state.filter !== "all";
      hint.textContent = filtering ? shown + " of " + rows.length + " monitors" : "";
    }
  }

  function bind() {
    var input = document.getElementById("filter");
    if (input) {
      input.value = state.query;
      input.addEventListener("input", function () {
        state.query = input.value;
        applyFilters();
      });
    }
    document.querySelectorAll(".chips button").forEach(function (btn) {
      btn.setAttribute("aria-pressed", String(btn.getAttribute("data-filter") === state.filter));
      btn.addEventListener("click", function () {
        state.filter = btn.getAttribute("data-filter");
        document.querySelectorAll(".chips button").forEach(function (b) {
          b.setAttribute("aria-pressed", String(b === btn));
        });
        applyFilters();
      });
    });
    document.querySelectorAll("details.service").forEach(function (svc) {
      svc.addEventListener("toggle", function () {
        state.closed[svc.getAttribute("data-service")] = !svc.open;
      });
    });
    applyFilters();
  }

  bind();

  var seconds = parseInt(document.body.getAttribute("data-refresh") || "0", 10);
  if (!seconds) return;

  function refresh() {
    if (document.hidden) return;
    var active = document.activeElement && document.activeElement.id;
    fetch(window.location.href, { credentials: "same-origin", headers: { Accept: "text/html" } })
      .then(function (r) {
        if (r.redirected || !r.ok) throw new Error("stop");
        return r.text();
      })
      .then(function (html) {
        var next = new DOMParser().parseFromString(html, "text/html").getElementById("main");
        var main = document.getElementById("main");
        if (!next || !main) return;
        main.innerHTML = next.innerHTML;
        bind();
        if (active) {
          var el = document.getElementById(active);
          if (el) el.focus();
        }
      })
      .catch(function () {
        var note = document.getElementById("refresh-note");
        if (note) note.textContent = "auto-update paused (reload the page)";
        clearInterval(timer);
      });
  }

  var timer = setInterval(refresh, seconds * 1000);
  document.addEventListener("visibilitychange", function () {
    if (!document.hidden) refresh();
  });
})();
