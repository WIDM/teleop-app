"""stub_server.py 的 pose_update 工作空間截邊與跳點過濾驗收。

每個情境重啟一次 stub（ws 8765 寫死），用 websockets 走真的 wire。
hold 系列的回歸在 stub_hold_ack_test.py。任一情境失敗 exit 1。

    python tests/stub_workspace_jump_test.py              # 全部情境
    python tests/stub_workspace_jump_test.py --case 2     # 指定情境
    python tests/stub_workspace_jump_test.py --stub /path/to/stub_server.py
"""

import argparse
import asyncio
import json
import math
import subprocess
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from stub_hold_ack_test import (  # noqa: E402
    ROOT,
    acks,
    check,
    close_to,
    fresh_state,
    is_ack,
    port_in_use,
    run_case,
    stop_stub,
    stream,
)

# stub 的暫定預設值（stub_server.py 的 CONFIG）
LO = [-0.23, -0.39, 0.05]
HI = [0.27, 0.11, 0.60]
JUMP = 0.060
CUSTOM = {
    "workspace_min_m": [-0.03, -0.19, 0.12],
    "workspace_max_m": [0.07, -0.09, 0.20],
    "jump_threshold_m": 0.02,
}


def update(p_rel, gripper_delta=0.0):
    return {"p_rel_m": list(p_rel), "q_rel": [0, 0, 0, 1], "gripper_delta": gripper_delta}


def full_acks(msgs):
    return [m["payload"] for m in msgs if m["type"] == "ack"]


def target(ee0, p_rel, lo=LO, hi=HI):
    """跟 stub 同一套算術：ee0 + p_rel 逐軸截進 [lo, hi]。"""
    raw = [e + v for e, v in zip(ee0, p_rel)]
    return [min(max(v, l), h) for v, l, h in zip(raw, lo, hi)], raw


async def settled_state(c):
    """跳過送出前就排隊的那則 state，拿一則確定在指令處理之後的。"""
    await fresh_state(c)
    return await fresh_state(c)


async def start_pose(c):
    ee0 = (await fresh_state(c))["ee_pos"]
    mark = c.mark()
    s = await c.send("pose_start", {"frame": "webxr_local", "control_mode": "translation"})
    return ee0, mark, s


async def release(c, s_start):
    """pose_stop 並等 start 的最終 ack；回傳這段的 ack。"""
    mark = c.mark()
    s_stop = await c.send("pose_stop")
    await c.wait_for(is_ack(s_start), 1, "pose final ack")
    got = acks(c.since(mark))
    want = [(s_stop, "ok", "pose_stop_requested"), (s_start, "ok", "pose_stopped")]
    check(got == want, f"release acks {got} != {want}")


async def send_seq(c, p_rels, gripper_delta=0.0):
    """依序送 pose_update（每 33 ms 一則）；回傳 seq。"""
    return await stream(c, "pose_update", lambda i: update(p_rels[i], gripper_delta), n=len(p_rels))


async def hold_ee(c):
    """hold 中拿一則已處理完前面 update 的 state（不超過 watchdog）。"""
    st = await settled_state(c)
    check(st["ik"] == "moving", f"hold ended unexpectedly: {st}")
    return st


def check_no_update_acks(c, mark, seqs, what):
    got = [a for a in full_acks(c.since(mark)) if a["seq"] in set(seqs)]
    check(not got, f"{what}: in-range updates must not be acked, got {got}")


# ---- 情境 ----------------------------------------------------------------


