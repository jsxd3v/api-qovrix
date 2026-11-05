// Refresh the page content in place (no full reload) when the page asks for it.
(function () {
  "use strict";
  var seconds = parseInt(document.body.getAttribute("data-refresh") || "0", 10);
  if (!seconds) return;

  function refresh() {
    if (document.hidden) return;
    fetch(window.location.href, { credentials: "same-origin", headers: { Accept: "text/html" } })
      .then(function (r) {
        if (r.redirected || !r.ok) throw new Error("stop");
        return r.text();
      })
      .then(function (html) {
        var next = new DOMParser().parseFromString(html, "text/html").getElementById("main");
        var main = document.getElementById("main");
        if (next && main) main.innerHTML = next.innerHTML;
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
