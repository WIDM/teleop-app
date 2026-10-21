#!/usr/bin/env python
"""teleop-app 急停與重置驗證：一個指令跑完 Launch -> Doctor -> Drive -> Evidence -> Cleanup。

用法：
    python verify.py [--scenario all|1|2|3|4|5] [--app-dir DIR] [--out DIR]
                     [--down-delay-ms N] [--doctor-only]

判準只看 wire（proxy 錄到的雙向訊息）與 DOM，不看 app 內部變數。
任一 doctor 檢查或情境失敗就 exit 1。只殺自己啟的 PID。
"""

import argparse
import asyncio
import json
import os
import socket
import subprocess
import sys
import tempfile
import time
import urllib.request
from datetime import datetime
from pathlib import Path

from playwright.async_api import async_playwright
from websockets.asyncio.client import connect as ws_connect
from websockets.asyncio.server import serve as ws_serve

STUB_PORT = 8765  # stub_server.py 寫死 8765
_guess = Path(__file__).resolve().parents[4]
REPO_ROOT = _guess if (_guess / "index.html").is_file() else Path.cwd()
WORK_ROOT = Path(os.environ.get("VERIFY_TELEOP_OUT") or Path(tempfile.gettempdir()) / "verify-teleop")

SEL_Z_UP = '.hold.move[data-axis="z"][data-dir="1"]'
SEL_X_UP = '.hold.move[data-axis="x"][data-dir="1"]'
GATE_OK = "可操作"
GATE_ESTOP = "急停中，按「解除急停」"
HOME_POS = [0.02, -0.14, 0.16]
REQUIRED_SELECTORS = [
    "#serverUrl", "#connectBtn", "#gate", "#stateBar", "#estopBtn",
    "#estopResetBtn", "#resetSceneBtn", SEL_Z_UP, SEL_X_UP,
]


def now_ms():
    return round(time.time() * 1000)


def port_in_use(port):
    with socket.socket() as s:
        s.settimeout(0.3)
        return s.connect_ex(("127.0.0.1", port)) == 0


def free_port(start):
    p = start
    while port_in_use(p):
        p += 1
    return p


class Wire:
    """proxy 錄到的雙向訊息。dir：up＝頁面送 server，down＝server 送頁面。"""

    def __init__(self, path):
        self.recs = []
        self.scn = 0
        self.fh = open(path, "a", encoding="utf-8")

    def add(self, dir_, conn, raw):
        try:
            msg = json.loads(raw)
        except Exception:
            msg = {}
        rec = {"dir": dir_, "t": now_ms(), "scn": self.scn, "conn": conn, "raw": raw}
        self.fh.write(json.dumps(rec, ensure_ascii=False) + "\n")
        self.fh.flush()
        self.recs.append({**rec, "msg": msg})

    def mark(self):
        return len(self.recs)

    def find(self, since, dir_, type_=None, pred=None):
        for i in range(since, len(self.recs)):
            r = self.recs[i]
            m = r["msg"]
            if r["dir"] != dir_ or (type_ and m.get("type") != type_):
                continue
            if pred is None or pred(m.get("payload", {}), m):
                return i
        return None

    def count(self, since, dir_, type_):
        return sum(
            1 for r in self.recs[since:]
            if r["dir"] == dir_ and r["msg"].get("type") == type_
        )

    def ack_for(self, since, seq):
        i = self.find(since, "down", "ack", lambda p, m: p.get("seq") == seq)
        return None if i is None else self.recs[i]["msg"]["payload"]


class Proxy:
    """錄音 WebSocket proxy：頁面連它，它轉給 stub。down 方向可加延遲模擬真實網路。"""

    def __init__(self, port, wire, delay_s):
        self.port, self.wire, self.delay = port, wire, delay_s
        self.n = 0
        self.conns = {}
        self.server = None

    async def start(self):
        self.server = await ws_serve(self.handler, "127.0.0.1", self.port)

    async def stop(self):
        self.server.close()
        await self.server.wait_closed()

    async def handler(self, client):
        self.n += 1
        cid = self.n
        try:
            up = await ws_connect(f"ws://127.0.0.1:{STUB_PORT}", open_timeout=5)
        except Exception:
            await client.close()
            return
        self.conns[cid] = (client, up)
        q = asyncio.Queue()

        async def c2s():
            try:
                async for raw in client:
                    self.wire.add("up", cid, raw)
                    await up.send(raw)
            except Exception:
                pass

        async def s2c_read():
            try:
                async for raw in up:
                    self.wire.add("down", cid, raw)
                    q.put_nowait((time.monotonic() + self.delay, raw))
            except Exception:
                pass
            q.put_nowait(None)

        async def s2c_write():
            try:
                while True:
                    item = await q.get()
                    if item is None:
                        break
                    due, raw = item
                    await asyncio.sleep(max(0, due - time.monotonic()))
                    await client.send(raw)
            except Exception:
                pass

        tasks = [asyncio.create_task(f()) for f in (c2s, s2c_read, s2c_write)]
        await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
        for t in tasks:
            t.cancel()
        for c in (client, up):
            try:
                await c.close()
            except Exception:
                pass
        self.conns.pop(cid, None)

    async def drop_clients(self):
        """模擬網路中斷：stub 那條關掉，頁面那條直接 abort。"""
        for client, up in list(self.conns.values()):
            await up.close()
            client.transport.abort()