async def case_1_in_range(c):
    """範圍內的 pose_update：照常套用、不回 ack（沒有 clamped 欄位），ee_pos == ee0 + p_rel_m。"""
    ee0, mark, s_start = await start_pose(c)
    path = [[0, 0, 0], [0.02, 0.0, -0.01], [0.04, 0.02, -0.02], [0.05, 0.04, -0.03]]
    seqs = await send_seq(c, path)
    st = await hold_ee(c)
    want, raw = target(ee0, path[-1])
    check(want == raw, "fixture must stay in range")
    check(close_to(st["ee_pos"], want), f"ee_pos {st['ee_pos']} != ee0+p_rel {want}")
    check_no_update_acks(c, mark, seqs, "in-range")
    await release(c, s_start)
    check(not c.errors(), f"error messages: {c.errors()}")
    return f"{len(seqs)} updates, no update ack; ee0={ee0} -> ee_pos={st['ee_pos']}"


async def case_2_each_axis_clamped(c):
    """x、y、z 各往 min、max 推出界：截在邊界、hold 照常，每筆截過的 ack 是 ok pose_updated clamped:true。"""
    out = []
    for axis in range(3):
        for sign, bound in ((-1, LO[axis]), (1, HI[axis])):
            ee0, mark, s_start = await start_pose(c)
            path, k = [[0, 0, 0]], 0
            while True:
                k += 1
                p = [0.0, 0.0, 0.0]
                p[axis] = sign * 0.03 * k  # 每步 30 mm，不觸發跳點
                path.append(p)
                raw = ee0[axis] + p[axis]
                check(abs(raw - bound) > 1e-6, f"fixture lands on the bound: {raw}")
                if sign * (raw - bound) > 0.04:
                    break
            seqs = await send_seq(c, path)
            st = await hold_ee(c)
            want = list(ee0)
            want[axis] = bound
            check(st["ee_pos"] == want, f"axis {'xyz'[axis]} {sign:+d}: ee_pos {st['ee_pos']} != {want}")
            expect = []
            for s, p in zip(seqs, path):
                t, raw = target(ee0, p)
                if t != raw:
                    expect.append({"seq": s, "status": "ok", "reason": "pose_updated", "clamped": True})
            got = full_acks(c.since(mark))
            check(expect and got == expect, f"axis {'xyz'[axis]} {sign:+d}: acks {got} != {expect}")
            await release(c, s_start)
            s_reset = await c.send("reset_scene")
            await c.wait_for(is_ack(s_reset, "scene_reset"), 1, "reset_scene ack")
            out.append(f"{'xyz'[axis]}{'+' if sign > 0 else '-'}→{bound} ({len(expect)} clamped acks)")
    check(not c.errors(), f"error messages: {c.errors()}")
    return "; ".join(out)


async def case_3_jump_rejected_hold_alive(c):
    """跳超過 60 mm 的那筆 rejected jump、整筆丟掉（含 gripper_delta）；hold 不中斷，>1 s 後仍能正常放開。"""
    ee0, mark, s_start = await start_pose(c)
    ok_path = [[0, 0, 0], [0.01, 0, 0], [0.04, 0, 0]]
    await send_seq(c, ok_path, gripper_delta=0.1)
    s_jump = await c.send("pose_update", update([0.11, 0, 0], gripper_delta=0.9))  # 離 0.04 有 70 mm
    await c.wait_for(is_ack(s_jump), 1, "jump ack")
    alive = await send_seq(c, [[0.04, 0, 0]] * 33, gripper_delta=0.1)  # ~1.1 s，跨過 watchdog
    st = await hold_ee(c)
    want, _ = target(ee0, [0.04, 0, 0])
    check(close_to(st["ee_pos"], want), f"ee_pos {st['ee_pos']} != last accepted {want}")
    check(st["gripper_delta"] == 0.1, f"dropped update leaked gripper_delta: {st}")
    got = full_acks(c.since(mark))
    check(got == [{"seq": s_jump, "status": "rejected", "reason": "jump"}], f"acks {got}")
    await release(c, s_start)
    check(not c.errors(), f"error messages: {c.errors()}")
    return (f"update#{s_jump} rejected jump; {len(alive)} more updates over ~1.1 s kept the hold; "
            f"ee_pos={st['ee_pos']} gripper_delta={st['gripper_delta']}; released normally")


