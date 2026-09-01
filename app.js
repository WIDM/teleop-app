// ===== 信封工具：所有訊息共用（介面協定 v0）=====
const PROTOCOL_VERSION = 0;
let seq = 0;

function makeEnvelope(type, payload = {}) {
  seq += 1;
  return { v: PROTOCOL_VERSION, type, ts: Date.now(), seq, payload };
}

// ===== 連線狀態機：未連 / 已連 / 斷線重連中 =====
const STATE = {
  DISCONNECTED: "未連",
  CONNECTED: "已連",
  RECONNECTING: "斷線重連中",
};

let ws = null;
let state = STATE.DISCONNECTED;
let wantConnected = false; // 使用者按過連線且沒按斷線
let reconnectTimer = null;
let reconnectDelay = 1000; // ms，指數退避到 8 秒封頂

const $ = (id) => document.getElementById(id);
const statusEl = $("status");
const logEl = $("log");

const DEFAULT_URL = `ws://${location.hostname || "localhost"}:8765`;
$("serverUrl").value = localStorage.getItem("serverUrl") || DEFAULT_URL;

function setState(next) {
  state = next;
  statusEl.textContent = next;
  statusEl.className =
    "status " +
    {
      [STATE.DISCONNECTED]: "disconnected",
      [STATE.CONNECTED]: "connected",
      [STATE.RECONNECTING]: "reconnecting",
    }[next];
  const connected = next === STATE.CONNECTED;
  $("connectBtn").disabled = wantConnected;
  $("disconnectBtn").disabled = !wantConnected;
  $("helloBtn").disabled = !connected;
  $("testBtn").disabled = !connected;
}

const LOG_MAX = 200; // 手機端只留最近 200 則，避免高頻訊息撐爆 DOM

function addLog(dir, text) {
  const li = document.createElement("li");
  const time = new Date().toLocaleTimeString("zh-TW", { hour12: false });
  li.innerHTML =
    `<span class="dir ${dir}">${dir === "out" ? "送" : "收"}</span>` +
    `<span class="time">${time}</span><code>${escapeHtml(text)}</code>`;
  logEl.prepend(li);
  while (logEl.children.length > LOG_MAX) logEl.lastChild.remove();
}

function escapeHtml(s) {
  return s.replace(
    /[&<>"']/g,
    (c) =>
      ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[
        c
      ],
  );
}

function send(type, payload) {
  if (!ws || ws.readyState !== WebSocket.OPEN) return;
  const msg = makeEnvelope(type, payload);
  ws.send(JSON.stringify(msg));
  addLog("out", JSON.stringify(msg));
}

function connect() {
  const url = $("serverUrl").value.trim();
  if (!url) return;
  localStorage.setItem("serverUrl", url);

  try {
    ws = new WebSocket(url);
  } catch (e) {
    addLog("in", `連線失敗：${e.message}`);
    scheduleReconnect();
    return;
  }

  ws.onopen = () => {
    reconnectDelay = 1000;
    setState(STATE.CONNECTED);
    send("hello", { client: "phone", name: "thomas-phone" });
  };

  ws.onmessage = (ev) => {
    // 協定只走 JSON 純文字；binary frame 直接標示不解析
    if (typeof ev.data === "string") addLog("in", ev.data);
    else addLog("in", "(忽略非文字 frame)");
  };

  ws.onclose = () => {
    ws = null;
    if (wantConnected) {
      setState(STATE.RECONNECTING);
      scheduleReconnect();
    } else {
      setState(STATE.DISCONNECTED);
    }
  };

  ws.onerror = () => {
    /* onclose 會跟著觸發，重連交給它 */
  };
}

function scheduleReconnect() {
  clearTimeout(reconnectTimer);
  reconnectTimer = setTimeout(() => {
    if (wantConnected) connect();
  }, reconnectDelay);
  reconnectDelay = Math.min(reconnectDelay * 2, 8000);
}

$("connectBtn").onclick = () => {
  wantConnected = true;
  setState(STATE.RECONNECTING);
  connect();
};

$("disconnectBtn").onclick = () => {
  wantConnected = false;
  clearTimeout(reconnectTimer);
  if (ws) ws.close();
  else setState(STATE.DISCONNECTED);
};

$("helloBtn").onclick = () =>
  send("hello", { client: "phone", name: "thomas-phone" });
$("testBtn").onclick = () => send("test", { msg: "hello" });
$("clearBtn").onclick = () => {
  logEl.innerHTML = "";
};

setState(STATE.DISCONNECTED);
