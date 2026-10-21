# 重置場景（情境 5）

按「重置場景」，手臂末端位置回到初始值。

## Sub-features

- `reset_scene` 的 ack（reason `scene_reset`）。
- state 的 `ee_pos` 回到 `[0.02, -0.14, 0.16]`（公尺）。
- `#stateBar` 顯示公分值 `ee(cm) 2.0, -14.0, 16.0`。

## How to get to it (user POV)

手臂被移動後，按「重置場景」。

## Driving it with Playwright＋CDP 觸控

`python scripts/verify.py --scenario 5`

1. 連線；壓住 z+ 直到 wire down 的 state `ee_pos[2] > 0.18`，放開（確認真的移動過）。
2. 滑鼠 click `#resetSceneBtn`。
3. 通過條件：ack `scene_reset`；ack 之後第一則 state 的 `ee_pos` 與初始值相差 < 1e-6；`#stateBar` 含「ee(cm) 2.0, -14.0, 16.0」。
4. 截圖 `s5-moved.png`、`s5-after-reset-scene.png`。

## Gotchas

- 先移動再重置才有意義；沒移動過的重置無法分辨「重置了」與「本來就在原點」。
- 只驗位置，不驗夾爪與其他欄位。
