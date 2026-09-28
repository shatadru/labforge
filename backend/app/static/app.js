/* LabForge - small UI helpers shared by all pages. */

/**
 * Copy text to the clipboard.
 *
 * navigator.clipboard is only available in secure contexts (https/localhost),
 * so fall back to the legacy execCommand path for plain-HTTP access.
 */
window.copyText = function (text, el) {
  function feedback() {
    if (!el) return;
    if (!el.dataset.originalLabel) el.dataset.originalLabel = el.textContent;
    el.textContent = "✓ Copied";
    setTimeout(function () {
      el.textContent = el.dataset.originalLabel;
    }, 1200);
  }

  function legacyCopy() {
    var ta = document.createElement("textarea");
    ta.value = text;
    ta.setAttribute("readonly", "");
    ta.style.position = "fixed";
    ta.style.top = "-1000px";
    ta.style.opacity = "0";
    document.body.appendChild(ta);
    ta.select();
    ta.setSelectionRange(0, text.length);
    var ok = false;
    try { ok = document.execCommand("copy"); } catch (e) { ok = false; }
    document.body.removeChild(ta);
    if (ok) feedback();
  }

  if (navigator.clipboard && window.isSecureContext) {
    navigator.clipboard.writeText(text).then(feedback).catch(legacyCopy);
  } else {
    legacyCopy();
  }
};

/**
 * Turn an API error body into a readable sentence.
 *
 * FastAPI validation errors return detail as a list of objects, which used to
 * render as "[object Object]". Strings, lists and objects are all handled.
 */
window.errorText = function (body) {
  if (!body) return "Request failed";
  var d = body.detail !== undefined ? body.detail : body;
  if (typeof d === "string") return d;
  if (Array.isArray(d)) {
    return d.map(function (e) {
      if (typeof e === "string") return e;
      if (!e || typeof e !== "object") return String(e);
      var loc = Array.isArray(e.loc)
        ? e.loc.filter(function (x) { return x !== "body" && x !== "query" && x !== "path"; }).join(".")
        : "";
      return (loc ? loc + ": " : "") + (e.msg || JSON.stringify(e));
    }).join("; ");
  }
  if (typeof d === "object") return d.msg || JSON.stringify(d);
  return String(d);
};

/** Parse a response body as JSON, falling back to text. */
window.readBody = function (resp) {
  return resp.text().then(function (text) {
    try { return JSON.parse(text); } catch (e) { return { detail: text }; }
  }).catch(function () { return { detail: "Request failed" }; });
};

/** Mark a button busy while an HTMX action is in flight. */
window.vmBusy = function (el, busy) {
  if (!el) return;
  el.disabled = !!busy;
  el.classList.toggle("is-busy", !!busy);
};

/**
 * Refresh the VM grid after an action.
 *
 * On the dashboard a #vm-grid exists, so we re-render just that fragment.
 * Anywhere else (for example the VM page) we fall back to a full reload.
 */
window.refreshGrid = function () {
  var grid = document.getElementById("vm-grid");
  if (grid && window.htmx) {
    window.htmx.ajax("GET", "/partials/vms", { target: "#vm-grid", swap: "innerHTML" });
  } else {
    window.location.reload();
  }
};

/**
 * Refresh just the status badge on the VM page.
 */
window.refreshStatus = function (name) {
  var el = document.getElementById("vm-status");
  if (el && window.htmx) {
    window.htmx.ajax("GET", "/partials/vm-status/" + encodeURIComponent(name),
                     { target: "#vm-status", swap: "innerHTML" });
  }
};

/**
 * One reliable place that reacts to VM actions.
 *
 * Inline hx-on handlers proved fragile (scope of `this` and `event`), so the
 * busy state, card removal and refresh are handled centrally from HTMX's own
 * events. e.detail.elt is always the element that issued the request.
 */
(function () {
  function isButton(elt) {
    return elt && elt.classList && elt.classList.contains("btn");
  }

  document.body.addEventListener("htmx:beforeRequest", function (e) {
    var elt = e.detail && e.detail.elt;
    if (isButton(elt)) {
      elt.classList.add("is-busy");
      elt.disabled = true;
    }
  });

  document.body.addEventListener("htmx:afterRequest", function (e) {
    var elt = e.detail && e.detail.elt;
    if (isButton(elt)) {
      elt.classList.remove("is-busy");
      if (elt.isConnected !== false) elt.disabled = false;
    }

    var cfg = e.detail.requestConfig || {};
    var verb = (cfg.verb || "").toLowerCase();
    var path = cfg.path || "";
    var card = elt && elt.closest ? elt.closest(".vm-card") : null;

    // Deleting a VM: drop the card, or leave the detail page.
    if (verb === "delete" && /^\/api\/v1\/vms\/[^/]+$/.test(path)) {
      if (e.detail.successful) {
        if (card) { card.remove(); window.refreshGrid(); }
        else { window.location.assign("/"); }
      } else if (card) {
        window.refreshGrid();
      }
      return;
    }

    // Starting, stopping, rebooting or resetting a VM.
    if (verb === "post" && /^\/api\/v1\/vms\/[^/]+\/(start|stop|reboot|reset)$/.test(path)) {
      var name = path.split("/")[4];
      var refresh = card ? window.refreshGrid : function () { window.refreshStatus(name); };
      refresh();
      // The state can take a moment to change, so refresh again shortly after.
      setTimeout(refresh, 3000);
      return;
    }

    // Snapshots: reload only on success, so a failure stays readable on screen.
    if (path.indexOf("/snapshots") !== -1 && e.detail.successful) {
      window.location.reload();
    }
  });
})();

