"""stub_server.py 的 gripper_set（單次夾爪開合）驗收。

每個情境重啟一次 stub（ws 8765 寫死），用 websockets 走真的 wire。
hold 系列的回歸在 stub_hold_ack_test.py。任一情境失敗 exit 1。

    python tests/stub_gripper_set_test.py              # 全部情境
    python tests/stub_gripper_set_test.py --case 2     # 指定情境
"""

import argparse
import asyncio
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from stub_hold_ack_test import (  # noqa: E402
    ROOT,
    acks,
    assert_release,
    check,
    fresh_state,
    is_ack,
    port_in_use,
    run_case,
    start_stub,
    stop_stub,
    stream,
)


async def gripper_set(c, state):
    mark = c.mark()
    s = await c.send("gripper_set", {"state": state})
    await c.wait_for(is_ack(s), 1, f"gripper_set {state} ack")
    return s, acks(c.since(mark))


async def settled_state(c):
    """跳過送出前就排隊的那則 state，拿一則確定在指令處理之後的。"""
    await fresh_state(c)
    return await fresh_state(c)


# ---- 情境 ----------------------------------------------------------------


async def case_1_set_closed_then_open(c):
    """gripper_set closed → open：各回一則 ok gripper_updated，state.gripper 跟著變。"""
    before = await fresh_state(c)
    check(before["gripper"] == "open", f"stub should start open: {before}")
    mark = c.mark()
    seqs, out = [], []
    for target in ("closed", "open"):
        s, got = await gripper_set(c, target)
        check(got == [(s, "ok", "gripper_updated")], f"gripper_set {target} acks {got}")
        st = await settled_state(c)
        check(st["gripper"] == target, f"state.gripper {st['gripper']} != {target}")
        check(st["ik"] == "ok", f"gripper_set must not start a hold: {st}")
        seqs.append(s)
        out.append(f"#{s} {target} -> state.gripper={st['gripper']}")
    await asyncio.sleep(1.0)  # 超過 watchdog：單次指令不得再冒出任何 ack
    got = acks(c.since(mark))
    want = [(s, "ok", "gripper_updated") for s in seqs]
    check(got == want, f"acks over 1 s after one-shots {got} != {want}")
    check(not c.errors(), f"error messages: {c.errors()}")
    return "; ".join(out)


async def case_2_busy_during_pose(c):
    """pose hold 中 gripper_set → rejected busy；hold 照常續命、照常釋放。"""
    g0 = (await fresh_state(c))["gripper"]
    mark = c.mark()
    s_pose = await c.send("pose_start", {"frame": "webxr_local", "control_mode": "translation"})
    payload = lambda i: {"p_rel_m": [0, 0, 0.01], "q_rel": [0, 0, 0, 1], "gripper_delta": 0}  # noqa: E731
    await stream(c, "pose_update", payload, n=3)
    s_set = await c.send("gripper_set", {"state": "closed"})
    await c.wait_for(is_ack(s_set), 1, "gripper_set ack")
    ups = await stream(c, "pose_update", payload, n=30)  # ~1 s，跨過 watchdog
    st = await fresh_state(c)
    check(st["ik"] == "moving", f"pose hold should still be active: {st}")
    check(st["gripper"] == g0, f"gripper changed during busy reject: {st['gripper']} != {g0}")
    mark_rel = c.mark()
    s_stop = await c.send("pose_stop")
    await c.wait_for(is_ack(s_pose), 1, "pose final ack")
    assert_release(c, mark_rel, "pose", s_pose, s_stop, ups)
    got = acks(c.since(mark))
    want = [
        (s_set, "rejected", "busy"),
        (s_stop, "ok", "pose_stop_requested"),
        (s_pose, "ok", "pose_stopped"),
    ]
    check(got == want, f"acks {got} != {want}")
    after = await fresh_state(c)
    check(after["gripper"] == g0 and after["ik"] == "ok", f"state after release: {after}")
    check(not c.errors(), f"error messages: {c.errors()}")
    return f"gripper_set#{s_set} rejected busy; pose#{s_pose} kept ~1 s, released stop_requested→stopped"