class Stack:
    """自己啟的子行程，記 PID，只殺這些。"""

    def __init__(self, app_dir, out):
        self.app_dir, self.out = app_dir, out
        self.procs = {}

    def spawn(self, name, args):
        log = open(self.out / f"{name}.log", "ab")
        p = subprocess.Popen(
            args, cwd=self.app_dir, stdout=log, stderr=subprocess.STDOUT
        )
        self.procs[name] = p
        return p

    def kill(self, name):
        p = self.procs.pop(name, None)
        if not p:
            return
        p.terminate()
        try:
            p.wait(5)
        except subprocess.TimeoutExpired:
            p.kill()
            p.wait()

    def kill_all(self):
        for name in list(self.procs):
            self.kill(name)

    async def start_stub(self):
        self.spawn("stub", [sys.executable, "stub_server.py"])
        for _ in range(50):
            try:
                async with ws_connect(f"ws://127.0.0.1:{STUB_PORT}", open_timeout=1):
                    return
            except Exception:
                await asyncio.sleep(0.1)
        raise RuntimeError("stub 起不來，看 stub.log")


class Session:
    """一個情境用的瀏覽器分頁，含 CDP 多點觸控（模擬兩根手指）。"""

    def __init__(self, page, cdp, wire, out, n):
        self.page, self.cdp, self.wire, self.out, self.n = page, cdp, wire, out, n
        self.touches = {}
        self.checks = []

    def check(self, name, ok, detail=""):
        self.checks.append({"name": name, "ok": bool(ok), "detail": str(detail)})

    async def shot(self, label):
        await self.page.screenshot(path=str(self.out / f"s{self.n}-{label}.png"))

    async def center(self, sel):
        await self.page.locator(sel).scroll_into_view_if_needed()
        b = await self.page.locator(sel).bounding_box()
        return b["x"] + b["width"] / 2, b["y"] + b["height"] / 2

    async def _touch(self, type_, pts):
        await self.cdp.send(
            "Input.dispatchTouchEvent", {"type": type_, "touchPoints": pts}
        )

    async def finger_down(self, fid, sel):
        x, y = await self.center(sel)
        self.touches[fid] = {"x": x, "y": y, "id": fid}
        await self._touch("touchStart", list(self.touches.values()))

    async def finger_up(self, fid):
        p = self.touches.pop(fid, None)
        if p:
            await self._touch("touchEnd", [p])

    async def release_all(self):
        self.touches.clear()
        await self._touch("touchEnd", [])

    async def text(self, sel):
        return (await self.page.locator(sel).text_content() or "").strip()

    async def until(self, pred, timeout=6.0, what="條件"):
        end = time.monotonic() + timeout
        while time.monotonic() < end:
            r = pred()
            if asyncio.iscoroutine(r):
                r = await r
            if r is not None and r is not False:
                return r
            await asyncio.sleep(0.02)
        raise TimeoutError(f"等不到：{what}")

    async def _gate_is(self, text):
        return (await self.text("#gate")) == text

    async def until_gate(self, text, timeout=6.0):
        await self.until(lambda: self._gate_is(text), timeout, f"#gate＝{text}")

    async def connect(self, base_url, proxy_port):
        await self.page.goto(base_url + "/index.html")
        await self.page.fill("#serverUrl", f"ws://127.0.0.1:{proxy_port}")
        await self.page.click("#connectBtn")
        await self.until_gate(GATE_OK)

    async def all_hold_disabled(self):
        return await self.page.eval_on_selector_all(
            ".hold", "els => els.length > 0 && els.every(e => e.disabled)"
        )


# ---------- 五個情境 ----------

