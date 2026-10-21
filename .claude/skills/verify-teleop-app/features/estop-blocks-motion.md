# 急停中不能動（情境 2）

急停狀態下按方向鍵，不能送出任何新的 `jog_start`。

## Sub-features

- 動作閘門：`motionAllowed()` 要已連、收過 welcome、最近 state `estop=false` 三者同時成立。
- 急停中 `.hold` 全部 disabled，`startHold` 也會擋。

## How to get to it (user POV)

按「急停 STOP」進急停後，再去按方向鍵。

## Driving it with Playwright＋CDP 觸控

`python scripts/verify.py --scenario 2`

1. 連線，用滑鼠 click `#estopBtn`，等 `#gate`＝急停中。
2. 兩根手指同時 `touchStart` 在 z+ 與 x+（disabled 的按鈕），等 1.2 秒。
3. 通過條件：此後 wire up 沒有 `jog_start`；`.hold` 仍全 disabled；`#gate` 仍是急停中。
4. 截圖 `s2-pressed-in-estop.png`。

## Gotchas

- 這個情境沒有按住前置，所以不依賴 proxy 延遲。
- 擋在 UI 層（disabled）與 `startHold` 兩層；此情境只驗 wire 結果，不分是哪一層擋的。
