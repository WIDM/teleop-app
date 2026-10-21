# 斷線急停與重連（情境 4）

按住中網路斷掉：stub 進急停，頁面自動重連後仍在急停，不自動續按。

## Sub-features

- stub 對照 Leo：控制中的連線斷掉就把急停旗標設為 true，且跨連線保留。
- 頁面 `onclose` 清掉本地按住，1 秒後自動重連，重連後拿到 welcome 與 `estop=true` 的 state。
- 解除急停後才回「可操作」。

## How to get to it (user POV)

按住方向鍵時手機網路中斷（或 server 掛掉），等它重連。

## Driving it with Playwright＋CDP 觸控

`python scripts/verify.py --scenario 4`

1. 連線；手指壓 z+，等 `jog_start` 與 `jog_keepalive`。
2. `proxy.drop_clients()`：stub 那條關掉，頁面那條 abort。
3. 等 `#gate`＝「急停中，按「解除急停」」（只有重連成功才會出現，逾時 15 秒）。
4. 通過條件：新連線（`conn` 不同）送了 `hello`；新連線 wire down 有 `state` 且 `estop=true`；之後 1.5 秒沒有新的 `jog_start`（手指仍壓著）；`.hold` 全 disabled。
5. 放開手指，click `#estopResetBtn`，`#gate` 回「可操作」。
6. 截圖 `s4-reconnected-estop.png`、`s4-after-reset.png`。

## Gotchas

- 重連退避第一次是 1 秒；若 app 改了退避，逾時要跟著調。
- 這個情境靠 stub 的 `finally` 區塊設急停，proxy 必須真的關掉 stub 那條連線（不能只 abort 頁面那條）。
