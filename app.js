// ===== 信封工具：所有訊息共用（介面協定 v0 ＋ hold 擴充 2026-09-09）=====
const PROTOCOL_VERSION = 1;
let seq = 0; // 全程遞增、不重用（含 keepalive 與 stop）

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

// 動作閘門：要 welcome 收到、且 state.estop=false 才開放動作鍵
let welcomed = false;
let estopActive = null; // null＝還沒收到 state
let currentStep = "fine"; // 預設 fine（Leo 2026-09-09 要求）
let stepConfig = null; // welcome 帶來的 {coarse_cm, fine_cm}

const $ = (id) => document.getElementById(id);
const statusEl = $("status");
const logEl = $("log");

const DEFAULT_URL = `ws://${location.hostname || "localhost"}:8765`;
$("serverUrl").value = localStorage.getItem("serverUrl") || DEFAULT_URL;

function motionAllowed() {
  return state === STATE.CONNECTED && welcomed && estopActive === false;
}

function updateButtons() {
  const connected = state === STATE.CONNECTED;
  $("connectBtn").disabled = wantConnected;
  $("disconnectBtn").disabled = !wantConnected;
  $("helloBtn").disabled = !connected;
  $("testBtn").disabled = !connected;
  const motion = motionAllowed();
  document
    .querySelectorAll(".hold, .mode, #resetSceneBtn")
    .forEach((b) => (b.disabled = !motion));
  $("estopBtn").disabled = !connected;
  $("estopResetBtn").disabled = !(connected && estopActive === true);
  const gate = $("gate");
  if (!connected) gate.textContent = "未連線";
  else if (!welcomed) gate.textContent = "等 welcome";
  else if (estopActive === null) gate.textContent = "等 state";
  else if (estopActive) gate.textContent = "急停中，按「解除急停」";
  else gate.textContent = "可操作";
  gate.className = "gate " + (motion ? "ok" : "blocked");
}

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
  if (next !== STATE.CONNECTED) {
    welcomed = false;
    estopActive = null;
    pendingAck.clear();
  }
  updateButtons();
}

const LOG_MAX = 200; // 手機端只留最近 200 則，避免高頻訊息撐爆 DOM

