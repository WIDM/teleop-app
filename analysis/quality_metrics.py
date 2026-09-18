#!/usr/bin/env python3
"""Demonstration quality metrics for a LeRobotDataset v3 folder.

Reads data/**/*.parquet plus meta/info.json and writes one CSV row per episode:

    episode_index, n_frames, duration_s, path_length,
    jerk_mean, jerk_rms, jerk_max, pause_time_s, pause_ratio, key, dims

- duration_s: (n_frames - 1) / fps, the time spanned by the samples.
- path_length: sum of |p[t+1] - p[t]| over the selected dims (units of the data).
- jerk: third time derivative of position (finite differences at 1/fps).
  jerk_mean / jerk_rms / jerk_max are over the per-frame jerk vector norm.
- pause: frames whose speed stays below --pause-speed for at least --min-pause seconds.
  Speed for pause detection is taken after a --pause-window moving average, so
  sensor noise alone does not break a pause.
- --smooth N applies an N-frame moving average before path length and jerk;
  it drops N-1 edge frames rather than padding them.

Join key for other per-episode logs (robot-side counters etc.) is `episode_index`.

Column choice when --key is omitted: observation.state if it has end-effector
columns; otherwise the first feature whose name contains `ee_pose`/`eef_pose`
(e.g. observation.ee_pose); otherwise observation.state joint angles.

Usage:
    python quality_metrics.py <dataset_root> [-o metrics.csv]
    python quality_metrics.py <root> --key observation.ee_pose --dims ee_x,ee_y,ee_z
    python quality_metrics.py <root> --dims 0,1,2 --smooth 5
"""

from __future__ import annotations

import argparse
import json
import math
import sys
from pathlib import Path

import numpy as np
import pandas as pd

# Name fragments that mark end-effector position columns, checked in order.
EE_NAME_HINTS = ("ee_pos", "eef_pos", "ee_", "eef_")
# Feature-name fragments for a separate end-effector pose feature.
EE_FEATURE_HINTS = ("ee_pose", "eef_pose")
DEFAULT_KEY = "observation.state"
CSV_COLUMNS = [
    "episode_index",
    "n_frames",
    "duration_s",
    "path_length",
    "jerk_mean",
    "jerk_rms",
    "jerk_max",
    "pause_time_s",
    "pause_ratio",
    "key",
    "dims",
]


def load_dataset(root: Path) -> tuple[dict, pd.DataFrame]:
    info_path = root / "meta" / "info.json"
    if not info_path.exists():
        sys.exit(f"找不到 {info_path}：這不是 LeRobotDataset 資料夾")
    info = json.loads(info_path.read_text())
    files = sorted((root / "data").glob("**/*.parquet"))
    if not files:
        sys.exit(f"{root / 'data'} 底下沒有 parquet")
    df = pd.concat((pd.read_parquet(f) for f in files), ignore_index=True)
    return info, df


def feature_names(info: dict, key: str) -> list[str]:
    feature = info.get("features", {}).get(key)
    if feature is None:
        sys.exit(
            f"meta/info.json 沒有 feature `{key}`，可用：{list(info.get('features', {}))}"
        )
    width = int(np.prod(feature["shape"]))
    names = feature.get("names") or [str(i) for i in range(width)]
    if isinstance(names, dict):  # some datasets store {"motors": [...]}
        names = next(iter(names.values()))
    return [str(n) for n in names]


def has_ee_columns(names: list[str]) -> bool:
    return any(h in n.lower() for h in EE_NAME_HINTS for n in names)


def choose_key(info: dict, key_arg: str | None) -> str:
    """--key wins; else state with EE columns; else a separate *ee_pose feature."""
    if key_arg:
        return key_arg
    features = info.get("features", {})
    if DEFAULT_KEY in features and has_ee_columns(feature_names(info, DEFAULT_KEY)):
        return DEFAULT_KEY
    for name in features:
        if any(h in name.lower() for h in EE_FEATURE_HINTS):
            return name
    return DEFAULT_KEY


def resolve_dims(
    info: dict, key: str, dims_arg: str | None
) -> tuple[list[int], list[str]]:
    names = feature_names(info, key)

    if dims_arg:
        idx = []
        for token in dims_arg.split(","):
            token = token.strip()
            if token.isdigit():
                idx.append(int(token))
            elif token in names:
                idx.append(names.index(token))
            else:
                sys.exit(f"`{token}` 不在 {key} 的 names 裡：{names}")
        return idx, [names[i] for i in idx]

    for hint in EE_NAME_HINTS:
        idx = [i for i, n in enumerate(names) if hint in n.lower()]
        if idx:
            return idx[:3], [names[i] for i in idx[:3]]
    if any(h in key.lower() for h in EE_FEATURE_HINTS):
        # Named pose columns such as x, y, z, qw...: pick x, y, z by name.
        lower = [n.lower() for n in names]
        if all(a in lower for a in ("x", "y", "z")):
            idx = [lower.index(a) for a in ("x", "y", "z")]
            return idx, [names[i] for i in idx]
        # Unnamed pose columns: position is the first three (quaternion follows).
        if not info["features"][key].get("names") and len(names) >= 3:
            return [0, 1, 2], names[:3]
        # Named but unrecognised: guessing could mix quaternion into path length.
        sys.exit(f"{key} 的欄名 {names} 認不出位置欄，用 --dims 指定三個位置欄")
    # No end-effector columns: fall back to every non-gripper dim (joint-space jerk).
    idx = [i for i, n in enumerate(names) if "gripper" not in n.lower()]
    if not idx:
        sys.exit(
            f"{key} 只有夾爪欄 {names}，沒有位置可算；用 --key 換欄位或 --dims 指定"
        )
    return idx, [names[i] for i in idx]


