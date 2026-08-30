/* delegate — client behaviour. Deliberately small and dependency-free.
 *
 * Three jobs: filter the list, keep a live conversation fresh, and get you to
 * the bottom of a long one.
 *
 * The refresh is a conditional GET of an HTML fragment. The server hashes the
 * fragment into an ETag, so a poll against a conversation that has not moved
 * is a 304 with an empty body — a few hundred bytes over the tunnel instead of
 * a re-render and a re-download of a megabyte of transcript. That is what
 * makes it acceptable to leave this page open on a phone while a run finishes.
 *
 * The token is never touched by this file. It lives in an HttpOnly cookie the
 * browser attaches on its own, so a stored XSS in a transcript still cannot
 * read it — and if the cookie ever stops working, polling stops rather than
 * hammering the server with 401s forever.
 */
(function () {
  "use strict";

  // ── list filter ──────────────────────────────────────────────────────
  var filter = document.getElementById("filter");
  if (filter) {
    filter.addEventListener("input", function () {
      var q = filter.value.trim().toLowerCase();
      var items = document.querySelectorAll(".runs .run");
      for (var i = 0; i < items.length; i++) {
        var hay = items[i].getAttribute("data-search") || "";
        items[i].style.display = (!q || hay.indexOf(q) !== -1) ? "" : "none";
      }
    });
  }

  // ── live fragment refresh ────────────────────────────────────────────
  var host = document.querySelector("[data-fragment]");
  if (host) {
    var url = host.getAttribute("data-fragment");
    var every = parseInt(host.getAttribute("data-poll") || "0", 10);
    var etag = null;
    var timer = null;

    function openIds() {
      var open = {};
      var nodes = host.querySelectorAll("details[data-id]");
      for (var i = 0; i < nodes.length; i++) {
        if (nodes[i].open) open[nodes[i].getAttribute("data-id")] = true;
      }
      return open;
    }

    function restore(open) {
      var nodes = host.querySelectorAll("details[data-id]");
      for (var i = 0; i < nodes.length; i++) {
        if (open[nodes[i].getAttribute("data-id")]) nodes[i].open = true;
      }
    }

    function nearBottom() {
      var slack = window.innerHeight * 0.4;
      return (window.innerHeight + window.scrollY) >=
             (document.body.scrollHeight - slack);
    }

    function poll() {
      var headers = { "X-Requested-With": "fetch" };
      if (etag) headers["If-None-Match"] = etag;
      fetch(url, { headers: headers, cache: "no-store", credentials: "same-origin" })
        .then(function (res) {
          if (res.status === 304) return null;
          if (res.status === 401 || res.status === 404) {
            // The session is gone or the conversation vanished. Stop rather
            // than retry: a phone in a pocket retrying forever is the one way
            // this page can become a nuisance.
            clearInterval(timer);
            return null;
          }
          if (!res.ok) return null;
          etag = res.headers.get("ETag");
          return res.text();
        })
        .then(function (html) {
          if (html === null || html === undefined) return;
          var wasOpen = openIds();
          var stick = nearBottom();
          host.innerHTML = html;
          restore(wasOpen);
          if (stick) window.scrollTo(0, document.body.scrollHeight);
        })
        .catch(function () { /* offline or asleep; the next tick retries */ });
    }

    if (every > 0) {
      timer = setInterval(poll, every * 1000);
      // Coming back to a backgrounded tab should show the truth immediately,
      // not whatever the interval left behind.
      document.addEventListener("visibilitychange", function () {
        if (!document.hidden) poll();
      });
    }
  }

  // ── jump to latest ───────────────────────────────────────────────────
  var conv = document.getElementById("conv");
  var btn = document.getElementById("to-bottom");
  if (conv && btn) {
    // A live conversation opens at the newest message: you tapped it to see
    // what the agent is doing now, not to reread how it started.
    if (conv.getAttribute("data-live") === "1") {
      window.scrollTo(0, document.body.scrollHeight);
    }
    var update = function () {
      var far = (document.body.scrollHeight - window.scrollY - window.innerHeight) > 600;
      btn.classList.toggle("show", far);
    };
    window.addEventListener("scroll", update, { passive: true });
    update();
    btn.addEventListener("click", function () {
      window.scrollTo({ top: document.body.scrollHeight, behavior: "smooth" });
    });
  }
})();
