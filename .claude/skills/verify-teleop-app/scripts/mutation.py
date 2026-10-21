#!/usr/bin/env python
"""反向測試：拿掉 fireEstop 的「先停 keepalive」，情境 1 必須 FAIL；原版必須 PASS。

用法：python mutation.py [--app-dir DIR]
只改複本（$VERIFY_TELEOP_OUT/mutant-<時間戳>/），不碰 repo 工作樹。
兩者都符合才 exit 0。
"""

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from datetime import datetime
from pathlib import Path

HERE = Path(__file__).resolve().parent
_guess = HERE.parents[3]
REPO_ROOT = _guess if (_guess / "index.html").is_file() else Path.cwd()
WORK_ROOT = Path(os.environ.get("VERIFY_TELEOP_OUT") or Path(tempfile.gettempdir()) / "verify-teleop")
LINE = re.compile(r"^[ \t]*endHold\(\{ sendStop: false \}\);[ \t]*\n", re.M)


def run_verify(app_dir, out):
    r = subprocess.run(
        [sys.executable, str(HERE / "verify.py"), "--scenario", "1",
         "--app-dir", str(app_dir), "--out", str(out)],
    )
    return r.returncode


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--app-dir", default=str(REPO_ROOT))
    args = ap.parse_args()
    app = Path(args.app_dir).resolve()
    ts = datetime.now().strftime("%Y%m%d-%H%M%S")
    mutant = WORK_ROOT / f"mutant-{ts}"
    shutil.copytree(app, mutant, ignore=shutil.ignore_patterns(".git", ".claude", ".idea"))

    src = (mutant / "app.js").read_text(encoding="utf-8")
    start = src.index("function fireEstop()")
    end = src.index("\n}\n", start)
    body = src[start:end]
    mutated_body, n = LINE.subn("", body + "\n")
    if n != 1:
        sys.exit(f"fireEstop 裡找到 {n} 行 endHold({{ sendStop: false }});，預期 1 行")
    (mutant / "app.js").write_text(
        src[:start] + mutated_body.rstrip("\n") + src[end:], encoding="utf-8"
    )
    print(f"--- 突變體：{mutant}（fireEstop 已拿掉 endHold({{ sendStop: false }});）")

    print("=== 突變體跑情境 1（必須 FAIL）")
    mutant_rc = run_verify(mutant, mutant / "evidence")
    print(f"突變體 exit code = {mutant_rc}")
    print("=== 原版跑情境 1（必須 PASS）")
    orig_rc = run_verify(app, WORK_ROOT / f"mutation-original-{ts}")
    print(f"原版 exit code = {orig_rc}")

    # 突變體要因為「keepalive 沒停」這項檢查失敗，不能只是腳本壞掉
    res = json.loads((mutant / "evidence" / "result.json").read_text(encoding="utf-8"))
    failed = [c["name"] for c in res["scenarios"]["1"]["checks"] if not c["ok"]]
    print("突變體失敗的檢查：", failed)
    ok = (
        mutant_rc == 1
        and "estop 之後沒有任何 jog_keepalive" in failed
        and orig_rc == 0
    )
    print("MUTATION:", "OK（突變體 FAIL、原版 PASS）" if ok else "NOT OK")
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()