async def s1(s, ctx):
    """按住 z+ 時按急停：keepalive 立刻停、ack 對得上、UI 鎖住。"""
    w = s.wire
    await s.connect(ctx["base"], ctx["proxy_port"])
    m0 = w.mark()
    await s.finger_down(1, SEL_Z_UP)
    ji = await s.until(lambda: w.find(m0, "up", "jog_start"), what="jog_start")
    jog_seq = w.recs[ji]["msg"]["seq"]
    await s.until(lambda: w.find(ji, "up", "jog_keepalive"), what="jog_keepalive")
    await s.shot("holding")
    await s.finger_down(2, "#estopBtn")  # 第二根手指按急停，第一根還按著
    ei = await s.until(lambda: w.find(ji, "up", "estop"), what="up estop")
    estop_seq = w.recs[ei]["msg"]["seq"]
    engaged = await s.until(
        lambda: w.ack_for(ei, estop_seq), what="estop ack", timeout=8
    )
    await asyncio.sleep(1.2)  # 急停後至少再觀察 1 秒
    ka = w.count(ei, "up", "jog_keepalive")
    s.check("estop 之後沒有任何 jog_keepalive", ka == 0, f"estop 之後看到 {ka} 則")
    s.check("ack estop_engaged", engaged.get("reason") == "estop_engaged", engaged)
    final = w.ack_for(ji, jog_seq)
    s.check(
        "原 jog_start 最終 ack reason＝estopped",
        final is not None and final.get("reason") == "estopped",
        final,
    )
    gate = await s.text("#gate")
    s.check("#gate＝急停中", gate == GATE_ESTOP, gate)
    s.check("所有 .hold disabled", await s.all_hold_disabled())
    s.check("#estopResetBtn enabled", await s.page.is_enabled("#estopResetBtn"))
    await s.shot("estop")
    await s.release_all()


async def _engage_estop(s):
    """前置：用滑鼠按急停（不按住任何鍵），等到 UI 進急停。"""
    await s.page.click("#estopBtn")
    await s.until_gate(GATE_ESTOP)


async def s2(s, ctx):
    """急停中按方向鍵：wire 不得出現新的 jog_start。"""
    w = s.wire
    await s.connect(ctx["base"], ctx["proxy_port"])
    await _engage_estop(s)
    m = w.mark()
    await s.finger_down(1, SEL_Z_UP)
    await s.finger_down(2, SEL_X_UP)
    await asyncio.sleep(1.2)
    n = w.count(m, "up", "jog_start")
    s.check("急停中按方向鍵沒有新 jog_start", n == 0, f"看到 {n} 則")
    s.check("按完 .hold 仍全 disabled", await s.all_hold_disabled())
    gate = await s.text("#gate")
    s.check("#gate 仍是急停中", gate == GATE_ESTOP, gate)
    await s.shot("pressed-in-estop")
    await s.release_all()


async def s3(s, ctx):
    """解除急停：ack、state、gate 恢復；按住的動作不自動恢復，重按才有新 jog_start。"""
    w = s.wire
    await s.connect(ctx["base"], ctx["proxy_port"])
    m0 = w.mark()
    await s.finger_down(1, SEL_Z_UP)
    await s.until(lambda: w.find(m0, "up", "jog_start"), what="jog_start")
    await s.finger_down(2, "#estopBtn")
    await s.until_gate(GATE_ESTOP)
    await s.finger_up(2)  # 第一根手指仍壓在 z+ 上
    m = w.mark()
    await s.page.click("#estopResetBtn")
    ri = await s.until(lambda: w.find(m, "up", "estop_reset"), what="up estop_reset")
    ack = await s.until(
        lambda: w.ack_for(ri, w.recs[ri]["msg"]["seq"]), what="estop_reset ack"
    )
    s.check("ack estop_reset", ack.get("reason") == "estop_reset", ack)
    st = await s.until(
        lambda: w.find(ri, "down", "state", lambda p, _m: p.get("estop") is False),
        what="state estop=false",
    )
    s.check("之後 state estop=false", st is not None)
    await s.until_gate(GATE_OK)
    s.check("#gate＝可操作", True)
    await asyncio.sleep(1.2)
    n = w.count(m, "up", "jog_start")
    s.check("解除後沒有自動恢復的 jog_start", n == 0, f"看到 {n} 則")
    await s.shot("after-reset")
    await s.finger_up(1)
    m2 = w.mark()
    await s.finger_down(1, SEL_Z_UP)
    try:
        await s.until(lambda: w.find(m2, "up", "jog_start"), what="重按後的 jog_start")
        s.check("重新按才有新的 jog_start", True)
    except TimeoutError as e:
        s.check("重新按才有新的 jog_start", False, e)
    await s.shot("pressed-again")
    await s.release_all()


