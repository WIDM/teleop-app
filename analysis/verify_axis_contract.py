#!/usr/bin/env python3
"""靜態檢查：Thomas App 的 jog 軸向／frame 合約是否與 hold-protocol 一致。

對應 docs/so-arm-classmate-issues.md「對一次左移對 log」的本機半邊：
確認 index.html 按鈕 data-* 與 app.js 硬編碼 frame:\"base\" 沒漂，
這樣同學在 S4 上看到方向錯時，可以先排除 App 軸表寫反。

不連 WebSocket、不碰 Isaac。exit 0＝通過；非 0＝合約漂移。

Usage:
    python3 analysis/verify_axis_contract.py
    python3 analysis/verify_axis_contract.py --app-dir /path/to/teleop-app
"""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

# hold-protocol.md / THOMAS_APP_HOLD_PROTOCOL.md 凍結的 base-frame 對照
EXPECTED_MOVES = {
    ("y", -1): "前",
    ("y", 1): "後",
    ("x", 1): "左",
    ("x", -1): "右",
    ("z", 1): "上",
    ("z", -1): "下",
}

BUTTON_RE = re.compile(
    r'class="hold move"[^>]*data-axis="(?P<axis>[xyz])"[^>]*data-dir="(?P<dir>-?1)"[^>]*>'
    r"\s*(?P<label>[^\s<]+)",
    re.DOTALL,
)
# 也接受 data-dir 在 data-axis 前面的寫法
BUTTON_RE_ALT = re.compile(
    r'class="hold move"[^>]*data-dir="(?P<dir>-?1)"[^>]*data-axis="(?P<axis>[xyz])"[^>]*>'
    r"\s*(?P<label>[^\s<]+)",
    re.DOTALL,
)


def parse_move_buttons(html: str) -> dict[tuple[str, int], str]:
    found: dict[tuple[str, int], str] = {}
    for rx in (BUTTON_RE, BUTTON_RE_ALT):
        for m in rx.finditer(html):
            key = (m.group("axis"), int(m.group("dir")))
            label = m.group("label").strip()
            found[key] = label
    return found


def check_html(index_html: Path, errors: list[str]) -> None:
    text = index_html.read_text(encoding="utf-8")
    found = parse_move_buttons(text)
    if len(found) != 6:
        errors.append(f"index.html 應有 6 顆 .hold.move，實際解析到 {len(found)}：{found}")
    for key, label in EXPECTED_MOVES.items():
        if key not in found:
            errors.append(f"缺少按鈕 {label} → axis={key[0]} dir={key[1]}")
        elif found[key] != label:
            errors.append(
                f"按鈕標籤不符：axis={key[0]} dir={key[1]} 預期「{label}」實際「{found[key]}」"
            )
    for key, label in found.items():
        if key not in EXPECTED_MOVES:
            errors.append(f"多餘／未知按鈕 {label} → axis={key[0]} dir={key[1]}")


def check_app_js(app_js: Path, errors: list[str]) -> None:
    text = app_js.read_text(encoding="utf-8")
    # 平移六鍵必須硬編碼 frame:"base"；允許空白差異
    if not re.search(r'frame\s*:\s*"base"', text):
        errors.append('app.js 找不到 frame: "base"（Cartesian jog 必須固定 base）')
    # 不應在 .move 綁定路徑送出 tool（粗查：整個檔若有 frame:"tool" 就警告）
    if re.search(r'frame\s*:\s*"tool"', text):
        errors.append('app.js 出現 frame: "tool"；生產按鈕路徑不應送 tool')
    # 控制路徑不應直接用 DeviceOrientation／WebXR（那些在 orientation.html）
    for needle in ("DeviceOrientationEvent", "AbsoluteOrientationSensor", "navigator.xr"):
        if needle in text:
            errors.append(f"app.js 不應含 IMU／WebXR API（{needle}）；姿態 demo 在 orientation.html")


def check_protocol_doc(protocol: Path, errors: list[str]) -> None:
    if not protocol.is_file():
        errors.append(f"缺少 {protocol}")
        return
    text = protocol.read_text(encoding="utf-8")
    if 'frame="base"' not in text and 'frame:`base`' not in text:
        if "frame" not in text or "base" not in text:
            errors.append("hold-protocol.md 缺少 frame=base 說明")
    for axis, direction, meaning in [
        ("x", "1", "左"),
        ("x", "-1", "右"),
        ("y", "-1", "前"),
        ("y", "1", "後"),
        ("z", "1", "上"),
        ("z", "-1", "下"),
    ]:
        # 協定表用 backtick 包 axis／dir
        if f"`{axis}`" not in text or f"`{direction}`" not in text:
            errors.append(
                f"hold-protocol.md 可能缺少 {meaning} 的 axis={axis} dir={direction} 對照"
            )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--app-dir",
        type=Path,
        default=Path(__file__).resolve().parents[1],
        help="teleop-app 根目錄（預設：本檔上一層）",
    )
    args = parser.parse_args()
    root: Path = args.app_dir.resolve()
    errors: list[str] = []

    index_html = root / "index.html"
    app_js = root / "app.js"
    protocol = root / "docs" / "hold-protocol.md"
    for p in (index_html, app_js):
        if not p.is_file():
            errors.append(f"缺少檔案 {p}")

    if not errors:
        check_html(index_html, errors)
        check_app_js(app_js, errors)
        check_protocol_doc(protocol, errors)

    if errors:
        print("FAIL：軸向／frame 合約檢查未過")
        for e in errors:
            print(f"  - {e}")
        return 1

    print("PASS：App 六方向與 frame:\"base\" 符合 hold-protocol；無 tool／IMU 控制路徑")
    print("若同學左移卻往上跑，請對 S4 log 的 frame= 與 EE Δ（見 docs/so-arm-classmate-issues.md）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