def moving_average(p: np.ndarray, window: int) -> np.ndarray:
    # "valid" mode drops window-1 edge frames instead of padding; padding
    # creates an artificial kink at both ends that shows up as a jerk spike.
    if window <= 1 or len(p) < window:
        return p
    kernel = np.ones(window) / window
    return np.stack(
        [np.convolve(p[:, d], kernel, mode="valid") for d in range(p.shape[1])],
        axis=1,
    )


def pause_intervals(slow_flags: np.ndarray, min_frames: int) -> int:
    total, run = 0, 0
    for slow in slow_flags:
        if slow:
            run += 1
            continue
        if run >= min_frames:
            total += run
        run = 0
    if run >= min_frames:
        total += run
    return total


def episode_metrics(
    raw: np.ndarray,
    fps: float,
    smooth: int,
    pause_speed: float,
    min_pause_s: float,
    pause_window_s: float,
) -> dict:
    dt = 1.0 / fps
    n = len(raw)
    p = moving_average(raw, smooth)
    step = np.diff(p, axis=0)
    # Pause detection needs gross motion only; frame-to-frame sensor noise
    # alone can push speed over the threshold, so it always gets some smoothing.
    coarse = moving_average(raw, max(smooth, round(pause_window_s * fps)))
    slow = np.linalg.norm(np.diff(coarse, axis=0), axis=1) / dt < pause_speed
    if len(slow) == 0 and n > 1:
        slow = np.linalg.norm(np.diff(raw, axis=0), axis=1) / dt < pause_speed
    # Smoothing trimmed edge intervals; extend the nearest flag so the pause
    # count covers all n-1 intervals of the episode.
    missing = max(n - 1 - len(slow), 0)
    slow = np.pad(slow, (missing // 2, missing - missing // 2), mode="edge")
    if len(p) >= 4:
        jerk = np.linalg.norm(np.diff(p, n=3, axis=0), axis=1) / dt**3
        jerk_mean, jerk_rms, jerk_max = (
            jerk.mean(),
            np.sqrt((jerk**2).mean()),
            jerk.max(),
        )
    else:
        jerk_mean = jerk_rms = jerk_max = float("nan")
    # ceil so a pause never qualifies below --min-pause; epsilon absorbs float
    # error such as 0.3 * 30 == 8.999999999999998.
    min_frames = max(1, math.ceil(min_pause_s * fps - 1e-9))
    paused = pause_intervals(slow, min_frames)
    # Time spanned by the samples, same interval basis as the pause count.
    duration = (n - 1) / fps if n > 1 else 0.0
    return {
        "n_frames": n,
        "duration_s": duration,
        "path_length": float(np.linalg.norm(step, axis=1).sum()),
        "jerk_mean": float(jerk_mean),
        "jerk_rms": float(jerk_rms),
        "jerk_max": float(jerk_max),
        "pause_time_s": paused / fps,
        "pause_ratio": paused / max(n - 1, 1),
    }


def compute(
    root: Path,
    key: str | None,
    dims_arg: str | None,
    smooth: int,
    pause_speed: float,
    min_pause_s: float,
    pause_window_s: float = 0.2,
) -> pd.DataFrame:
    info, df = load_dataset(root)
    fps = float(info["fps"])
    key = choose_key(info, key)
    idx, dim_names = resolve_dims(info, key, dims_arg)
    rows = []
    for ep, g in df.sort_values(["episode_index", "frame_index"]).groupby(
        "episode_index"
    ):
        raw = np.stack(g[key].to_numpy()).astype(np.float64)[:, idx]
        rows.append(
            {
                "episode_index": int(ep),
                **episode_metrics(
                    raw, fps, smooth, pause_speed, min_pause_s, pause_window_s
                ),
                "key": key,
                "dims": "|".join(dim_names),
            }
        )
    return pd.DataFrame(rows, columns=CSV_COLUMNS)


def main() -> None:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    ap.add_argument(
        "root", type=Path, help="LeRobotDataset v3 資料夾（含 meta/ 與 data/）"
    )
    ap.add_argument("-o", "--out", type=Path, default=Path("metrics.csv"))
    ap.add_argument(
        "--key",
        default=None,
        help="要算的欄位；不給就自動選：state 有 EE 欄用 state，否則找 *ee_pose 欄，再沒有才用 state 關節角",
    )
    ap.add_argument(
        "--dims",
        help="逗號分隔的 index 或 names；不給就自動找 ee 位置欄，找不到用全部非夾爪欄",
    )
    ap.add_argument("--smooth", type=int, default=1, help="移動平均視窗幀數，1＝不平滑")
    ap.add_argument(
        "--pause-speed",
        type=float,
        default=0.01,
        help="低於這個速度（資料單位/秒）算停頓",
    )
    ap.add_argument(
        "--min-pause", type=float, default=0.3, help="停頓至少要持續幾秒才算"
    )
    ap.add_argument(
        "--pause-window",
        type=float,
        default=0.2,
        help="判斷停頓前先平滑的秒數，壓掉逐幀雜訊",
    )
    args = ap.parse_args()

    out = compute(
        args.root,
        args.key,
        args.dims,
        args.smooth,
        args.pause_speed,
        args.min_pause,
        args.pause_window,
    )
    out.to_csv(args.out, index=False)
    with pd.option_context("display.width", 160, "display.max_columns", None):
        print(out.drop(columns=["key"]).round(4).to_string(index=False))
    used = out["key"].iloc[0] if len(out) else args.key
    print(f"\n{len(out)} episodes → {args.out}（欄位 {used}）")


if __name__ == "__main__":
    main()
