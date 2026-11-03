"""stub_server.py 的 orient_*／pose_* ack 驗收（含 jog 系列回歸）。

每個情境重啟一次 stub（ws 8765 寫死），用 websockets 走真的 wire。
任一情境失敗 exit 1。

    python tests/stub_hold_ack_test.py                 # 全部情境
    python tests/stub_hold_ack_test.py --case 2 4      # 指定情境
    python tests/stub_hold_ack_test.py --stub /path/to/stub_server.py
"""

import argparse
import asyncio
import json
import math
import socket
import subprocess
import sys
import time
from pathlib import Path

import websockets

ROOT = Path(__file__).resolve().parent.parent
URL = "ws://127.0.0.1:8765"
UPDATE_S = 0.033
TOL = 1e-6


class CheckFailed(Exception):
    pass


def check(cond, msg):
    if not cond:
        raise CheckFailed(msg)


def port_in_use(port):
    with socket.socket() as s:
        return s.connect_ex(("127.0.0.1", port)) == 0


class Client:
    def __init__(self, ws):
        self.ws = ws
        self.seq = 0
        self.inbox = []  # (monotonic, msg)
        self.reader = asyncio.create_task(self._read())

    async def _read(self):
        try:
            async for raw in self.ws:
                self.inbox.append((time.monotonic(), json.loads(raw)))
        except websockets.ConnectionClosed:
            pass

    async def send(self, type_, payload=None):
        self.seq += 1
        await self.ws.send(
            json.dumps(
                {
                    "v": 1,
                    "type": type_,
                    "ts": int(time.time() * 1000),
                    "seq": self.seq,
                    "payload": payload or {},
                }
            )
        )
        return self.seq

    async def wait_for(self, pred, timeout, what):
        deadline = time.monotonic() + timeout
        start = len(self.inbox)
        while time.monotonic() < deadline:
            for _, m in self.inbox[start:]:
                if pred(m):
                    return m
            await asyncio.sleep(0.01)
        raise CheckFailed(f"timeout waiting for {what}")

    def mark(self):
        return len(self.inbox)

    def since(self, mark, type_=None):
        return [m for _, m in self.inbox[mark:] if type_ is None or m["type"] == type_]

    def errors(self):
        errs = [m["payload"] for _, m in self.inbox if m["type"] == "error"]
        return f"{len(errs)} error(s), first: {errs[:2]}" if errs else None

    async def close(self):
        await self.ws.close()
        self.reader.cancel()


def is_ack(seq, reason=None, status=None):
    def pred(m):
        if m["type"] != "ack" or m["payload"]["seq"] != seq:
            return False
        if reason is not None and m["payload"]["reason"] != reason:
            return False
        return status is None or m["payload"]["status"] == status

    return pred


def acks(msgs):
    return [
        (m["payload"]["seq"], m["payload"]["status"], m["payload"]["reason"])
        for m in msgs
        if m["type"] == "ack"
    ]


async def connect():
    c = Client(await websockets.connect(URL))
    await c.send("hello", {"client": "phone", "name": "stub-hold-ack-test"})
    await c.wait_for(lambda m: m["type"] == "welcome", 2, "welcome")
    st = await c.wait_for(lambda m: m["type"] == "state", 2, "first state")
    check(st["payload"]["estop"] is False, f"stub starts in estop: {st}")
    return c


async def fresh_state(c):
    """等一則新的 state（呼叫之後才送出的）。"""
    return (await c.wait_for(lambda m: m["type"] == "state", 1, "state"))["payload"]


async def stream(c, type_, payload_of, n=None, until=None):
    """每 33 ms 送一則 *_update，送 n 則或直到 until 被 set；回傳送出的 seq。"""
    seqs = []
    i = 0
    while (n is None or i < n) and not (until and until.is_set()):
        seqs.append(await c.send(type_, payload_of(i)))
        i += 1
        await asyncio.sleep(UPDATE_S)
    return seqs


def assert_release(c, mark, kind, start_seq, stop_seq, update_seqs=()):
    got = acks(c.since(mark))
    want = [
        (stop_seq, "ok", f"{kind}_stop_requested"),
        (start_seq, "ok", f"{kind}_stopped"),
    ]
    check(got == want, f"{kind} release acks {got} != {want}")
    acked = {s for s, _, _ in got}
    check(not acked & set(update_seqs), f"updates were acked: {acked & set(update_seqs)}")


def assert_no_debug_fields(state, when):
    for k in ("orient_deg", "gripper_delta"):
        check(k not in state, f"{k} present in state {when}: {state}")


def close_to(a, b):
    return len(a) == len(b) and all(abs(x - y) <= TOL for x, y in zip(a, b))


