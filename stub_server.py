"""本地假 server：模擬 Leo 的 relay 行為，供單機開發。

- hello 回 welcome，test 原樣 echo
- *_start：記住 seq，放開（*_stop）時先回 stop 的 ack，再回 start 的最終 ack
- jog_keepalive 不回；750 ms 沒 keepalive 就當 watchdog 逾時，回 start 的 ack
- 也支援 orient_*（姿態）與 pose_*（AR）：*_update 兼 keepalive、不回 ack；不看 control_mode
- estop／estop_reset／reset_scene／mode：ack ok，estop 會反映在 state
- gripper_set {"state": "open"|"closed"}：單次指令（非 hold），立即設定夾爪並回
  ok gripper_updated；急停中 rejected estop_active，有 hold 時 rejected busy，
  state 不合法 rejected bad_payload
- 每 200 ms 廣播 state（ee_pos 依按住方向慢慢動；pose 時是 ee0 + p_rel_m，
  另附除錯用的 orient_deg、gripper_delta）
- pose_update 的目標 ee0 + p_rel_m 逐軸截在 workspace_min_m／workspace_max_m 上，
  有截就回 ok pose_updated 且帶 clamped: true；沒截不回 ack（照舊）。
  截完的目標離這次 hold 上一個採用的目標（第一筆則是 hold 開始時的 ee）超過
  jump_threshold_m 就整筆丟掉，回 rejected jump，hold 不中斷
- python stub_server.py --config cfg.json：JSON 可覆寫 CONFIG 的任一個鍵
"""

import argparse
import asyncio
import json
import math
import time

import websockets

WATCHDOG_S = 0.75
START_TYPES = {
    "jog_start",
    "joint_jog_start",
    "gripper_jog_start",
    "orient_start",
    "pose_start",
}
STOP_OF = {
    "jog_stop": "jog",
    "joint_jog_stop": "joint_jog",
    "gripper_jog_stop": "gripper_jog",
    "orient_stop": "orient",
    "pose_stop": "pose",
}
UPDATE_OF = {"orient_update": "orient", "pose_update": "pose"}
GRIPPER_STATES = ("open", "closed")  # tuple：payload 的 state 可能是不可 hash 的值
KEEPALIVE_KINDS = {"jog", "joint_jog", "gripper_jog"}  # 由 jog_keepalive 續命的 hold

# pose 的工作空間（base frame 絕對座標，xyz）與跳點門檻。
# 暫定：真手臂的數字要等手臂規格實測後再填。x、y = stub 起始 ee [0.02, -0.14]
# （reset_scene 的位置，從這裡開始的 hold 的 ee0）± 0.25 m；z 0.05–0.60 m。
CONFIG = {
    "workspace_min_m": [-0.23, -0.39, 0.05],  # 暫定
    "workspace_max_m": [0.27, 0.11, 0.60],  # 暫定
    "jump_threshold_m": 0.060,  # 預設照 Phone2Act
}


def now_ms():
    return int(time.time() * 1000)


def rotation_deg(q):
    """單位四元數 [x, y, z, w] 的旋轉角（度，小數一位）。"""
    return round(math.degrees(2 * math.acos(min(1.0, abs(q[3])))), 1)


def envelope(type_, seq, payload):
    return json.dumps(
        {"v": 1, "type": type_, "ts": now_ms(), "seq": seq, "payload": payload}
    )


# 模擬機器人狀態跨連線保留：控制中斷線觸發的急停，重連後仍在，要先 estop_reset
SIM = {"ee": [0.02, -0.14, 0.16], "gripper": "open", "step": "fine", "estop": False}
# 進行中的按住也跨連線共用：任一連線送 estop 都要停掉它，ack 回給發起的那條連線
ACTIVE = {"cmd": None}  # cmd = {"ws", "seq", "kind", "payload", "last_keepalive"}
# orient／pose 另有 "update"（最近一次採用的 *_update payload）；pose 再加 "ee0" 與
# "target"（最近一次採用、截過的目標；hold 開始時 = ee0）