async def case_4_recovery_after_jump(c):
    """跳點後比的仍是上一個採用的目標：離它 60 mm 內的下一筆就採用，手臂接著動。"""
    ee0, mark, s_start = await start_pose(c)
    path = [
        [0, 0, 0],      # 採用
        [0.02, 0, 0],   # 採用（20 mm）
        [0.10, 0, 0],   # 丟（離 0.02 有 80 mm）
        [0.12, 0, 0],   # 丟（離被丟的 0.10 只有 20 mm，但離採用的 0.02 有 100 mm）
        [0.06, 0, 0],   # 採用（離 0.02 有 40 mm）
        [0.10, 0, 0],   # 採用（離 0.06 有 40 mm）
    ]
    seqs = await send_seq(c, path)
    st = await hold_ee(c)
    want, _ = target(ee0, path[-1])
    check(close_to(st["ee_pos"], want), f"ee_pos {st['ee_pos']} != {want}")
    got = full_acks(c.since(mark))
    expect = [{"seq": seqs[i], "status": "rejected", "reason": "jump"} for i in (2, 3)]
    check(got == expect, f"acks {got} != {expect}")
    await release(c, s_start)
    check(not c.errors(), f"error messages: {c.errors()}")
    return f"#{seqs[2]}, #{seqs[3]} rejected jump; #{seqs[4]}, #{seqs[5]} accepted; ee_pos={st['ee_pos']}"


async def case_5_custom_params(c):
    """--config 換工作空間與門檻：小門檻擋下預設會過的 30 mm，窄邊界截在自訂值；壞設定 stub 拒絕啟動。"""
    lo, hi = CUSTOM["workspace_min_m"], CUSTOM["workspace_max_m"]
    ee0, mark, s_start = await start_pose(c)
    path = [
        [0, 0, 0],
        [0.015, 0, 0],   # 採用（15 mm）
        [0.045, 0, 0],   # 丟（30 mm > 20 mm；預設門檻會採用）
        [0.03, 0, 0],    # 採用
        [0.045, 0, 0],   # 採用，x 0.065
        [0.06, 0, 0],    # x 0.08 > 0.07 → 截在 0.07
    ]
    seqs = await send_seq(c, path)
    st = await hold_ee(c)
    want, _ = target(ee0, path[-1], lo, hi)
    check(st["ee_pos"] == want and want[0] == hi[0], f"ee_pos {st['ee_pos']} != {want}")
    got = full_acks(c.since(mark))
    expect = [
        {"seq": seqs[2], "status": "rejected", "reason": "jump"},
        {"seq": seqs[5], "status": "ok", "reason": "pose_updated", "clamped": True},
    ]
    check(got == expect, f"acks {got} != {expect}")
    await release(c, s_start)
    check(not c.errors(), f"error messages: {c.errors()}")
    return f"custom {CUSTOM}: #{seqs[2]} rejected jump, #{seqs[5]} clamped to x={hi[0]}"


async def case_6_first_update_vs_hold_start(c):
    """每次 hold 的第一筆跟 hold 開始時的 ee 比：離 ee0 70 mm 的第一筆 rejected jump，50 mm 的採用。"""
    ee0, mark, s_start = await start_pose(c)
    s1 = (await send_seq(c, [[0, 0.07, 0]]))[0]
    s2 = (await send_seq(c, [[0, 0.05, 0]]))[0]
    st = await hold_ee(c)
    want, _ = target(ee0, [0, 0.05, 0])
    check(close_to(st["ee_pos"], want), f"ee_pos {st['ee_pos']} != {want}")
    await release(c, s_start)
    got = full_acks(c.since(mark))[:-2]
    check(got == [{"seq": s1, "status": "rejected", "reason": "jump"}], f"acks {got}")
    # 第二次 hold：ee0 是上一次停下的位置，第一筆照樣跟它比
    ee1, mark, s_start = await start_pose(c)
    check(close_to(ee1, want), f"second hold ee0 {ee1} != previous ee {want}")
    s3 = (await send_seq(c, [[0, 0, 0.065]]))[0]
    s4 = (await send_seq(c, [[0, 0, 0.055]]))[0]
    st2 = await hold_ee(c)
    want2, _ = target(ee1, [0, 0, 0.055])
    check(close_to(st2["ee_pos"], want2), f"ee_pos {st2['ee_pos']} != {want2}")
    await release(c, s_start)
    got = full_acks(c.since(mark))[:-2]
    check(got == [{"seq": s3, "status": "rejected", "reason": "jump"}], f"second hold acks {got}")
    check(not c.errors(), f"error messages: {c.errors()}")
    return f"hold 1: #{s1} rejected, #{s2} accepted; hold 2 (ee0={ee1}): #{s3} rejected, #{s4} accepted"