def deg_of(q):
    return round(math.degrees(2 * math.acos(min(1.0, abs(q[3])))), 1)


# ---- 情境 ----------------------------------------------------------------


async def case_1_orient(c):
    """orient_start → 3 則 orient_update（33 ms）→ orient_stop。"""
    q = [0.1672, 0.0, 0.0, 0.98592]
    idle = await fresh_state(c)
    assert_no_debug_fields(idle, "before hold")
    mark = c.mark()
    s_start = await c.send("orient_start", {"frame": "phone_body", "rate_hz": 30,
                                            "gain": 0.7, "deadband_deg": 2.5,
                                            "smoothing": 0.3})
    ups = await stream(c, "orient_update",
                       lambda i: {"q_rel": [0, 0, 0, 1] if i == 0 else q}, n=3)
    held = (await c.wait_for(
        lambda m: m["type"] == "state" and "orient_deg" in m["payload"], 1,
        "state with orient_deg"))["payload"]
    check(held["orient_deg"] == deg_of(q), f"orient_deg {held['orient_deg']} != {deg_of(q)}")
    check(held["ik"] == "moving", f"ik not moving during hold: {held}")
    check("gripper_delta" not in held, f"gripper_delta during orient hold: {held}")
    s_stop = await c.send("orient_stop")
    await c.wait_for(is_ack(s_start), 1, "orient start final ack")
    assert_release(c, mark, "orient", s_start, s_stop, ups)
    after = await fresh_state(c)
    assert_no_debug_fields(after, "after hold")
    check(after["ik"] == "ok", f"ik after hold: {after}")
    check(not c.errors(), f"error messages: {c.errors()}")
    return f"acks stop#{s_stop} orient_stop_requested, start#{s_start} orient_stopped; orient_deg={held['orient_deg']}"


async def pose_roundtrip(c, control_mode):
    P = [0.031, -0.052, 0.0735]
    G = 0.4
    q = [0.0, 0.0, 0.0, 1.0] if control_mode == "translation" else [0.13, 0.0, 0.0, math.sqrt(1 - 0.13 ** 2)]
    ee0 = (await fresh_state(c))["ee_pos"]
    mark = c.mark()
    s_start = await c.send("pose_start", {"frame": "webxr_local", "rate_hz": 30,
                                          "control_mode": control_mode, "gain": 0.7,
                                          "deadband_deg": 2.5, "smoothing": 0.3})
    stop_streaming = asyncio.Event()

    def payload(i):
        if i == 0:
            return {"p_rel_m": [0, 0, 0], "q_rel": [0, 0, 0, 1], "gripper_delta": 0}
        return {"p_rel_m": P, "q_rel": q, "gripper_delta": G}

    streamer = asyncio.create_task(stream(c, "pose_update", payload, until=stop_streaming))
    await asyncio.sleep(0.7)  # 至少 3 則 state 廣播
    stop_streaming.set()
    ups = await streamer
    s_stop = await c.send("pose_stop")
    await c.wait_for(is_ack(s_start), 1, "pose start final ack")
    assert_release(c, mark, "pose", s_start, s_stop, ups)

    expected = [e + d for e, d in zip(ee0, P)]
    hold_states = [m["payload"] for m in c.since(mark, "state") if m["payload"]["ik"] == "moving"]
    check(hold_states, "no state during pose hold")
    for st in hold_states:
        check(close_to(st["ee_pos"], ee0) or close_to(st["ee_pos"], expected),
              f"ee_pos {st['ee_pos']} is neither ee0 {ee0} nor ee0+p_rel_m {expected}")
        check("orient_deg" in st and "gripper_delta" in st, f"debug fields missing: {st}")
    final = [st for st in hold_states if close_to(st["ee_pos"], expected)]
    check(final, f"no hold state with ee_pos == ee0+p_rel_m {expected}: {hold_states}")
    check(all(st["gripper_delta"] == G for st in final), f"gripper_delta != {G}: {final}")
    check(all(st["orient_deg"] == deg_of(q) for st in final), f"orient_deg != {deg_of(q)}: {final}")
    after = await fresh_state(c)
    assert_no_debug_fields(after, "after hold")
    check(not c.errors(), f"error messages: {c.errors()}")
    err = max(abs(a - b) for a, b in zip(final[-1]["ee_pos"], expected))
    return (f"acks stop#{s_stop} pose_stop_requested, start#{s_start} pose_stopped; "
            f"ee0={ee0} ee_pos={final[-1]['ee_pos']} max_err={err:.2e} "
            f"orient_deg={final[-1]['orient_deg']} gripper_delta={final[-1]['gripper_delta']}")


