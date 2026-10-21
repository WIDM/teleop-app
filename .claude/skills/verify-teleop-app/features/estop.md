# 急停（情境 1）

按住方向鍵的同時按「急停 STOP」，手臂要立刻停、不能再有 keepalive。

## Sub-features

- 本地先停 keepalive 再送 `estop`（`fireEstop` 裡的 `endHold({ sendStop: false })`）。
- stub 回兩個 ack：原 `jog_start` 的最終 ack（reason `estopped`），與 `estop` 的 ack（reason `estop_engaged`）。
- UI 鎖住：`#gate` 顯示急停中、所有 `.hold` disabled、`#estopResetBtn` enabled。

## How to get to it (user POV)

連上後，一根手指按住「上」（z+），另一根手指按紅色「急停 STOP」。

## Driving it with Playwright＋CDP 觸控

`python scripts/verify.py --scenario 1`

1. 連線，等 `#gate`＝「可操作」。
2. 手指 1 `touchStart` 在 `.hold.move[data-axis="z"][data-dir="1"]`，等 wire up 出現 `jog_start` 再出現 `jog_keepalive`。
3. 手指 2 `touchStart` 在 `#estopBtn`，等 wire up `estop` 及其 ack，再多觀察 1.2 秒。
4. 通過條件：`estop` 之後 wire up 沒有 `jog_keepalive`；ack `estop_engaged`；原 `jog_start` 的 ack reason＝`estopped`；`#gate`＝「急停中，按「解除急停」」；所有 `.hold` disabled；`#estopResetBtn` enabled。
5. 截圖 `s1-holding.png`、`s1-estop.png`。

## Gotchas

- 反向測試：`scripts/mutation.py` 拿掉 `endHold({ sendStop: false });` 後本情境必須 FAIL。
- 需要 proxy 的 400 ms 下行延遲，反向測試才穩（見 SKILL.md）。
- 需要兩根手指，滑鼠只有一個指標做不到，所以用 CDP 多點觸控。