/**
 * Live server status dot in the sidebar.
 *
 * Polls /api/health and turns the dot green (radiating) while LabForge
 * responds, red when it does not, so a lab user can see at a glance whether
 * the server is up.
 */
(function () {
  var dot = document.getElementById("server-dot");
  var text = document.getElementById("server-status-text");
  if (!dot || !text) return;

  var failures = 0;

  function setState(state, label) {
    dot.classList.remove("up", "down");
    if (state) dot.classList.add(state);
    text.textContent = label;
  }

  function ping() {
    fetch("/api/health", { cache: "no-store" })
      .then(function (r) {
        if (!r.ok) throw new Error("bad status " + r.status);
        return r.json();
      })
      .then(function () {
        failures = 0;
        setState("up", "LabForge online");
      })
      .catch(function () {
        failures += 1;
        setState("down", failures >= 2 ? "LabForge offline" : "Reconnecting…");
      });
  }

  ping();
  setInterval(ping, 5000);
  document.addEventListener("visibilitychange", function () {
    if (!document.hidden) ping();
  });
})();

/** Small toast, used so action failures are visible instead of silent.
 *
 * Errors stay on screen until dismissed, so the message can be read and copied.
 */
window.showToast = function (message, kind) {
  var host = document.getElementById("toast-host");
  if (!host) {
    host = document.createElement("div");
    host.id = "toast-host";
    host.className = "toast-host";
    document.body.appendChild(host);
  }
  var toast = document.createElement("div");
  toast.className = "toast " + (kind || "info");
  // Errors are assertive so screen readers announce them immediately.
  if (kind === "error") {
    toast.setAttribute("role", "alert");
    toast.setAttribute("aria-live", "assertive");
  } else {
    toast.setAttribute("role", "status");
    toast.setAttribute("aria-live", "polite");
  }

  var text = document.createElement("span");
  text.className = "toast-text";
  text.textContent = message;
  toast.appendChild(text);

  var close = document.createElement("button");
  close.type = "button";
  close.className = "toast-close";
  close.setAttribute("aria-label", "Dismiss");
  close.textContent = "\u00d7";
  close.addEventListener("click", dismiss);
  toast.appendChild(close);

  host.appendChild(toast);

  function dismiss() {
    toast.classList.remove("visible");
    setTimeout(function () { toast.remove(); }, 300);
  }

  setTimeout(function () { toast.classList.add("visible"); }, 10);
  if (kind !== "error") setTimeout(dismiss, 4000);
};

// Surface HTMX action failures (start, stop, delete, snapshots) as a toast.
document.body.addEventListener("htmx:responseError", function (e) {
  var xhr = e.detail && e.detail.xhr;
  var body = { detail: "Request failed" };
  if (xhr) {
    try { body = JSON.parse(xhr.responseText); }
    catch (err) { body = { detail: xhr.status + " " + (xhr.statusText || "request failed") }; }
  }
  window.showToast(window.errorText(body), "error");
});
document.body.addEventListener("htmx:sendError", function () {
  window.showToast("Cannot reach the LabForge server", "error");
});

// Delegated UI actions. Using data-* attributes keeps Jinja values out of
// inline JavaScript string literals, where a quote could break out of the
// string, and avoids inline handlers that a strict CSP would block.
document.body.addEventListener("click", function (e) {
  var el = e.target.closest ? e.target.closest(
    "[data-copy],[data-console],[data-screen],[data-provision]") : null;
  if (!el) return;
  var d = el.dataset;
  if (d.copy !== undefined) {
    window.copyText(d.copy, el);
  } else if (d.console) {
    window.openConsole(d.console);
  } else if (d.screen) {
    window.openScreen(d.screen);
  } else if (d.provision) {
    window.showProvisionForm(d.provision, Number(d.mem), Number(d.cpu), Number(d.disk));
  }
});

document.body.addEventListener("submit", function (e) {
  var form = e.target.closest ? e.target.closest("[data-snapshot-vm]") : null;
  if (!form) return;
  e.preventDefault();
  window.submitSnapshot(e, form.dataset.snapshotVm);
});