async def s4(s, ctx):
    """按住中斷線：stub 進急停；重連後仍急停、不自動續按；解除才恢復。"""
    w = s.wire
    await s.connect(ctx["base"], ctx["proxy_port"])
    m0 = w.mark()
    await s.finger_down(1, SEL_Z_UP)
    ji = await s.until(lambda: w.find(m0, "up", "jog_start"), what="jog_start")
    await s.until(lambda: w.find(ji, "up", "jog_keepalive"), what="jog_keepalive")
    old_conn = w.recs[ji]["conn"]
    await ctx["proxy"].drop_clients()
    m = w.mark()
    await s.until_gate(GATE_ESTOP, timeout=15)  # 自動重連（約 1 秒後）才會回到這個畫面
    s.check("#gate 重連後顯示急停中", True)
    new_hello = w.find(m, "up", "hello")
    s.check(
        "頁面自動重連（新連線送 hello）",
        new_hello is not None and w.recs[new_hello]["conn"] != old_conn,
    )
    st = w.find(m, "down", "state", lambda p, _m: p.get("estop") is True)
    s.check("stub 進急停（新連線的 state estop=true）", st is not None)
    await s.shot("reconnected-estop")
    await asyncio.sleep(1.5)
    n = w.count(m, "up", "jog_start")
    s.check("重連後沒有自動續按（無新 jog_start）", n == 0, f"看到 {n} 則")
    s.check("重連後 .hold 全 disabled", await s.all_hold_disabled())
    await s.release_all()
    await s.page.click("#estopResetBtn")
    await s.until_gate(GATE_OK)
    s.check("解除後才回「可操作」", True)
    await s.shot("after-reset")


async def _contains(s, sel, needle):
    return needle in await s.text(sel)


async def s5(s, ctx):
    """重置場景：ack scene_reset，ee_pos 回到初始值，stateBar 顯示公分值。"""
    w = s.wire
    await s.connect(ctx["base"], ctx["proxy_port"])
    m0 = w.mark()
    await s.finger_down(1, SEL_Z_UP)
    await s.until(lambda: w.find(m0, "up", "jog_start"), what="jog_start")
    await s.until(
        lambda: w.find(m0, "down", "state", lambda p, _m: p["ee_pos"][2] > 0.18),
        what="手臂 z 被推高",
    )
    await s.release_all()
    await s.shot("moved")
    m = w.mark()
    await s.page.click("#resetSceneBtn")
    ri = await s.until(lambda: w.find(m, "up", "reset_scene"), what="up reset_scene")
    ack = await s.until(
        lambda: w.ack_for(ri, w.recs[ri]["msg"]["seq"]), what="scene_reset ack"
    )
    s.check("ack scene_reset", ack.get("reason") == "scene_reset", ack)
    ai = w.find(ri, "down", "ack", lambda p, _m: p.get("reason") == "scene_reset")
    st = await s.until(lambda: w.find(ai, "down", "state"), what="reset 後的 state")
    pos = w.recs[st]["msg"]["payload"]["ee_pos"]
    s.check(
        "state ee_pos 回到初始值",
        all(abs(a - b) < 1e-6 for a, b in zip(pos, HOME_POS)),
        pos,
    )
    expect = "ee(cm) 2.0, -14.0, 16.0"
    try:
        await s.until(
            lambda: _contains(s, "#stateBar", expect), what=f"#stateBar 含 {expect}"
        )
        s.check("#stateBar 顯示公分值", True, expect)
    except TimeoutError:
        s.check("#stateBar 顯示公分值", False, await s.text("#stateBar"))
    await s.shot("after-reset-scene")


SCENARIOS = {"1": s1, "2": s2, "3": s3, "4": s4, "5": s5}


# ---------- Doctor ----------

def doctor(app_dir):
    out = []
    for f in ("index.html", "app.js", "stub_server.py"):
        out.append({"name": f"app-dir 有 {f}", "ok": (app_dir / f).is_file(), "detail": str(app_dir / f)})
    out.append({"name": f"ws {STUB_PORT} 沒被占用", "ok": not port_in_use(STUB_PORT), "detail": ""})
    return out


async def doctor_runtime(page_url, page):
    out = []
    resp = urllib.request.urlopen(page_url + "/index.html", timeout=3)
    out.append({"name": "http 伺服器回 200", "ok": resp.status == 200, "detail": resp.status})
    await page.goto(page_url + "/index.html")
    missing = [s for s in REQUIRED_SELECTORS if await page.locator(s).count() == 0]
    out.append({"name": "頁面有所有需要的 selector", "ok": not missing, "detail": str(missing)})
    return out


# ---------- 主流程 ----------