async def case_2_pose_translation(c):
    """pose_start(control_mode=translation)：ee_pos == ee0 + p_rel_m。"""
    return await pose_roundtrip(c, "translation")


async def case_3_pose_wrist(c):
    """pose_start(control_mode=wrist)：結果同情境 2（stub 不分模式）。"""
    return await pose_roundtrip(c, "wrist")


async def case_4_pose_watchdog(c):
    """pose_start 後 >1 s 不送 update（只送 jog_keepalive，不得續命）→ pose_watchdog_timeout。"""
    mark = c.mark()
    t0 = time.monotonic()
    s_start = await c.send("pose_start", {"frame": "webxr_local", "control_mode": "translation"})
    ka = []
    while time.monotonic() - t0 < 1.2:
        ka.append(await c.send("jog_keepalive"))
        await asyncio.sleep(0.15)
    got = acks(c.since(mark))
    want = [(s_start, "ok", "pose_watchdog_timeout")]
    check(got == want, f"acks {got} != {want}")
    t_ack = next(t for t, m in c.inbox[mark:] if m["type"] == "ack")
    dt = t_ack - t0
    check(0.75 <= dt <= 1.0, f"watchdog fired after {dt:.3f}s, expected 0.75–1.0s")
    after = await fresh_state(c)
    assert_no_debug_fields(after, "after timeout")
    check(after["estop"] is False, "watchdog timeout must not engage estop")
    check(not c.errors(), f"error messages: {c.errors()}")
    return f"start#{s_start} pose_watchdog_timeout after {dt:.3f}s ({len(ka)} jog_keepalive ignored)"


async def case_5_estop(c):
    """hold 中 estop → start 收 estopped；之後 pose_start → rejected estop_active。"""
    mark = c.mark()
    s_start = await c.send("pose_start", {"frame": "webxr_local", "control_mode": "translation"})
    await stream(c, "pose_update",
                 lambda i: {"p_rel_m": [0, 0, 0.01], "q_rel": [0, 0, 0, 1], "gripper_delta": 0}, n=5)
    s_estop = await c.send("estop")
    await c.wait_for(is_ack(s_estop), 1, "estop ack")
    await asyncio.sleep(0.1)
    got = acks(c.since(mark))
    want = [(s_start, "ok", "estopped"), (s_estop, "ok", "estop_engaged")]
    check(got == want, f"acks {got} != {want}")
    st = await fresh_state(c)
    check(st["estop"] is True and st["ik"] == "ok", f"state after estop: {st}")
    assert_no_debug_fields(st, "after estop")
    mark2 = c.mark()
    s_again = await c.send("pose_start", {"frame": "webxr_local", "control_mode": "translation"})
    await c.wait_for(is_ack(s_again), 1, "second pose_start ack")
    got2 = acks(c.since(mark2))
    check(got2 == [(s_again, "rejected", "estop_active")], f"second pose_start acks {got2}")
    s_reset = await c.send("estop_reset")
    await c.wait_for(is_ack(s_reset, "estop_reset"), 1, "estop_reset ack")
    check(not c.errors(), f"error messages: {c.errors()}")
    return f"start#{s_start} estopped, estop#{s_estop} estop_engaged, pose_start#{s_again} rejected estop_active"


async def case_6_busy(c):
    """hold 中再送 orient_start → rejected busy；原 hold 不受影響。"""
    mark = c.mark()
    s_pose = await c.send("pose_start", {"frame": "webxr_local", "control_mode": "translation"})
    await stream(c, "pose_update",
                 lambda i: {"p_rel_m": [0, 0, 0], "q_rel": [0, 0, 0, 1], "gripper_delta": 0}, n=3)
    s_orient = await c.send("orient_start", {"frame": "phone_body"})
    await c.wait_for(is_ack(s_orient), 1, "orient_start ack")
    ups = await stream(c, "pose_update",
                       lambda i: {"p_rel_m": [0, 0, 0], "q_rel": [0, 0, 0, 1], "gripper_delta": 0}, n=3)
    s_stop = await c.send("pose_stop")
    await c.wait_for(is_ack(s_pose), 1, "pose final ack")
    got = acks(c.since(mark))
    want = [
        (s_orient, "rejected", "busy"),
        (s_stop, "ok", "pose_stop_requested"),
        (s_pose, "ok", "pose_stopped"),
    ]
    check(got == want, f"acks {got} != {want}")
    check(not c.errors(), f"error messages: {c.errors()}")
    return f"orient_start#{s_orient} rejected busy; pose hold kept and released normally"


