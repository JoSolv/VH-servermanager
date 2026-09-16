/* Live dashboard client.
 *
 * One websocket carries a full snapshot of every instance on each sampling
 * tick; this script patches the DOM in place rather than re-rendering, so the
 * page stays responsive and htmx keeps control of everything interactive.
 */
(function () {
  "use strict";

  var SPARK_POINTS = 40;
  var history = {};      // instanceId -> [cpu%]
  var lastStatus = {};   // instanceId -> status, to refresh buttons on change

  // ---------------------------------------------------------------- utils
  function fmtBytes(value) {
    value = value || 0;
    var units = ["B", "KiB", "MiB", "GiB", "TiB"], i = 0;
    while (Math.abs(value) >= 1024 && i < units.length - 1) { value /= 1024; i++; }
    return (i === 0 ? Math.round(value) : value.toFixed(1)) + " " + units[i];
  }

  function fmtDuration(seconds) {
    seconds = Math.floor(seconds || 0);
    if (seconds < 60) return seconds + "s";
    var m = Math.floor(seconds / 60), s = seconds % 60;
    if (m < 60) return m + "m " + s + "s";
    var h = Math.floor(m / 60); m = m % 60;
    if (h < 24) return h + "h " + m + "m";
    return Math.floor(h / 24) + "d " + (h % 24) + "h";
  }

  function setText(scope, field, text) {
    var node = scope.querySelector('[data-f="' + field + '"]');
    if (node && node.textContent !== text) node.textContent = text;
  }

  function setMeter(scope, name, percent) {
    var meter = scope.querySelector('[data-meter="' + name + '"]');
    if (!meter) return;
    var bar = meter.firstElementChild;
    if (bar) bar.style.width = Math.max(0, Math.min(100, percent)) + "%";
    meter.classList.remove("ok", "warn", "err");
    meter.classList.add(percent >= 90 ? "err" : percent >= 70 ? "warn" : "ok");
  }

  function drawSpark(scope, id, value) {
    var svg = scope.querySelector('[data-spark="cpu"]');
    if (!svg) return;
    var series = history[id] || (history[id] = []);
    series.push(value);
    if (series.length > SPARK_POINTS) series.shift();

    var line = svg.querySelector("polyline");
    if (!line) return;
    var w = 100, h = 30, peak = Math.max(10, Math.max.apply(null, series));
    var points = series.map(function (v, i) {
      var x = series.length < 2 ? 0 : (i / (series.length - 1)) * w;
      return x.toFixed(1) + "," + (h - (v / peak) * h).toFixed(1);
    });
    line.setAttribute("points", points.join(" "));
  }

  // ------------------------------------------------------------- rendering
  function applyInstance(snapshot) {
    var scope = document.querySelector('[data-instance="' + snapshot.id + '"]');
    if (!scope) return;

    var pill = scope.querySelector('[data-f="status"]');
    if (pill) {
      pill.textContent = snapshot.status;
      pill.className = "pill " + snapshot.status;
    }

    var players = snapshot.players || {};
    var metrics = snapshot.metrics || {};
    var net = snapshot.net || {};
    var active = ["starting", "running", "stopping"].indexOf(snapshot.status) >= 0;

    setText(scope, "players", players.count + (players.max ? " / " + players.max : ""));
    setText(scope, "uptime", active ? fmtDuration(snapshot.uptime) : "--");
    setText(scope, "cpu", active ? metrics.cpu_percent.toFixed(1) + "%" : "--");
    setText(scope, "mem", active ? fmtBytes(metrics.memory_rss) : "--");
    setText(scope, "threads", active ? String(metrics.threads) : "--");
    setText(scope, "pid", snapshot.pid ? String(snapshot.pid) : "--");
    setText(
      scope, "net",
      net.source === "unavailable" || !active
        ? "--"
        : "↓ " + fmtBytes(net.rx_rate) + "/s  ↑ " + fmtBytes(net.tx_rate) + "/s"
    );

    setMeter(scope, "cpu", active ? metrics.cpu_percent : 0);
    setMeter(scope, "mem", active ? metrics.memory_percent : 0);
    drawSpark(scope, snapshot.id, active ? metrics.cpu_percent : 0);

    renderPlayers(scope, players.players || []);

    // Status changed: let the server re-render the action buttons.
    if (lastStatus[snapshot.id] !== snapshot.status) {
      lastStatus[snapshot.id] = snapshot.status;
      var controls = scope.querySelector("[data-controls]");
      if (controls && window.htmx) {
        window.htmx.ajax("GET", "/instances/" + snapshot.id + "/controls", { target: controls, swap: "outerHTML" });
      }
    }
  }

  function renderPlayers(scope, players) {
    var list = scope.querySelector("[data-players-list]");
    if (!list) return;
    if (!players.length) {
      if (list.dataset.state !== "empty") {
        list.dataset.state = "empty";
        list.innerHTML = '<li class="muted">Nobody connected</li>';
      }
      return;
    }
    var signature = players.map(function (p) { return p.name + p.playtime; }).join("|");
    if (list.dataset.state === signature) return;
    list.dataset.state = signature;
    list.innerHTML = players.map(function (p) {
      return '<li><strong></strong> <span class="muted"></span></li>';
    }).join("");
    // Fill via textContent so player names can never inject markup.
    Array.prototype.forEach.call(list.children, function (li, i) {
      li.querySelector("strong").textContent = players[i].name;
      li.querySelector("span").textContent = fmtDuration(players[i].playtime);
    });
  }

  function applyHost(payload) {
    var host = payload.host || {}, net = payload.host_net || {};
    var strip = document.querySelector("[data-host]");
    if (strip) {
      setText(strip, "host-cpu", host.cpu_percent.toFixed(0) + "%");
      setText(strip, "host-mem", fmtBytes(host.memory_used) + " / " + fmtBytes(host.memory_total));
      setText(strip, "host-load", (host.load || []).join("  "));
      setText(strip, "host-net", "↓ " + fmtBytes(net.rx_rate) + "/s  ↑ " + fmtBytes(net.tx_rate) + "/s");
      setMeter(strip, "host-cpu", host.cpu_percent);
      setMeter(strip, "host-mem", host.memory_percent);
    }
    var summary = document.getElementById("host-summary");
    if (summary) {
      var running = (payload.instances || []).filter(function (i) { return i.status === "running"; }).length;
      var online = (payload.instances || []).reduce(function (sum, i) { return sum + (i.players.count || 0); }, 0);
      summary.textContent = running + " running · " + online + " player(s) online · host CPU " +
        host.cpu_percent.toFixed(0) + "%";
    }
  }

  // ------------------------------------------------------------- transport
  function setConnState(state, label) {
    var pill = document.getElementById("conn-state");
    if (!pill) return;
    pill.className = "conn " + state;
    pill.querySelector(".label").textContent = label;
  }

  function connect() {
    var proto = location.protocol === "https:" ? "wss:" : "ws:";
    var socket = new WebSocket(proto + "//" + location.host + "/ws/metrics");

    socket.onopen = function () { setConnState("live", "live"); };
    socket.onmessage = function (event) {
      var payload;
      try { payload = JSON.parse(event.data); } catch (e) { return; }
      if (payload.type !== "snapshot") return;
      applyHost(payload);
      (payload.instances || []).forEach(applyInstance);
    };
    socket.onclose = function () {
      setConnState("down", "reconnecting");
      setTimeout(connect, 3000);
    };
    socket.onerror = function () { socket.close(); };
  }

  // --------------------------------------------------------------- console
  function attachConsole() {
    var box = document.getElementById("console");
    if (!box) return;
    var instanceId = box.dataset.instanceId;
    var proto = location.protocol === "https:" ? "wss:" : "ws:";

    function open() {
      var socket = new WebSocket(proto + "//" + location.host + "/ws/console/" + instanceId);
      socket.onmessage = function (event) {
        var pinned = box.scrollHeight - box.scrollTop - box.clientHeight < 40;
        var line = document.createElement("div");
        line.className = "line";
        if (event.data.indexOf("[manager]") >= 0) line.classList.add("mgr");
        if (/error|exception|failed/i.test(event.data)) line.classList.add("err");
        line.textContent = event.data;      // never innerHTML: log text is untrusted
        box.appendChild(line);
        while (box.childElementCount > 1200) box.removeChild(box.firstChild);
        if (pinned) box.scrollTop = box.scrollHeight;
      };
      socket.onclose = function () { setTimeout(open, 3000); };
      socket.onerror = function () { socket.close(); };
    }
    open();
  }

  document.addEventListener("DOMContentLoaded", function () {
    connect();
    attachConsole();
  });
})();