async def run(args):
    app_dir = Path(args.app_dir).resolve()
    out = (
        Path(args.out).expanduser()
        if args.out
        else WORK_ROOT / datetime.now().strftime("%Y%m%d-%H%M%S")
    )
    out.mkdir(parents=True, exist_ok=True)
    result = {"app_dir": str(app_dir), "out": str(out), "doctor": [], "scenarios": {}}
    names = [] if args.doctor_only else (list(SCENARIOS) if args.scenario == "all" else [args.scenario])

    result["doctor"] += doctor(app_dir)
    wire = Wire(out / "wire.jsonl")
    stack = Stack(app_dir, out)
    proxy = None
    try:
        if all(d["ok"] for d in result["doctor"]):
            http_port = free_port(8800)
            stack.spawn("http", [sys.executable, "-m", "http.server", str(http_port), "--bind", "127.0.0.1"])
            proxy_port = free_port(http_port + 1)
            proxy = Proxy(proxy_port, wire, args.down_delay_ms / 1000)
            await proxy.start()
            base = f"http://127.0.0.1:{http_port}"
            for _ in range(50):
                if port_in_use(http_port):
                    break
                await asyncio.sleep(0.1)
            async with async_playwright() as pw:
                browser = await pw.chromium.launch(headless=True)
                try:
                    bctx = await browser.new_context(
                        has_touch=True, viewport={"width": 420, "height": 1100}
                    )
                    page = await bctx.new_page()
                    result["doctor"] += await doctor_runtime(base, page)
                    await bctx.close()
                    if all(d["ok"] for d in result["doctor"]):
                        for n in names:
                            await run_scenario(
                                n, browser, stack, proxy, wire, out, base, proxy_port, result
                            )
                finally:
                    await browser.close()
    except Exception as e:
        result["fatal"] = repr(e)
    finally:
        if proxy:
            await proxy.stop()
        stack.kill_all()
        wire.fh.close()
    ok = (
        "fatal" not in result
        and all(d["ok"] for d in result["doctor"])
        and (args.doctor_only or bool(result["scenarios"]))
        and all(r["passed"] for r in result["scenarios"].values())
    )
    result["passed"] = ok
    (out / "result.json").write_text(
        json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    return result, ok


async def run_scenario(n, browser, stack, proxy, wire, out, base, proxy_port, result):
    """每個情境：全新 stub（乾淨狀態）、全新瀏覽器 context。"""
    wire.scn = int(n)
    rec = {"passed": False, "checks": [], "error": None}
    result["scenarios"][n] = rec
    s = None
    bctx = None
    try:
        await stack.start_stub()
        bctx = await browser.new_context(
            has_touch=True, viewport={"width": 420, "height": 1100}
        )
        page = await bctx.new_page()
        cdp = await bctx.new_cdp_session(page)
        s = Session(page, cdp, wire, out, int(n))
        await SCENARIOS[n](s, {"base": base, "proxy_port": proxy_port, "proxy": proxy})
    except Exception as e:
        rec["error"] = f"{type(e).__name__}: {e}"
        if s:
            try:
                await s.shot("error")
            except Exception:
                pass
    finally:
        if s:
            rec["checks"] = s.checks
        rec["passed"] = (
            rec["error"] is None
            and bool(rec["checks"])
            and all(c["ok"] for c in rec["checks"])
        )
        if bctx:
            await bctx.close()
        stack.kill("stub")
        await asyncio.sleep(0.3)
    print(f"情境 {n}: {'PASS' if rec['passed'] else 'FAIL'}")
    for c in rec["checks"]:
        tail = f"  {c['detail']}" if not c["ok"] else ""
        print(f"  [{'ok' if c['ok'] else 'FAIL'}] {c['name']}{tail}")
    if rec["error"]:
        print(f"  [ERROR] {rec['error']}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--scenario", default="all", choices=["all", "1", "2", "3", "4", "5"])
    ap.add_argument("--app-dir", default=str(REPO_ROOT))
    ap.add_argument("--out", default=None)
    ap.add_argument(
        "--down-delay-ms", type=int, default=400,
        help="proxy 對 server→頁面方向加的延遲（模擬網路），讓「先停 keepalive」的差別看得出來",
    )
    ap.add_argument("--doctor-only", action="store_true")
    args = ap.parse_args()
    result, ok = asyncio.run(run(args))
    for d in result["doctor"]:
        print(f"doctor [{'ok' if d['ok'] else 'FAIL'}] {d['name']}")
    if "fatal" in result:
        print("fatal:", result["fatal"])
    print(f"證據：{result['out']}")
    print("RESULT:", "PASS" if ok else "FAIL")
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()