async def jog_roundtrip(c, kind, payload):
    mark = c.mark()
    s_start = await c.send(f"{kind}_start", payload)
    for _ in range(3):
        await asyncio.sleep(0.15)
        await c.send("jog_keepalive")
    s_stop = await c.send(f"{kind}_stop")
    await c.wait_for(is_ack(s_start), 1, f"{kind} final ack")
    assert_release(c, mark, kind, s_start, s_stop)
    return acks(c.since(mark))


async def case_7_regression(c):
    """jog／joint_jog／gripper_jog 原本的 ack 序列（含 jog watchdog、orient_update 不幫 jog 續命）。"""
    out = []
    for kind, payload in [
        ("jog", {"axis": "z", "dir": 1, "step": "fine", "frame": "base"}),
        ("joint_jog", {"joint": "wrist_flex", "dir": 1, "step": "fine"}),
        ("gripper_jog", {"action": "close", "step": "fine"}),
    ]:
        got = await jog_roundtrip(c, kind, payload)
        out.append(f"{kind}: {[r for _, _, r in got]}")

    mark = c.mark()
    t0 = time.monotonic()
    s_jog = await c.send("jog_start", {"axis": "x", "dir": 1, "step": "fine", "frame": "base"})
    while time.monotonic() - t0 < 1.1:
        await c.send("orient_update", {"q_rel": [0, 0, 0, 1]})
        await c.send("pose_update", {"p_rel_m": [0, 0, 0], "q_rel": [0, 0, 0, 1], "gripper_delta": 0})
        await asyncio.sleep(UPDATE_S)
    got = acks(c.since(mark))
    check(got == [(s_jog, "ok", "jog_watchdog_timeout")], f"jog watchdog acks {got}")
    out.append("jog watchdog: ['jog_watchdog_timeout'] (orient/pose_update ignored)")

    s_busy0 = await c.send("gripper_jog_start", {"action": "open", "step": "fine"})
    s_busy1 = await c.send("jog_start", {"axis": "y", "dir": -1, "step": "fine", "frame": "base"})
    await c.wait_for(is_ack(s_busy1), 1, "busy ack")
    s_busy_stop = await c.send("gripper_jog_stop")
    await c.wait_for(is_ack(s_busy0), 1, "gripper_jog final ack")
    got = acks(c.since(mark))[1:]
    want = [
        (s_busy1, "rejected", "busy"),
        (s_busy_stop, "ok", "gripper_jog_stop_requested"),
        (s_busy0, "ok", "gripper_jog_stopped"),
    ]
    check(got == want, f"busy acks {got} != {want}")
    out.append("busy: ['busy', 'gripper_jog_stop_requested', 'gripper_jog_stopped']")
    check(not c.errors(), f"error messages: {c.errors()}")
    return "; ".join(out)


CASES = {
    1: case_1_orient,
    2: case_2_pose_translation,
    3: case_3_pose_wrist,
    4: case_4_pose_watchdog,
    5: case_5_estop,
    6: case_6_busy,
    7: case_7_regression,
}


# ---- runner --------------------------------------------------------------


def start_stub(stub):
    proc = subprocess.Popen(
        [sys.executable, str(stub)],
        cwd=str(stub.parent),
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
    )
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        if proc.poll() is not None:
            raise RuntimeError(f"stub exited: {proc.stderr.read().decode()}")
        if port_in_use(8765):
            return proc
        time.sleep(0.05)
    proc.kill()
    raise RuntimeError("stub did not open ws 8765")


def stop_stub(proc):
    proc.terminate()
    try:
        proc.wait(3)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait()
    deadline = time.monotonic() + 3
    while port_in_use(8765) and time.monotonic() < deadline:
        time.sleep(0.05)


async def run_case(fn):
    c = await connect()
    try:
        return await asyncio.wait_for(fn(c), 15)
    finally:
        await c.close()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--case", type=int, nargs="*", choices=sorted(CASES))
    ap.add_argument("--stub", type=Path, default=ROOT / "stub_server.py")
    args = ap.parse_args()
    if port_in_use(8765):
        print("FAIL: ws 8765 already in use; stop the other stub first")
        return 2

    failed = 0
    for n in args.case or sorted(CASES):
        fn = CASES[n]
        title = fn.__doc__.strip().splitlines()[0]
        proc = start_stub(args.stub.resolve())
        try:
            detail = asyncio.run(run_case(fn))
            print(f"PASS case {n}: {title}\n    {detail}")
        except Exception as e:  # noqa: BLE001 — 任何例外都算這個情境失敗
            failed += 1
            print(f"FAIL case {n}: {title}\n    {type(e).__name__}: {e}")
        finally:
            stop_stub(proc)
    total = len(args.case or CASES)
    print(f"\n{total - failed} passed, {failed} failed")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