async def case_7_compare_post_clamp(c):
    """跳點比的是截過的目標：界外再往外跳 150 mm 照樣採用（仍 clamped），從界外跳回界內 30 mm 處也採用。"""
    ee0, mark, s_start = await start_pose(c)
    ramp = [[0, 0, 0.03 * k] for k in range(16)]  # z 0.16 → 0.61（截在 0.60）
    seqs = await send_seq(c, ramp)
    s_far = (await send_seq(c, [[0, 0, 0.60]]))[0]   # raw z 0.76：截前差 150 mm、截後差 0
    st_far = await hold_ee(c)
    s_back = (await send_seq(c, [[0, 0, 0.41]]))[0]  # raw z 0.57：截前差 190 mm、截後差 30 mm
    st_back = await hold_ee(c)
    check(st_far["ee_pos"][2] == HI[2], f"far update not applied/clamped: {st_far}")
    want, raw = target(ee0, [0, 0, 0.41])
    check(want == raw and close_to(st_back["ee_pos"], want), f"back-in update: {st_back} != {want}")
    got = full_acks(c.since(mark))
    clamped = [a["seq"] for a in got if a.get("clamped") is True and a["status"] == "ok"]
    check(clamped == [seqs[-1], s_far], f"clamped acks {clamped} != {[seqs[-1], s_far]}")
    check(all(a["status"] == "ok" for a in got), f"unexpected rejection: {got}")
    check(s_back not in {a["seq"] for a in got}, f"in-range update acked: {got}")
    await release(c, s_start)
    check(not c.errors(), f"error messages: {c.errors()}")
    return f"#{s_far} (raw z 0.76) ok clamped -> z={st_far['ee_pos'][2]}; #{s_back} back to z={st_back['ee_pos'][2]:.2f}, no ack"


async def case_8_threshold_boundary(c):
    """門檻是嚴格的 >：沿 x 離上一個採用的目標 59.9 mm 採用、60.1 mm 整筆丟掉（hold 不斷）。"""
    ee0, mark, s_start = await start_pose(c)
    p0, p1, p2 = [0, 0, 0], [0.0599, 0, 0], [0.12, 0, 0]
    t1, raw1 = target(ee0, p1)
    t2, raw2 = target(ee0, p2)
    check(t1 == raw1 and t2 == raw2, "fixture must stay in range (no clamping)")
    # 送出前先用跟 stub 一樣的算術（ee0 + p_rel_m，再 math.dist）確認落在門檻哪一邊
    d1, d2 = math.dist(t1, ee0), math.dist(t2, t1)
    check(d1 < JUMP, f"59.9 mm step computes as {d1!r}, not < {JUMP!r}")
    check(d2 > JUMP, f"60.1 mm step computes as {d2!r}, not > {JUMP!r}")

    s0, s1 = await send_seq(c, [p0, p1], gripper_delta=0.1)
    st1 = await hold_ee(c)
    check(st1["ee_pos"] == t1, f"59.9 mm: ee_pos {st1['ee_pos']} != {t1}")
    check(not full_acks(c.since(mark)), f"59.9 mm must not be acked: {full_acks(c.since(mark))}")

    s2 = await c.send("pose_update", update(p2, gripper_delta=0.9))
    await c.wait_for(is_ack(s2), 1, "60.1 mm jump ack")
    alive = await send_seq(c, [p1] * 33, gripper_delta=0.1)  # ~1.1 s，跨過 watchdog
    st2 = await hold_ee(c)
    check(st2["ee_pos"] == t1, f"60.1 mm not dropped: ee_pos {st2['ee_pos']} != {t1}")
    check(st2["gripper_delta"] == 0.1, f"60.1 mm leaked gripper_delta: {st2}")

    got = full_acks(c.since(mark))
    check(got == [{"seq": s2, "status": "rejected", "reason": "jump"}], f"acks {got}")
    await release(c, s_start)
    check(not c.errors(), f"error messages: {c.errors()}")
    return (f"#{s1} d={d1!r} accepted x={st1['ee_pos'][0]!r}; #{s2} d={d2!r} rejected jump, "
            f"{len(alive)} more updates kept the hold, x stays {st2['ee_pos'][0]!r}")