def pose_target(ee0, p_rel):
    """ee0 + p_rel 逐軸截進工作空間，回傳 (目標, 有沒有截)。"""
    raw = [e + v for e, v in zip(ee0, p_rel)]  # 不做座標對齊
    lo, hi = CONFIG["workspace_min_m"], CONFIG["workspace_max_m"]
    target = [min(max(v, l), h) for v, l, h in zip(raw, lo, hi)]
    return target, target != raw


async def send_ack(ws, seq, status, reason, **extra):
    try:
        await ws.send(
            envelope(
                "ack", seq, {"seq": seq, "status": status, "reason": reason, **extra}
            )
        )
    except websockets.ConnectionClosed:
        pass


async def finish_active(reason):
    a, ACTIVE["cmd"] = ACTIVE["cmd"], None
    if a:
        await send_ack(a["ws"], a["seq"], "ok", reason)


async def handle(ws):
    sim = SIM

    def active():
        return ACTIVE["cmd"]

    def mine():
        a = ACTIVE["cmd"]
        return a if a and a["ws"] is ws else None

    async def ack(seq, status, reason):
        await send_ack(ws, seq, status, reason)

    async def watchdog():
        while True:
            await asyncio.sleep(0.05)
            a = mine()
            if a and time.monotonic() - a["last_keepalive"] > WATCHDOG_S:
                await finish_active(f"{a['kind']}_watchdog_timeout")

    async def broadcaster():
        while True:
            await asyncio.sleep(0.2)
            a = mine()
            if a and sim["estop"]:
                await finish_active("estopped")  # 急停中絕不移動
            elif a and a["kind"] == "jog":
                p = a["payload"]
                i = "xyz".index(p.get("axis", "x"))
                sim["ee"][i] += (
                    0.2 * (0.09 if p.get("step") == "fine" else 0.18) * p.get("dir", 1)
                )
            elif a and a["kind"] == "gripper_jog":
                sim["gripper"] = a["payload"].get("action", "open")
            elif a and a["kind"] == "pose":
                sim["ee"] = list(a["target"])
            state = {
                "ee_pos": sim["ee"],
                "gripper": sim["gripper"],
                "step": sim["step"],
                "ik": "moving" if active() else "ok",
                "estop": sim["estop"],
                "task_success": False,
                "episode": 0,
            }
            a = active()
            if a and a["kind"] in UPDATE_OF.values():  # 除錯欄位，不屬正式 contract
                state["orient_deg"] = rotation_deg(
                    a["update"].get("q_rel", [0.0, 0.0, 0.0, 1.0])
                )
                if a["kind"] == "pose":
                    state["gripper_delta"] = a["update"].get("gripper_delta", 0.0)
            await ws.send(envelope("state", 0, state))

    tasks = [asyncio.create_task(broadcaster()), asyncio.create_task(watchdog())]
    try:
        async for raw in ws:
            msg = json.loads(raw)
            t, seq, p = msg.get("type"), msg.get("seq", 0), msg.get("payload", {})
            if t == "hello":
                await ws.send(
                    envelope(
                        "welcome",
                        seq,
                        {"session": "stub", "config": {"coarse_cm": 2, "fine_cm": 0.5}},
                    )
                )
            elif t == "test":
                await ws.send(raw)
            elif t == "jog_keepalive" or t in UPDATE_OF:
                renews = KEEPALIVE_KINDS if t == "jog_keepalive" else {UPDATE_OF[t]}
                a = mine()
                if a and a["kind"] not in renews:
                    a = None  # orient／pose 只認自己的 *_update，jog_keepalive 不幫它們續命
                if a and time.monotonic() - a["last_keepalive"] > WATCHDOG_S:
                    await finish_active(f"{a['kind']}_watchdog_timeout")  # 晚到的 keepalive 不能續命
                elif a:
                    a["last_keepalive"] = time.monotonic()  # 被丟掉的跳點也算續命
                    if t == "pose_update":
                        target, clamped = pose_target(
                            a["ee0"], p.get("p_rel_m", [0.0, 0.0, 0.0])
                        )
                        # 比截過的目標：手臂每次採用的移動量一定不超過門檻
                        if math.dist(target, a["target"]) > CONFIG["jump_threshold_m"]:
                            await ack(seq, "rejected", "jump")
                        else:
                            a["update"], a["target"] = p, target
                            if clamped:
                                await send_ack(ws, seq, "ok", "pose_updated", clamped=True)
                    elif t in UPDATE_OF:
                        a["update"] = p
            elif t in START_TYPES:
                if sim["estop"]:
                    await ack(seq, "rejected", "estop_active")
                elif active():
                    await ack(seq, "rejected", "busy")
                else:
                    cmd = {
                        "ws": ws,
                        "seq": seq,
                        "kind": t[:-6],
                        "payload": p,
                        "last_keepalive": time.monotonic(),
                    }
                    if cmd["kind"] in UPDATE_OF.values():
                        cmd["update"] = {}
                    if cmd["kind"] == "pose":
                        cmd["ee0"] = list(sim["ee"])  # control_mode 不處理
                        cmd["target"] = list(sim["ee"])
                    ACTIVE["cmd"] = cmd
            elif t in STOP_OF:
                await ack(seq, "ok", f"{STOP_OF[t]}_stop_requested")
                if mine():
                    await finish_active(f"{STOP_OF[t]}_stopped")
            elif t == "estop":
                sim["estop"] = True
                await finish_active("estopped")
                await ack(seq, "ok", "estop_engaged")
            elif t == "estop_reset":
                sim["estop"] = False
                await ack(seq, "ok", "estop_reset")
            elif t == "reset_scene":
                sim["ee"] = [0.02, -0.14, 0.16]
                await ack(seq, "ok", "scene_reset")
            elif t == "mode":
                sim["step"] = p.get("step", "fine")
                await ack(seq, "ok", "mode_updated")
            elif t == "gripper_set":
                if sim["estop"]:
                    await ack(seq, "rejected", "estop_active")
                elif active():
                    await ack(seq, "rejected", "busy")  # 不動進行中的 hold
                elif not isinstance(p, dict) or p.get("state") not in GRIPPER_STATES:
                    await ack(seq, "rejected", "bad_payload")
                else:
                    sim["gripper"] = p["state"]
                    await ack(seq, "ok", "gripper_updated")
            elif t in {"move_delta", "gripper"}:  # 舊的一次一步，向下相容
                await ack(seq, "ok", "completed")
            else:
                await ws.send(
                    envelope("error", seq, {"code": "unknown_type", "msg": t})
                )
    finally:
        for task in tasks:
            task.cancel()
        if mine():
            sim["estop"] = True  # 對照 Leo：控制中斷線就急停
            ACTIVE["cmd"] = None


