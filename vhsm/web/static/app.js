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

  function pushHistory(id, value) {
    var series = history[id] || (history[id] = []);
    series.push(value);
    if (series.length > SPARK_POINTS) series.shift();
  }

  function drawSpark(scope, id) {
    var svg = scope.querySelector('[data-spark="cpu"]');
    if (!svg) return;
    var series = history[id] || [];
    if (!series.length) return;

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
    // An instance can have more than one region on a page -- the detail page
    // puts its address and reachability in the header, outside the main block
    // -- so every matching region is updated, not just the first.
    var scopes = document.querySelectorAll('[data-instance="' + snapshot.id + '"]');
    if (!scopes.length) return;
    pushHistory(snapshot.id, snapshot.metrics && snapshot.status !== "stopped"
      ? snapshot.metrics.cpu_host_percent : 0);
    Array.prototype.forEach.call(scopes, function (scope) {
      applyToScope(scope, snapshot);
    });

    // Status changed: let the server re-render the action buttons.
    if (lastStatus[snapshot.id] !== snapshot.status) {
      lastStatus[snapshot.id] = snapshot.status;
      var controls = document.querySelector(
        '[data-instance="' + snapshot.id + '"] [data-controls]'
      );
      if (controls && window.htmx) {
        window.htmx.ajax("GET", "/instances/" + snapshot.id + "/controls", { target: controls, swap: "outerHTML" });
      }
    }
  }

  function applyToScope(scope, snapshot) {

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

    setText(scope, "players", String(players.count));
    setText(scope, "players-max", players.max ? "/ " + players.max : "");
    setText(scope, "uptime", active ? fmtDuration(snapshot.uptime) : "--");
    // metrics.cpu_percent is psutil's raw figure and is summed across cores
    // (300% = three cores). Show the share of the whole host instead, with the
    // core count beside it, so the number and its meter agree.
    setText(scope, "cpu", active ? metrics.cpu_host_percent.toFixed(1) + "%" : "--");
    setText(
      scope, "cpu-cores",
      active ? metrics.cpu_cores.toFixed(2) + " / " + metrics.cpu_count + " cores" : "\u00a0"
    );
    setText(scope, "version", snapshot.version || "\u2014");
    applyReach(scope, snapshot);
    setText(scope, "mem", active ? fmtBytes(metrics.memory_rss) : "--");
    setText(scope, "threads", active ? String(metrics.threads) : "--");
    setText(scope, "pid", snapshot.pid ? String(snapshot.pid) : "--");
    // Threads and pid are context for the numbers above, not numbers to watch,
    // so they ride in one quiet line rather than two tiles of their own.
    setText(
      scope, "proc",
      active && snapshot.pid
        ? metrics.threads + " threads \u00b7 pid " + snapshot.pid
        : ""
    );
    setText(
      scope, "net",
      net.source === "unavailable" || !active
        ? "--"
        : "↓ " + fmtBytes(net.rx_rate) + "/s  ↑ " + fmtBytes(net.tx_rate) + "/s"
    );

    setMeter(scope, "cpu", active ? metrics.cpu_host_percent : 0);
    setMeter(scope, "mem", active ? metrics.memory_percent : 0);
    drawSpark(scope, snapshot.id);

    renderPlayers(scope, snapshot.id, players.players || []);
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

  function applyReach(scope, snapshot) {
    var node = scope.querySelector('[data-f="reach"]');
    if (!node) return;
    var active = ["starting", "running", "stopping"].indexOf(snapshot.status) >= 0;
    var label, cls;
    if (!active) {
      label = "not running"; cls = "reach";
    } else if (snapshot.reachable === true) {
      label = "reachable"; cls = "reach yes";
    } else if (snapshot.reachable === false) {
      // The process holding its game port is the real "is it up" signal; a
      // silent query socket is a separate, weaker fact.
      label = snapshot.listening ? "up, query silent" : "not reachable";
      cls = snapshot.listening ? "reach warn" : "reach no";
    } else if (snapshot.listening) {
      label = "up"; cls = "reach yes";
    } else {
      label = "checking\u2026"; cls = "reach";
    }
    if (node.textContent !== label) node.textContent = label;
    if (node.className !== cls) node.className = cls;
    node.title = snapshot.reachable_detail ||
      "Probed from this host, so a router that does not loop traffic back can " +
      "report a working server as unreachable.";
  }

  // ------------------------------------------------------------ card menu
  // The menus are plain <details>, so opening and closing is free; all that is
  // left is making them behave like menus -- one open at a time, and a click
  // anywhere else puts them away.
  document.addEventListener("click", function (event) {
    var inside = event.target.closest && event.target.closest("details.menu");
    document.querySelectorAll("details.menu[open]").forEach(function (menu) {
      if (menu !== inside) menu.open = false;
    });
    // A menu item was picked: close the menu rather than leave it hanging over
    // the page while the action runs.
    if (inside && event.target.closest(".menu-body")) inside.open = false;
  });

  document.addEventListener("keydown", function (event) {
    if (event.key !== "Escape") return;
    document.querySelectorAll("details.menu[open]").forEach(function (menu) {
      menu.open = false;
    });
  });

  // Copy an address without needing to select it by hand.
  document.addEventListener("click", function (event) {
    var button = event.target.closest("[data-copy]");
    if (!button) return;
    var value = button.dataset.copy;
    var done = function () {
      var original = button.textContent;
      button.textContent = "copied";
      setTimeout(function () { button.textContent = original; }, 1200);
    };
    if (navigator.clipboard && navigator.clipboard.writeText) {
      navigator.clipboard.writeText(value).then(done, function () {});
    }
  });

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
      td.colSpan = 4; td.className = "muted"; td.textContent = "Nobody connected";
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
      { target: "#players", swap: "outerHTML" }
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

  // A directory picker submits every file in the folder, but the folder's own
  // name only exists in webkitRelativePath. Valheim 1.0 takes the world name
  // from that folder, so it has to travel with the upload.
  document.addEventListener("change", function (event) {
    var input = event.target;
    if (!input.dataset || !input.dataset.folderName) return;
    var target = document.getElementById(input.dataset.folderName);
    if (!target) return;
    var first = input.files && input.files[0];
    var relative = first && (first.webkitRelativePath || "");
    target.value = relative ? relative.split("/")[0] : "";
  });

  // ------------------------------------------------------------- roster
  // The players panel is re-rendered by htmx after every moderation action, so
  // the search box and the active chip cannot live in the DOM: they are held
  // here and put back on the fresh markup.
  var roster = { query: "", filter: "all" };

  function applyRoster() {
    var rows = document.querySelectorAll('#players tr[data-row]');
    var shown = 0;
    Array.prototype.forEach.call(rows, function (row) {
      var hit = !roster.query || (row.dataset.search || "").indexOf(roster.query) >= 0;
      if (hit && roster.filter !== "all") hit = row.dataset[roster.filter] === "1";
      row.hidden = !hit;
      // A hidden row must take its action strip with it, or the strip is left
      // floating under somebody else's name.
      var strip = row.nextElementSibling;
      if (!hit && strip && strip.hasAttribute("data-strip")) strip.hidden = true;
      if (hit) shown++;
    });
    var none = document.querySelector("#players .roster-none");
    if (none) none.hidden = shown > 0 || !rows.length;
  }

  function syncRoster() {
    var box = document.querySelector("#players [data-roster-search]");
    if (!box) return;
    if (box.value !== roster.query) box.value = roster.query;
    document.querySelectorAll("#players [data-filter]").forEach(function (chip) {
      chip.classList.toggle("on", chip.dataset.filter === roster.filter);
    });
    applyRoster();
  }

  document.addEventListener("input", function (event) {
    var box = event.target.closest && event.target.closest("[data-roster-search]");
    if (!box) return;
    roster.query = box.value.trim().toLowerCase();
    applyRoster();
  });

  document.addEventListener("click", function (event) {
    var chip = event.target.closest && event.target.closest("[data-filter]");
    if (chip) {
      roster.filter = chip.dataset.filter;
      syncRoster();
      return;
    }
    // The "..." toggle: one contextual action stays on the row, the rest live
    // in a strip that opens under it.
    var more = event.target.closest && event.target.closest("[data-more]");
    if (!more) return;
    var strip = more.closest("tr").nextElementSibling;
    if (!strip || !strip.hasAttribute("data-strip")) return;
    strip.hidden = !strip.hidden;
    more.classList.toggle("on", !strip.hidden);
  });

  // --------------------------------------------------------- world import
  // One field takes either shape a world arrives in. A picked .zip goes
  // straight through the form; a dropped folder is walked and sent file by
  // file with its path, which is exactly what a directory picker submits.
  function syncUploadButton(scope) {
    var form = (scope || document).querySelector("[data-world-form]");
    if (!form) return;
    var input = form.querySelector("[data-world-files]");
    var button = form.querySelector("[data-world-submit]");
    if (!input || !button || input.disabled) return;
    button.disabled = !(input.files && input.files.length);
  }

  document.addEventListener("change", function (event) {
    if (event.target.closest && event.target.closest("[data-world-files]")) syncUploadButton();
  });

  function walkEntry(entry, prefix, out) {
    return new Promise(function (resolve) {
      if (entry.isFile) {
        entry.file(
          function (file) { out.push({ file: file, path: prefix + entry.name }); resolve(); },
          resolve
        );
        return;
      }
      // readEntries hands back at most a page of children at a time, so it has
      // to be called until it returns nothing.
      var reader = entry.createReader(), children = [];
      (function readMore() {
        reader.readEntries(function (batch) {
          if (!batch.length) {
            Promise.all(children.map(function (child) {
              return walkEntry(child, prefix + entry.name + "/", out);
            })).then(resolve);
            return;
          }
          children = children.concat(Array.prototype.slice.call(batch));
          readMore();
        }, resolve);
      })();
    });
  }

  function fireTriggers(header) {
    var events;
    try { events = JSON.parse(header); } catch (e) { return; }
    Object.keys(events).forEach(function (name) {
      document.body.dispatchEvent(
        new CustomEvent(name, { detail: events[name], bubbles: true })
      );
    });
  }

  function sendWorld(form, files, worldName) {
    var panel = document.getElementById("transfer");
    // Replacing a world that is already there is destructive, so a drop asks
    // exactly what the browse-and-submit path asks through hx-confirm.
    var ask = form.dataset.worldConfirm;
    if (ask && !confirm(ask)) return;

    var payload = new FormData();
    files.forEach(function (item) { payload.append("files", item.file, item.path); });
    payload.append("name", worldName || "");
    if (ask) payload.append("confirm", "1");

    if (panel) panel.classList.add("htmx-request");
    fetch(form.dataset.url, { method: "POST", body: payload })
      .then(function (response) {
        return response.text().then(function (text) {
          return { text: text, trigger: response.headers.get("HX-Trigger") };
        });
      })
      .then(function (result) {
        if (window.htmx) {
          window.htmx.swap("#transfer", result.text, { swapStyle: "outerHTML" });
        }
        if (result.trigger) fireTriggers(result.trigger);
      })
      .catch(function () {
        if (panel) panel.classList.remove("htmx-request");
      });
  }

  function dropZone(event) {
    var zone = event.target.closest && event.target.closest("[data-world-drop]");
    if (!zone) return null;
    var input = zone.querySelector("[data-world-files]");
    return input && !input.disabled ? zone : null;
  }

  document.addEventListener("dragover", function (event) {
    var zone = dropZone(event);
    if (!zone) return;
    event.preventDefault();
    zone.classList.add("dragging");
  });

  document.addEventListener("dragleave", function (event) {
    var zone = dropZone(event);
    if (zone && !zone.contains(event.relatedTarget)) zone.classList.remove("dragging");
  });

  document.addEventListener("drop", function (event) {
    var zone = dropZone(event);
    if (!zone) return;
    event.preventDefault();
    zone.classList.remove("dragging");
    var form = zone.querySelector("[data-world-form]");
    var items = event.dataTransfer && event.dataTransfer.items;
    if (!form || !items) return;

    var entries = [];
    for (var i = 0; i < items.length; i++) {
      var entry = items[i].webkitGetAsEntry && items[i].webkitGetAsEntry();
      if (entry) entries.push(entry);
    }
    if (!entries.length) return;

    var collected = [];
    Promise.all(entries.map(function (entry) {
      return walkEntry(entry, "", collected);
    })).then(function () {
      if (!collected.length) return;
      // A dropped folder carries the world's name; a dropped zip carries its
      // own, so leave it to the server to read out of the archive.
      sendWorld(form, collected, entries[0].isDirectory ? entries[0].name : "");
    });
  });

  // ------------------------------------------------- collapsible sections
  // Sections start closed, and htmx replaces whole panels, so the open ones
  // are remembered per browser rather than reset on every swap.
  var SECTION_KEY = "vhsm.sections";

  function openSections() {
    try {
      return JSON.parse(localStorage.getItem(SECTION_KEY) || "{}") || {};
    } catch (e) {
      return {};                      // private mode, blocked storage, bad JSON
    }
  }

  function rememberSection(name, open) {
    try {
      var state = openSections();
      if (open) { state[name] = 1; } else { delete state[name]; }
      localStorage.setItem(SECTION_KEY, JSON.stringify(state));
    } catch (e) { /* storage is a convenience, never a requirement */ }
  }

  function restoreSections(scope) {
    var state = openSections();
    (scope || document).querySelectorAll("details[data-remember]").forEach(function (el) {
      el.open = !!state[el.dataset.remember];
    });
  }

  document.addEventListener("toggle", function (event) {
    var el = event.target;
    if (el && el.dataset && el.dataset.remember) rememberSection(el.dataset.remember, el.open);
  }, true);

  document.body.addEventListener("htmx:afterSwap", function (event) {
    restoreSections(event.target);
    syncRoster();
    syncUploadButton();
  });

  // Uploading a world repoints the instance at it. The configuration form
  // below was rendered with the old name, so keep it in step rather than let
  // a later save send the stale value back.
  document.body.addEventListener("vhsm:world-renamed", function (event) {
    var input = document.getElementById("world");
    if (input && event.detail && event.detail.world) {
      input.value = event.detail.world;
      input.classList.add("changed");
      setTimeout(function () { input.classList.remove("changed"); }, 2500);
    }
  });

  document.addEventListener("DOMContentLoaded", function () {
    restoreSections();
    syncRoster();
    syncUploadButton();
    connect();
    attachConsole();
  });
})();
