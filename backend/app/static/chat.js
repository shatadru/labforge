// LabForge chat - progressive enhancement on top of the HTMX-rendered partial.
//
// The server renders the message list; this file adds a live SSE connection, a
// few client-side affordances (local timestamps, autoscroll, star filter,
// optimistic send) and avatar fallbacks. Reactions/pin/star/delete are plain
// HTMX buttons and need no JS here.
(function () {
  "use strict";

  var card = document.querySelector("[data-chat]");
  if (!card) return;

  var list = document.getElementById("chat-messages");
  var form = card.querySelector("[data-chat-form]");
  var input = form ? form.querySelector(".chat-input") : null;
  var jump = card.querySelector("[data-chat-jump]");
  var statusEl = card.querySelector("[data-chat-status]");
  var connEl = card.querySelector("[data-chat-conn]");
  var starBtn = card.querySelector("[data-chat-star-filter]");

  var starredOnly = false;
  var refreshTimer = null;

  function nearBottom() {
    return list.scrollHeight - list.scrollTop - list.clientHeight < 80;
  }
  function toBottom() {
    list.scrollTop = list.scrollHeight;
  }
  function sameDay(a, b) {
    return a.getFullYear() === b.getFullYear() &&
      a.getMonth() === b.getMonth() && a.getDate() === b.getDate();
  }

  function formatTimes() {
    var now = new Date();
    var yesterday = new Date(now.getTime() - 86400000);
    Array.prototype.forEach.call(list.querySelectorAll(".chat-time[data-ts]"), function (el) {
      var ts = parseInt(el.getAttribute("data-ts"), 10) * 1000;
      if (!ts) return;
      var d = new Date(ts);
      el.textContent = d.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" });
      el.title = d.toLocaleString();
    });
    Array.prototype.forEach.call(list.querySelectorAll(".chat-day [data-ts]"), function (el) {
      var d = new Date(parseInt(el.getAttribute("data-ts"), 10) * 1000);
      var label;
      if (sameDay(d, now)) label = "Today";
      else if (sameDay(d, yesterday)) label = "Yesterday";
      else label = d.toLocaleDateString([], { weekday: "short", day: "numeric", month: "short" });
      el.textContent = label;
    });
  }

  function applyStarFilter() {
    Array.prototype.forEach.call(list.querySelectorAll(".chat-message"), function (m) {
      m.classList.toggle("chat-hidden", starredOnly && !m.classList.contains("starred"));
    });
  }

  function setConn(up) {
    if (!connEl) return;
    connEl.classList.toggle("live", up);
    connEl.classList.toggle("down", !up);
    connEl.title = up ? "Live" : "Reconnecting\u2026";
  }

  function showStatus(msg, isError) {
    if (!statusEl) return;
    statusEl.textContent = msg || "";
    statusEl.classList.toggle("error", !!isError);
  }

  function afterRender(wasBottom) {
    formatTimes();
    applyStarFilter();
    if (wasBottom) {
      toBottom();
      if (jump) jump.hidden = true;
    } else if (jump) {
      jump.hidden = false;
    }
  }

  function refresh() {
    if (!window.htmx) return;
    window.htmx.ajax("GET", "/partials/chat/messages",
      { target: "#chat-messages", swap: "innerHTML" });
  }

  function scheduleRefresh() {
    if (refreshTimer) return;
    refreshTimer = setTimeout(function () { refreshTimer = null; refresh(); }, 150);
  }

  // Preserve scroll intent across every swap of the list.
  document.body.addEventListener("htmx:beforeSwap", function (e) {
    if (e.target && e.target.id === "chat-messages") list._wasBottom = nearBottom();
  });
  document.body.addEventListener("htmx:afterSwap", function (e) {
    if (e.target && e.target.id === "chat-messages") {
      var wasBottom = list._wasBottom !== false;
      list._wasBottom = true;
      afterRender(wasBottom);
    }
  });
  document.body.addEventListener("htmx:responseError", function (e) {
    if (e.detail && card.contains(e.detail.elt)) {
      showStatus("Could not reach the chat backend.", true);
    }
  });

  // Live updates, with a slow polling fallback if SSE cannot stay connected.
  var fallbackTimer = null;

  function startFallback() {
    if (fallbackTimer) return;
    fallbackTimer = setInterval(refresh, 10000);
  }
  function stopFallback() {
    if (fallbackTimer) {
      clearInterval(fallbackTimer);
      fallbackTimer = null;
    }
  }
  function connect() {
    if (!("EventSource" in window)) {
      startFallback();
      return;
    }
    var es = new EventSource("/chat/stream");
    es.addEventListener("open", function () { setConn(true); stopFallback(); });
    es.addEventListener("error", function () { setConn(false); startFallback(); });
    es.addEventListener("chat", scheduleRefresh);
  }
  setConn(false);
  connect();

  // Send: handled directly so it does not depend on HTMX form binding.
  if (form) {
    form.addEventListener("submit", function (e) {
      e.preventDefault();
      var text = (input && input.value || "").trim();
      if (!text) return;
      var temp = document.createElement("article");
      temp.className = "chat-message mine pending";
      temp.innerHTML = '<div class="chat-body"><div class="chat-html"></div></div>';
      temp.querySelector(".chat-html").textContent = text;
      list.appendChild(temp);
      toBottom();
      list._wasBottom = true;
      if (input) input.value = "";
      showStatus("Sending\u2026");
      fetch("/chat/send", {
        method: "POST",
        headers: { "Content-Type": "application/x-www-form-urlencoded" },
        credentials: "same-origin",
        body: "message=" + encodeURIComponent(text),
      }).then(function (r) {
        if (!r.ok) throw new Error("HTTP " + r.status);
        return r.text();
      }).then(function (html) {
        list.innerHTML = html;
        if (window.htmx) window.htmx.process(list);
        list._wasBottom = true;
        afterRender(true);
        showStatus("");
      }).catch(function () {
        if (input && !input.value) input.value = text;
        showStatus("Message failed to send.", true);
        refresh();
      });
    });
  }

  if (jump) {
    jump.addEventListener("click", function () { toBottom(); jump.hidden = true; });
  }

  // Reaction picker: click to open (works on touch), click outside to close.
  function closeReactMenus() {
    Array.prototype.forEach.call(document.querySelectorAll(".chat-react-add.open"), function (w) {
      w.classList.remove("open");
    });
  }
  document.body.addEventListener("click", function (e) {
    var toggle = e.target.closest ? e.target.closest("[data-react-toggle]") : null;
    if (toggle) {
      e.preventDefault();
      var wrap = toggle.closest(".chat-react-add");
      var wasOpen = wrap.classList.contains("open");
      closeReactMenus();
      if (!wasOpen) wrap.classList.add("open");
      return;
    }
    if (!(e.target.closest && e.target.closest(".chat-react-menu"))) closeReactMenus();
  });

  if (starBtn) {
    starBtn.addEventListener("click", function () {
      starredOnly = !starredOnly;
      starBtn.setAttribute("aria-pressed", starredOnly ? "true" : "false");
      starBtn.classList.toggle("on", starredOnly);
      applyStarFilter();
    });
  }
})();