def load_config(path):
    with open(path, encoding="utf-8") as f:
        cfg = json.load(f)
    if not isinstance(cfg, dict) or set(cfg) - set(CONFIG):
        raise SystemExit(f"config: 只接受 {sorted(CONFIG)} 這些鍵：{cfg!r}")
    CONFIG.update(cfg)
    lo, hi = CONFIG["workspace_min_m"], CONFIG["workspace_max_m"]
    for name in ("workspace_min_m", "workspace_max_m"):
        v = CONFIG[name]
        if not (isinstance(v, list) and len(v) == 3 and all(type(x) in (int, float) for x in v)):
            raise SystemExit(f"config: {name} 要是 3 個數字 [x, y, z]：{v!r}")
    if any(l > h for l, h in zip(lo, hi)):
        raise SystemExit(f"config: workspace_min_m {lo} 有軸大於 workspace_max_m {hi}")
    j = CONFIG["jump_threshold_m"]
    if type(j) not in (int, float) or j <= 0:
        raise SystemExit(f"config: jump_threshold_m 要是正數：{j!r}")


async def main():
    async with websockets.serve(handle, "0.0.0.0", 8765):
        print("stub on ws://0.0.0.0:8765")
        print(f"pose config: {json.dumps(CONFIG)}")
        await asyncio.Future()


ap = argparse.ArgumentParser()
ap.add_argument("--config", help="JSON：workspace_min_m、workspace_max_m、jump_threshold_m")
args = ap.parse_args()
if args.config:
    load_config(args.config)
asyncio.run(main())