EXACT_JUMP = 0.0625  # 2**-4，二進位精確


async def case_9_threshold_exact(c):
    """門檻 0.0625（--config）：剛好等於門檻的一步採用（嚴格 >），多 2**-10 m 就 rejected jump。"""
    ee0, mark, s_start = await start_pose(c)
    check(ee0 == [0.02, -0.14, 0.16], f"case needs the stub's home pose, got {ee0}")
    # 先用一步把 x 移到二進位精確的 0.0625，之後的距離才會剛好
    p_a, p_eq, p_over = [0.0425, 0, 0], [0.105, 0, 0], [0.1684765625, 0, 0]
    t_a, raw_a = target(ee0, p_a)
    t_eq, raw_eq = target(ee0, p_eq)
    t_over, raw_over = target(ee0, p_over)
    check(t_a == raw_a and t_eq == raw_eq and t_over == raw_over, "fixture must stay in range (no clamping)")
    check(t_a[0] == 0.0625 and t_eq[0] == 0.125, f"x not binary-exact: {t_a[0]!r}, {t_eq[0]!r}")
    d_a, d_eq, d_over = math.dist(t_a, ee0), math.dist(t_eq, t_a), math.dist(t_over, t_eq)  # 跟 stub 同一套算術
    check(d_a < EXACT_JUMP, f"setup step {d_a!r} not < {EXACT_JUMP!r}")
    check(d_eq == EXACT_JUMP, f"equal step computes as {d_eq!r}, not exactly {EXACT_JUMP!r}")
    check(d_over == EXACT_JUMP + 2 ** -10 and d_over > EXACT_JUMP, f"over step computes as {d_over!r}")

    s0, s_a = await send_seq(c, [[0, 0, 0], p_a])
    st_a = await hold_ee(c)
    check(st_a["ee_pos"] == t_a, f"setup step: ee_pos {st_a['ee_pos']} != {t_a}")
    s_eq = (await send_seq(c, [p_eq]))[0]
    st_eq = await hold_ee(c)
    check(st_eq["ee_pos"] == t_eq, f"d == threshold must be accepted: ee_pos {st_eq['ee_pos']} != {t_eq}")
    check(not full_acks(c.since(mark)), f"accepted steps must not be acked: {full_acks(c.since(mark))}")

    s_over = await c.send("pose_update", update(p_over))
    await c.wait_for(is_ack(s_over), 1, "threshold + 2**-10 jump ack")
    alive = await send_seq(c, [p_eq] * 10)
    st_over = await hold_ee(c)
    check(st_over["ee_pos"] == t_eq, f"over-threshold step not dropped: ee_pos {st_over['ee_pos']} != {t_eq}")
    got = full_acks(c.since(mark))
    check(got == [{"seq": s_over, "status": "rejected", "reason": "jump"}], f"acks {got}")
    await release(c, s_start)
    check(not c.errors(), f"error messages: {c.errors()}")
    return (f"threshold {EXACT_JUMP!r}: #{s_a} d={d_a!r} accepted x={st_a['ee_pos'][0]!r}; "
            f"#{s_eq} d={d_eq!r} (== threshold) accepted x={st_eq['ee_pos'][0]!r}, no ack; "
            f"#{s_over} d={d_over!r} rejected jump, x stays {st_over['ee_pos'][0]!r}; "
            f"{len(alive)} more updates, hold released normally")