async def case_3_busy_other_holds(c):
    """jog／joint_jog／gripper_jog／orient hold 中 gripper_set 一樣 rejected busy。"""
    out = []
    for kind, payload, update in [
        ("jog", {"axis": "z", "dir": 1, "step": "fine", "frame": "base"}, "jog_keepalive"),
        ("joint_jog", {"joint": "wrist_flex", "dir": 1, "step": "fine"}, "jog_keepalive"),
        ("gripper_jog", {"action": "open", "step": "fine"}, "jog_keepalive"),
        ("orient", {"frame": "phone_body"}, "orient_update"),
    ]:
        mark = c.mark()
        s_start = await c.send(f"{kind}_start", payload)
        await c.send(update, {"q_rel": [0, 0, 0, 1]} if update == "orient_update" else {})
        s_set = await c.send("gripper_set", {"state": "closed"})
        await c.wait_for(is_ack(s_set), 1, "gripper_set ack")
        await c.send(update, {"q_rel": [0, 0, 0, 1]} if update == "orient_update" else {})
        s_stop = await c.send(f"{kind}_stop")
        await c.wait_for(is_ack(s_start), 1, f"{kind} final ack")
        got = acks(c.since(mark))
        want = [
            (s_set, "rejected", "busy"),
            (s_stop, "ok", f"{kind}_stop_requested"),
            (s_start, "ok", f"{kind}_stopped"),
        ]
        check(got == want, f"{kind}: acks {got} != {want}")
        out.append(kind)
    check(not c.errors(), f"error messages: {c.errors()}")
    return f"busy during {', '.join(out)}; each hold released normally"


async def case_4_estop(c):
    """estop 中 gripper_set → rejected estop_active，夾爪不動；estop_reset 後可再設。"""
    g0 = (await fresh_state(c))["gripper"]
    s_estop = await c.send("estop")
    await c.wait_for(is_ack(s_estop, "estop_engaged"), 1, "estop ack")
    s, got = await gripper_set(c, "closed")
    check(got == [(s, "rejected", "estop_active")], f"gripper_set under estop acks {got}")
    st = await settled_state(c)
    check(st["estop"] is True and st["gripper"] == g0, f"state under estop: {st}")
    s_reset = await c.send("estop_reset")
    await c.wait_for(is_ack(s_reset, "estop_reset"), 1, "estop_reset ack")
    s2, got2 = await gripper_set(c, "closed")
    check(got2 == [(s2, "ok", "gripper_updated")], f"gripper_set after reset acks {got2}")
    st2 = await settled_state(c)
    check(st2["gripper"] == "closed", f"state after reset+set: {st2}")
    check(not c.errors(), f"error messages: {c.errors()}")
    return f"#{s} rejected estop_active (gripper stays {g0}); after estop_reset #{s2} ok -> closed"


async def case_5_bad_payload(c):
    """state 缺漏或不合法 → rejected bad_payload，夾爪不動、stub 不掛。"""
    g0 = (await fresh_state(c))["gripper"]
    bad = [
        {},
        {"state": "close"},  # gripper_jog 的 action 拼法，不是 gripper_set 的 state
        {"state": "OPEN"},
        {"state": None},
        {"state": 1},
        {"state": ["open"]},
        {"action": "open"},
        None,
        ["open"],
        "open",
    ]
    out = []
    for p in bad:
        c.seq += 1
        mark = c.mark()
        await c.ws.send(json.dumps({"v": 1, "type": "gripper_set",
                                    "ts": int(time.time() * 1000), "seq": c.seq, "payload": p}))
        await c.wait_for(is_ack(c.seq), 1, f"ack for payload {p!r}")
        got = acks(c.since(mark))
        check(got == [(c.seq, "rejected", "bad_payload")], f"payload {p!r}: acks {got}")
        out.append(json.dumps(p))
    st = await settled_state(c)
    check(st["gripper"] == g0, f"gripper changed by bad payloads: {st['gripper']} != {g0}")
    s, got = await gripper_set(c, "closed")
    check(got == [(s, "ok", "gripper_updated")], f"stub not usable after bad payloads: {got}")
    check(not c.errors(), f"error messages: {c.errors()}")
    return f"{len(bad)} bad payloads rejected bad_payload: {', '.join(out)}"


CASES = {
    1: case_1_set_closed_then_open,
    2: case_2_busy_during_pose,
    3: case_3_busy_other_holds,
    4: case_4_estop,
    5: case_5_bad_payload,
}


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
