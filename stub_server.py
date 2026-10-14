"""本地假 server：模擬 Leo 的 relay 行為，供單機開發。

- hello 回 welcome，test 原樣 echo
- *_start：記住 seq，放開（*_stop）時先回 stop 的 ack，再回 start 的最終 ack
- jog_keepalive 不回；750 ms 沒 keepalive 就當 watchdog 逾時，回 start 的 ack
- orient_start／orient_update／orient_stop：姿態模式，orient_update 同時當 keepalive；
  state 多帶 orient_deg（目前 q_rel 的轉角，度），方便看手機轉動有沒有送到
- pose_start／pose_update／pose_stop：AR 模式，pose_update 同時當 keepalive；
  ee_pos 直接加上 p_rel_m（不做座標對齊，只為了看得出有在動），state 也帶 orient_deg 與 gripper_delta
- estop／estop_reset／reset_scene／mode：ack ok，estop 會反映在 state
- 每 200 ms 廣播 state（ee_pos 依按住方向慢慢動）
"""

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
KEEPALIVE_TYPES = {"jog_keepalive", "orient_update", "pose_update"}


def now_ms():
    return int(time.time() * 1000)


def envelope(type_, seq, payload):
    return json.dumps(
        {"v": 1, "type": type_, "ts": now_ms(), "seq": seq, "payload": payload}
    )


# 模擬機器人狀態跨連線保留：控制中斷線觸發的急停，重連後仍在，要先 estop_reset
SIM = {
    "ee": [0.02, -0.14, 0.16],
    "gripper": "open",
    "step": "fine",
    "estop": False,
    "orient_deg": 0.0,
    "gripper_delta": 0.0,
}
# 進行中的按住也跨連線共用：任一連線送 estop 都要停掉它，ack 回給發起的那條連線
ACTIVE = {"cmd": None}  # cmd = {"ws", "seq", "kind", "payload", "last_keepalive"}


async def send_ack(ws, seq, status, reason):
    try:
        await ws.send(
            envelope("ack", seq, {"seq": seq, "status": status, "reason": reason})
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
            elif a and a["kind"] in ("orient", "pose"):
                q = a.get("q_rel") or [0, 0, 0, 1]
                sim["orient_deg"] = round(
                    math.degrees(2 * math.atan2(math.hypot(*q[:3]), abs(q[3]))), 1
                )
                if a["kind"] == "pose":
                    p_rel = a.get("p_rel_m") or [0, 0, 0]
                    sim["ee"] = [e0 + d for e0, d in zip(a["ee0"], p_rel)]
                    sim["gripper_delta"] = a.get("gripper_delta", 0.0)
            await ws.send(
                envelope(
                    "state",
                    0,
                    {
                        "ee_pos": sim["ee"],
                        "gripper": sim["gripper"],
                        "step": sim["step"],
                        "ik": "moving" if active() else "ok",
                        "estop": sim["estop"],
                        "orient_deg": sim["orient_deg"],
                        "gripper_delta": sim["gripper_delta"],
                        "task_success": False,
                        "episode": 0,
                    },
                )
            )

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
            elif t in KEEPALIVE_TYPES:
                a = mine()
                if a and time.monotonic() - a["last_keepalive"] > WATCHDOG_S:
                    await finish_active(f"{a['kind']}_watchdog_timeout")  # 晚到的 keepalive 不能續命
                elif a:
                    a["last_keepalive"] = time.monotonic()
                    if t == "orient_update" and a["kind"] == "orient":
                        a["q_rel"] = p.get("q_rel")
                    elif t == "pose_update" and a["kind"] == "pose":
                        a["q_rel"] = p.get("q_rel")
                        a["p_rel_m"] = p.get("p_rel_m")
                        a["gripper_delta"] = p.get("gripper_delta", 0.0)
            elif t in START_TYPES:
                if sim["estop"]:
                    await ack(seq, "rejected", "estop_active")
                elif active():
                    await ack(seq, "rejected", "busy")
                else:
                    ACTIVE["cmd"] = {
                        "ws": ws,
                        "seq": seq,
                        "kind": t[:-6],
                        "payload": p,
                        "last_keepalive": time.monotonic(),
                        "ee0": list(sim["ee"]),
                    }
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


async def main():
    async with websockets.serve(handle, "0.0.0.0", 8765):
        print("stub on ws://0.0.0.0:8765")
        await asyncio.Future()


asyncio.run(main())