CASES = {
    1: case_1_in_range,
    2: case_2_each_axis_clamped,
    3: case_3_jump_rejected_hold_alive,
    4: case_4_recovery_after_jump,
    5: case_5_custom_params,
    6: case_6_first_update_vs_hold_start,
    7: case_7_compare_post_clamp,
    8: case_8_threshold_boundary,
    9: case_9_threshold_exact,
}
CONFIG_OF = {5: CUSTOM, 9: {"jump_threshold_m": EXACT_JUMP}}
BAD_CONFIGS = [
    {"workspace_min_m": [0.1, 0, 0], "workspace_max_m": [0.0, 1, 1]},  # min > max
    {"jump_threshold_m": 0},
    {"workspace_min_m": [0, 0]},
    {"jump_mm": 60},  # 不認得的鍵
]


# ---- runner --------------------------------------------------------------


def write_config(tmp, n, cfg):
    path = Path(tmp) / f"cfg-{n}.json"
    path.write_text(json.dumps(cfg), encoding="utf-8")
    return path


def start_stub(stub, config=None):
    cmd = [sys.executable, str(stub)] + (["--config", str(config)] if config else [])
    proc = subprocess.Popen(cmd, cwd=str(stub.parent), stdout=subprocess.DEVNULL,
                            stderr=subprocess.PIPE)
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        if proc.poll() is not None:
            raise RuntimeError(f"stub exited: {proc.stderr.read().decode()}")
        if port_in_use(8765):
            return proc
        time.sleep(0.05)
    proc.kill()
    raise RuntimeError("stub did not open ws 8765")


def bad_configs_refused(stub, tmp):
    out = []
    for i, cfg in enumerate(BAD_CONFIGS):
        path = write_config(tmp, f"bad{i}", cfg)
        r = subprocess.run([sys.executable, str(stub), "--config", str(path)],
                           cwd=str(stub.parent), capture_output=True, timeout=5)
        check(r.returncode != 0, f"stub accepted bad config {cfg}")
        check(not port_in_use(8765), "stub left a listener behind")
        out.append(r.stderr.decode().strip().splitlines()[-1])
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--case", type=int, nargs="*", choices=sorted(CASES))
    ap.add_argument("--stub", type=Path, default=ROOT / "stub_server.py")
    args = ap.parse_args()
    if port_in_use(8765):
        print("FAIL: ws 8765 already in use; stop the other stub first")
        return 2

    stub = args.stub.resolve()
    failed = 0
    with tempfile.TemporaryDirectory() as tmp:
        for n in args.case or sorted(CASES):
            fn = CASES[n]
            title = fn.__doc__.strip().splitlines()[0]
            try:
                cfg = CONFIG_OF.get(n)
                proc = start_stub(stub, cfg and write_config(tmp, n, cfg))
                try:
                    detail = asyncio.run(run_case(fn))
                finally:
                    stop_stub(proc)
                if n == 5:
                    detail += "; bad configs refused: " + " | ".join(bad_configs_refused(stub, tmp))
                print(f"PASS case {n}: {title}\n    {detail}")
            except Exception as e:  # noqa: BLE001 — 任何例外都算這個情境失敗
                failed += 1
                print(f"FAIL case {n}: {title}\n    {type(e).__name__}: {e}")
    total = len(args.case or CASES)
    print(f"\n{total - failed} passed, {failed} failed")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
