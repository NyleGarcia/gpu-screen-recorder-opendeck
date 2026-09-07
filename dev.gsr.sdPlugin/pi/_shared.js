// Connecting to OpenDeck, once, for every inspector in this plugin.
//
// Two conventions are in the wild and OpenDeck ships both: the Elgato SDK
// calls a global function with the connection details, while OpenDeck's own
// newer plugins await a global promise of the same tuple. Supporting both is
// four lines and removes a whole class of "the panel is blank" bug.
//
// The context to send on is inActionInfo.context -- the ACTION's context, not
// the inspector's own uuid. Sending the inspector's uuid instead is accepted
// by the socket and then routed nowhere: settings are never saved, the
// plugin is never asked for its lists, and the panel sits empty with no error
// anywhere to explain it.
(function () {
  let socket = null;
  let context = null;
  let action = null;
  let controller = "Keypad";
  let settings = {};
  let answered = false;
  const listeners = [];

  function start(port, _uuid, registerEvent, _info, actionInfo) {
    const info = typeof actionInfo === "string"
      ? JSON.parse(actionInfo) : actionInfo;
    context = info.context;
    action = info.action;
    settings = (info.payload && info.payload.settings) || {};
    controller = (info.payload && info.payload.controller) || "Keypad";
    socket = new WebSocket("ws://127.0.0.1:" + port);
    socket.onopen = () => {
      socket.send(JSON.stringify({ event: registerEvent, uuid: _uuid }));
      GSR.ask();
      // Asking once is not enough: the socket can be open before the plugin
      // has finished registering, and a reply that lands then is answered to
      // nobody. Retry until a payload arrives, then stop.
      let tries = 0;
      const retry = setInterval(() => {
        if (answered || ++tries > 6) { clearInterval(retry); return; }
        GSR.ask();
      }, 600);
    };
    socket.onmessage = (event) => {
      let message;
      try { message = JSON.parse(event.data); } catch (e) { return; }
      if (message.event === "sendToPropertyInspector") {
        answered = true;
        report("payload " + JSON.stringify(message.payload || {}).length + "b");
        try {
          listeners.forEach((fn) => fn(message.payload || {}));
        } catch (err) {
          report("render threw: " + err);
        }
      } else if (message.event === "didReceiveSettings") {
        settings = (message.payload && message.payload.settings) || settings;
      }
    };
    window.addEventListener("error", (e) => report(
      "error: " + e.message + " @" + e.filename + ":" + e.lineno));
    listeners.forEach((fn) => fn(null));
  }

  window.connectElgatoStreamDeckSocket = start;
  window.connectOpenActionSocket = start;
  if (globalThis.connectOpenActionSocketData) {
    Promise.resolve(globalThis.connectOpenActionSocketData)
      .then((details) => start.apply(null, details));
  }

  // A webview inside a Tauri window has no console anyone can read, so the
  // inspector reports what it did back to the plugin, which has a log file.
  // Diagnosing "the dropdown is empty" any other way means guessing.
  function report(text) {
    if (!socket || socket.readyState !== WebSocket.OPEN) return;
    socket.send(JSON.stringify({
      event: "sendToPlugin", context, action,
      payload: { debug: text, action },
    }));
  }

  window.GSR = {
    report,
    controller: () => controller,
    settings: () => settings,
    onPayload(fn) { listeners.push(fn); },
    ask() {
      if (!socket || socket.readyState !== WebSocket.OPEN) return;
      socket.send(JSON.stringify({
        event: "sendToPlugin", context, action,
        payload: { request: "state", action },
      }));
    },
    // Plugin-wide, not per key: a deck with one key in Amber and the rest in
    // Default reads as broken rather than customised.
    saveGlobal(changes) {
      if (!socket || socket.readyState !== WebSocket.OPEN) return;
      socket.send(JSON.stringify({
        event: "setGlobalSettings", context, payload: changes,
      }));
    },
    // Every inspector ends with the same two blocks -- the plugin-wide
    // colour choice and a readout of what is running -- so they are filled in
    // from here rather than copied into six pages that would drift apart.
    theme(payload) {
      const node = document.getElementById("theme");
      if (!node || !payload) return;
      if (payload.themes && !node.options.length) {
        for (const theme of payload.themes) {
          node.appendChild(new Option(theme.label, theme.id));
        }
        node.onchange = () => GSR.saveGlobal({ theme: node.value });
      }
      if (payload.theme) node.value = payload.theme;
    },
    running(payload) {
      const node = document.getElementById("running");
      if (!node || !payload) return;
      const rows = [];
      if (!payload.ui_cli) {
        rows.push(["gsr-ui-cli", "not installed"]);
      } else {
        rows.push(["gsr-ui", payload.ui ? "running" : "not running"]);
      }
      for (const inst of payload.running || []) {
        rows.push([inst.mode, (inst.capture || "?")
          + (inst.ipc ? " · ipc" : "")]);
      }
      if (!(payload.running || []).length) {
        rows.push(["recorder", "nothing running"]);
      }
      node.innerHTML = rows.map(([term, value]) =>
        "<div><dt>" + term + "</dt><dd>" + value + "</dd></div>").join("");
    },

    save(changes) {
      Object.assign(settings, changes);
      if (!socket || socket.readyState !== WebSocket.OPEN) return;
      socket.send(JSON.stringify({
        event: "setSettings", context, payload: settings,
      }));
    },
  };
})();
