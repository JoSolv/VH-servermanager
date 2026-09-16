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
      if (snapshot.operation) {
        pill.textContent = snapshot.operation;
        pill.className = "pill busy";
      } else {
        pill.textContent = snapshot.status;
        pill.className = "pill " + snapshot.status;
      }
    }

    var players = snapshot.players || {};
    var metrics = snapshot.metrics || {};
    var net = snapshot.net || {};
    var active = ["starting", "running", "stopping"].indexOf(snapshot.status) >= 0;

    setText(scope, "players", players.count + (players.max ? " / " + players.max : ""));
    setText(scope, "uptime", active ? fmtDuration(snapshot.uptime) : "--");
    // metrics.cpu_percent is psutil's raw figure and is summed across cores
    // (300% = three cores). Show the share of the whole host instead, with the
    // core count underneath, so the number and its meter agree.
    setText(scope, "cpu", active ? metrics.cpu_host_percent.toFixed(1) + "%" : "--");
    setText(
      scope, "cpu-cores",
      active ? metrics.cpu_cores.toFixed(2) + " of " + metrics.cpu_count + " cores" : "\u00a0"
    );
    setText(scope, "version", snapshot.version || "\u2014");
    setText(scope, "mem", active ? fmtBytes(metrics.memory_rss) : "--");
    setText(scope, "threads", active ? String(metrics.threads) : "--");
    setText(scope, "pid", snapshot.pid ? String(snapshot.pid) : "--");
    setText(
      scope, "net",
      net.source === "unavailable" || !active
        ? "--"
        : "↓ " + fmtBytes(net.rx_rate) + "/s  ↑ " + fmtBytes(net.tx_rate) + "/s"
    );

    setMeter(scope, "cpu", active ? metrics.cpu_host_percent : 0);
    setMeter(scope, "mem", active ? metrics.memory_percent : 0);
    drawSpark(scope, snapshot.id, active ? metrics.cpu_host_percent : 0);

    renderPlayers(scope, snapshot.id, players.players || []);

    // Status changed: let the server re-render the action buttons.
    if (lastStatus[snapshot.id] !== snapshot.status) {
      lastStatus[snapshot.id] = snapshot.status;
      var controls = scope.querySelector("[data-controls]");
      if (controls && window.htmx) {
        window.htmx.ajax("GET", "/instances/" + snapshot.id + "/controls", { target: controls, swap: "outerHTML" });
      }
    }
  }

  function cell(row, text, className) {
    var td = document.createElement("td");
    if (className) td.className = className;
    td.textContent = text;              // textContent: player names are untrusted
    row.appendChild(td);
    return td;
  }

  function actionButton(label, action, instanceId, playerId, danger) {
    var b = document.createElement("button");
    b.className = "small" + (danger ? " danger" : "");
    b.textContent = label;
    b.dataset.moderate = action;
    b.dataset.instance = instanceId;
    b.dataset.player = playerId;
    return b;
  }

  function renderPlayers(scope, instanceId, players) {
    var body = scope.querySelector("[data-players-body]");
    if (!body) {
      // Dashboard cards have no table; nothing else to do there.
      return;
    }
    // Re-render only when something actually changed, so buttons stay clickable.
    var signature = players.map(function (p) {
      return [p.name, p.player_id, p.playtime, p.admin, p.banned, p.permitted, p.in_world].join(",");
    }).join("|");
    if (body.dataset.state === signature) return;
    body.dataset.state = signature;
    body.innerHTML = "";

    if (!players.length) {
      var empty = document.createElement("tr");
      var td = document.createElement("td");
      td.colSpan = 5; td.className = "muted"; td.textContent = "Nobody connected";
      empty.appendChild(td); body.appendChild(empty);
      return;
    }

    players.forEach(function (p) {
      var row = document.createElement("tr");
      cell(row, p.name + (p.in_world ? "" : " (loading)"));

      var idCell = document.createElement("td");
      if (p.player_id) {
        var code = document.createElement("code");
        code.textContent = p.player_id;
        idCell.appendChild(code);
        var plat = document.createElement("div");
        plat.className = "muted"; plat.style.fontSize = "11px";
        plat.textContent = p.platform;
        idCell.appendChild(plat);
      } else {
        idCell.className = "muted";
        idCell.textContent = "not seen in log";
      }
      row.appendChild(idCell);

      cell(row, fmtDuration(p.playtime));

      var flags = [];
      if (p.admin) flags.push("admin");
      if (p.permitted) flags.push("permitted");
      if (p.banned) flags.push("banned");
      cell(row, flags.join(", ") || "—", flags.length ? "" : "muted");

      var acts = document.createElement("td");
      var wrap = document.createElement("div");
      wrap.className = "acts";
      if (p.can_moderate) {
        wrap.appendChild(actionButton(p.admin ? "Un-admin" : "Admin",
          p.admin ? "unadmin" : "admin", instanceId, p.player_id, false));
        wrap.appendChild(actionButton("Kick", "kick", instanceId, p.player_id, true));
        wrap.appendChild(actionButton(p.banned ? "Unban" : "Ban",
          p.banned ? "unban" : "ban", instanceId, p.player_id, true));
      } else {
        wrap.textContent = "—";
      }
      acts.appendChild(wrap);
      row.appendChild(acts);
      body.appendChild(row);
    });
  }

  // Delegated so buttons rendered after page load still work; htmx only
  // processes markup it swapped in itself.
  document.addEventListener("click", function (event) {
    var button = event.target.closest("[data-moderate]");
    if (!button || !window.htmx) return;
    var action = button.dataset.moderate;
    if (action === "ban" && !confirm("Ban " + button.dataset.player + "?")) return;
    window.htmx.ajax(
      "POST",
      "/api/instances/" + button.dataset.instance + "/players/" +
        encodeURIComponent(button.dataset.player) + "/" + action,
      { target: "#player-lists", swap: "outerHTML" }
    );
  });

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
