/* LabForge - live usage sparklines.
 *
 * Usage cards (the host card and each VM's "VM Resources" card) are
 * server-rendered and refreshed by HTMX. This module keeps a short in-memory
 * history per card and paints a tiny sparkline into each
 * <canvas data-spark="...">, so a card reads like a small activity monitor
 * rather than static numbers.
 *
 * A card opts in with data-usage="<id>" plus data-cpu/data-mem/data-disk.
 */
(function () {
  "use strict";

  var MAX = 48;                     // ~2.5 min of history at 3s polls
  var METRICS = ["cpu", "mem", "disk"];
  var COLORS = { cpu: "#22d3ee", mem: "#34e5a0", disk: "#f5c451" };
  var histories = {};

  function key(id, metric) { return id + ":" + metric; }

  function draw(canvas, points, color) {
    var ctx = canvas.getContext("2d");
    if (!ctx) return;
    var w = canvas.width;
    var h = canvas.height;
    ctx.clearRect(0, 0, w, h);

    if (points.length < 2) return;

    var step = w / (MAX - 1);
    var offset = Math.max(0, MAX - points.length);
    var y = function (v) { return h - (Math.max(0, Math.min(100, v)) / 100) * (h - 4) - 2; };

    // filled area
    ctx.beginPath();
    ctx.moveTo(offset * step, h);
    for (var i = 0; i < points.length; i++) {
      ctx.lineTo((offset + i) * step, y(points[i]));
    }
    ctx.lineTo((offset + points.length - 1) * step, h);
    ctx.closePath();
    ctx.fillStyle = color + "22";
    ctx.fill();

    // line
    ctx.beginPath();
    for (var j = 0; j < points.length; j++) {
      var px = (offset + j) * step;
      var py = y(points[j]);
      if (j === 0) ctx.moveTo(px, py); else ctx.lineTo(px, py);
    }
    ctx.strokeStyle = color;
    ctx.lineWidth = 1.6;
    ctx.lineJoin = "round";
    ctx.stroke();

    // leading dot
    ctx.beginPath();
    ctx.arc((offset + points.length - 1) * step, y(points[points.length - 1]), 2.2, 0, Math.PI * 2);
    ctx.fillStyle = color;
    ctx.fill();
  }

  function sampleRoot(root) {
    var id = root.getAttribute("data-usage");
    if (!id) return;

    METRICS.forEach(function (metric) {
      var value = parseFloat(root.getAttribute("data-" + metric));
      if (isNaN(value)) value = 0;
      var h = histories[key(id, metric)] || (histories[key(id, metric)] = []);
      h.push(value);
      if (h.length > MAX) h.shift();
    });

    root.querySelectorAll("canvas[data-spark]").forEach(function (canvas) {
      var metric = canvas.getAttribute("data-spark");
      var h = histories[key(id, metric)];
      if (h) draw(canvas, h, COLORS[metric] || "#22d3ee");
    });
  }

  function sample() {
    document.querySelectorAll("[data-usage]").forEach(sampleRoot);
  }

  document.addEventListener("DOMContentLoaded", sample);
  document.body.addEventListener("htmx:afterSwap", sample);
  // In case HTMX has already swapped before this script ran.
  sample();
})();