function addLog(dir, text) {
  const li = document.createElement("li");
  const time = new Date().toLocaleTimeString("zh-TW", { hour12: false });
  li.innerHTML =
    `<span class="dir ${dir}">${dir === "out" ? "送" : dir === "in" ? "收" : "訊"}</span>` +
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

// ===== 送出：quiet＝不寫進紀錄（keepalive 每 150 ms 一則，寫了會洗版）=====
function send(type, payload, { quiet = false } = {}) {
  if (!ws || ws.readyState !== WebSocket.OPEN) return null;
  const msg = makeEnvelope(type, payload);
  ws.send(JSON.stringify(msg));
  if (!quiet) addLog("out", JSON.stringify(msg));
  return msg.seq;
}

// ===== ack 對帳：seq → {btn, kind} =====
const pendingAck = new Map();

function sendTracked(btn, type, payload, kind = "oneshot") {
  const s = send(type, payload);
  if (s !== null) pendingAck.set(s, { btn, kind });
  return s;
}

function flash(btn, ok) {
  if (!btn) return;
  const cls = ok ? "flash-ok" : "flash-bad";
  btn.classList.remove("flash-ok", "flash-bad");
  void btn.offsetWidth; // 重新觸發 CSS 動畫
  btn.classList.add(cls);
}

function handleAck(p) {
  const entry = pendingAck.get(p?.seq);
  pendingAck.delete(p?.seq);
  if (p?.status && p.status !== "ok") {
    addLog("in", `指令被拒（seq ${p.seq}）：${p.reason || p.status}`);
  }
  if (!entry) return;
  flash(entry.btn, p.status === "ok");
  // 按住中的 start 收到最終 ack（不論成功或被拒）＝這次按住結束：
  // 例如夾爪碰到物體 server 自動停、watchdog 逾時、被拒。不重送。
  if (hold && entry.kind === "start" && p.seq === hold.startSeq) {
    endHold({
      sendStop: false,
      reason: `server 結束：${p.reason || p.status}`,
    });
  }
}

// ===== 按住控制（press-and-hold）=====
// 一次只允許一個按住；按下送 *_start，按住期間每 150 ms 送 jog_keepalive，
// 放開／取消／背景／失焦／斷線立即送 *_stop。watchdog 750 ms，keepalive 不等 ack。
const KEEPALIVE_MS = 150;
let hold = null; // {btn, stopType, startSeq, timer, pointerId}

function startHold(btn, startType, stopType, payload, pointerId) {
  if (!motionAllowed() || hold) return;
  const startSeq = sendTracked(btn, startType, payload, "start");
  if (startSeq === null) return;
  btn.classList.add("held");
  hold = { btn, stopType, startSeq, timer: null, pointerId };
  hold.timer = setInterval(() => {
    if (!hold || !ws || ws.readyState !== WebSocket.OPEN) return;
    send("jog_keepalive", {}, { quiet: true });
  }, KEEPALIVE_MS);
}

function endHold({ sendStop = true, reason = "" } = {}) {
  if (!hold) return;
  const h = hold;
  hold = null;
  clearInterval(h.timer);
  h.btn.classList.remove("held");
  if (sendStop) sendTracked(h.btn, h.stopType, {}, "stop");
  if (reason) addLog("sys", reason);
}

function bindHold(btn, startType, stopType, payloadFn) {
  btn.addEventListener("pointerdown", (e) => {
    e.preventDefault();
    if (btn.disabled) return;
    startHold(btn, startType, stopType, payloadFn(), e.pointerId);
    if (hold && hold.btn === btn) {
      try {
        btn.setPointerCapture(e.pointerId); // 手指滑出按鈕仍算按住，放開才停
      } catch {}
    }
  });
  // 只認開始按住的那根手指，第二指碰同一顆鍵不會誤停第一指
  const release = (e) => {
    if (hold && hold.btn === btn && e.pointerId === hold.pointerId) endHold();
  };
  btn.addEventListener("pointerup", release);
  btn.addEventListener("pointercancel", release);
  btn.addEventListener("pointerleave", release);
  // 捕捉被系統收回（來電、手勢、對話框）＝失去控制，立即停
  btn.addEventListener("lostpointercapture", release);
  btn.addEventListener("contextmenu", (e) => e.preventDefault());
}

// 全域兜底：同一根手指在任何地方放開或取消，都結束按住
["pointerup", "pointercancel"].forEach((t) =>
  window.addEventListener(
    t,
    (e) => {
      if (hold && e.pointerId === hold.pointerId) endHold();
    },
    true,
  ),
);

// 任何「失去控制」的情況都要停：背景、失焦、離開頁面、socket 出事
function stopEverything(reason) {
  endHold({ sendStop: true, reason });
}
document.addEventListener("visibilitychange", () => {
  if (document.hidden) stopEverything("App 進入背景，已送 stop");
});
window.addEventListener("blur", () => stopEverything("視窗失焦，已送 stop"));
window.addEventListener("pagehide", () =>
  stopEverything("離開頁面，已送 stop"),
);

// ===== WebSocket =====
function connect() {
  const url = $("serverUrl").value.trim();
  if (!url) return;
  localStorage.setItem("serverUrl", url);

  try {
    ws = new WebSocket(url);
  } catch (e) {
    addLog("sys", `連線失敗：${e.message}`);
    scheduleReconnect();
    return;
  }

  ws.onopen = () => {
    reconnectDelay = 1000;
    setState(STATE.CONNECTED);
    send("hello", { client: "phone", name: "thomas-app" });
  };

  ws.onmessage = (ev) => {
    // 協定只走 JSON 純文字；binary frame 直接標示不解析
    if (typeof ev.data !== "string") {
      addLog("in", "(忽略非文字 frame)");
      return;
    }
    let msg;
    try {
      msg = JSON.parse(ev.data);
    } catch {
      addLog("in", ev.data);
      return;
    }
    if (msg.type === "state") {
      handleState(msg.payload || {});
      return; // 每秒五則，不進紀錄，顯示在狀態列
    }
    addLog("in", ev.data);
    if (msg.type === "ack") handleAck(msg.payload);
    if (msg.type === "welcome") {
      welcomed = true;
      if (msg.payload?.config) stepConfig = msg.payload.config;
      updateModeBtn();
      updateButtons();
    }
  };

  ws.onclose = () => {
    // 斷線＝失去控制：本地清掉按住狀態（socket 已死，stop 送不出去，server 端 watchdog 會接手）
    endHold({ sendStop: false, reason: hold ? "斷線，按住已清除" : "" });
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

// ===== state 廣播：顯示在狀態列，並更新急停閘門 =====
function handleState(p) {
  const prevEstop = estopActive;
  if (typeof p.estop === "boolean") estopActive = p.estop;
  if (estopActive && hold) {
    endHold({ sendStop: true, reason: "server 回報急停，已送 stop" });
  }
  if (prevEstop !== estopActive) updateButtons();
  const pos = Array.isArray(p.ee_pos)
    ? p.ee_pos.map((v) => (v * 100).toFixed(1)).join(", ")
    : "?";
  const parts = [
    `ee(cm) ${pos}`,
    `夾爪 ${p.gripper ?? "?"}`,
    `ik ${p.ik ?? "?"}`,
    `estop ${p.estop ?? "?"}`,
  ];
  if (typeof p.task_success === "boolean")
    parts.push(p.task_success ? "任務 ✓" : "任務 …");
  if (typeof p.episode === "number") parts.push(`ep ${p.episode}`);
  $("stateBar").textContent = parts.join("　");
}

// ===== 按鈕綁定 =====
$("connectBtn").onclick = () => {
  wantConnected = true;
  setState(STATE.RECONNECTING);
  connect();
};

$("disconnectBtn").onclick = () => {
  wantConnected = false;
  clearTimeout(reconnectTimer);
  stopEverything("");
  if (ws) ws.close();
  else setState(STATE.DISCONNECTED);
};

$("helloBtn").onclick = () =>
  send("hello", { client: "phone", name: "thomas-app" });
$("testBtn").onclick = () => send("test", { msg: "hello" });
$("clearBtn").onclick = () => {
  logEl.innerHTML = "";
};

function updateModeBtn() {
  const cm = stepConfig
    ? ` ${currentStep === "coarse" ? stepConfig.coarse_cm : stepConfig.fine_cm} cm`
    : "";
  $("modeBtn").textContent = (currentStep === "coarse" ? "粗調" : "微調") + cm;
}
$("modeBtn").onclick = () => {
  currentStep = currentStep === "coarse" ? "fine" : "coarse";
  updateModeBtn();
  sendTracked($("modeBtn"), "mode", { step: currentStep });
};

// 平移六鍵：jog_start {axis, dir, step, frame:"base"}
document.querySelectorAll(".move").forEach((btn) => {
  bindHold(btn, "jog_start", "jog_stop", () => ({
    axis: btn.dataset.axis,
    dir: Number(btn.dataset.dir),
    step: currentStep,
    frame: "base",
  }));
});

// 手腕兩鍵：joint_jog_start {joint:"wrist_flex", dir, step}
document.querySelectorAll(".wrist").forEach((btn) => {
  bindHold(btn, "joint_jog_start", "joint_jog_stop", () => ({
    joint: "wrist_flex",
    dir: Number(btn.dataset.dir),
    step: currentStep,
  }));
});

// 夾爪兩鍵：gripper_jog_start {action, step}
document.querySelectorAll(".grip").forEach((btn) => {
  bindHold(btn, "gripper_jog_start", "gripper_jog_stop", () => ({
    action: btn.dataset.action,
    step: currentStep,
  }));
});

// 急停：獨立、醒目、連線中隨時可按；先本地停 keepalive 再送 estop
function fireEstop() {
  endHold({ sendStop: false });
  sendTracked($("estopBtn"), "estop", {});
}
// 按下就送，不等放開：另一指還按著方向鍵時也能立刻停
$("estopBtn").addEventListener("pointerdown", (e) => {
  e.preventDefault();
  if (!$("estopBtn").disabled) fireEstop();
});
// 鍵盤 Enter／Space 產生的 click 沒有指標（detail 0）才送；指標產生的 click 已在 pointerdown 送過
$("estopBtn").addEventListener("click", (e) => {
  if (e.detail === 0 && !$("estopBtn").disabled) fireEstop();
});
$("estopResetBtn").onclick = () =>
  sendTracked($("estopResetBtn"), "estop_reset", {});
$("resetSceneBtn").onclick = () =>
  sendTracked($("resetSceneBtn"), "reset_scene", {});

updateModeBtn();
setState(STATE.DISCONNECTED);
