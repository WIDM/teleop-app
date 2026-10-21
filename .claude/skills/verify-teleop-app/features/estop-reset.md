# 解除急停（情境 3）

按「解除急停」恢復可操作，但之前按住的動作不會自動恢復。

## Sub-features

- `estop_reset` 的 ack（reason `estop_reset`）。
- 之後 state 廣播 `estop=false`，`#gate` 回「可操作」。
- 沒有自動續按：解除後 wire 不得出現新的 `jog_start`，重新按才有。

## How to get to it (user POV)

急停後按橘色「解除急停」，鬆手再按方向鍵。

## Driving it with Playwright＋CDP 觸控

`python scripts/verify.py --scenario 3`

1. 連線；手指 1 壓住 z+ 等 `jog_start`；手指 2 按 `#estopBtn` 等 `#gate`＝急停中；手指 2 放開，手指 1 仍壓著。
2. 滑鼠 click `#estopResetBtn`。
3. 通過條件：ack `estop_reset`；其後 wire down 有 `state` 且 `estop=false`；`#gate`＝「可操作」；之後 1.2 秒內 wire up 沒有新的 `jog_start`。
4. 手指 1 放開再壓一次 z+，必須出現新的 `jog_start`。
5. 截圖 `s3-after-reset.png`、`s3-pressed-again.png`。

## Gotchas

- 解除時手指 1 仍壓在按鈕上，這正是要驗「不自動續按」的地方。
