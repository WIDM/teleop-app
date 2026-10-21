---
name: verify-teleop-app
description: 驗證 teleop-app（手機遙操作網頁，純靜態 HTML/JS，連 WebSocket stub）的急停與重置行為。用真 Chromium＋錄音 proxy 驅動頁面，判準看 wire 與 DOM。改了 app.js 的急停、按住控制、重連、reset_scene 後，或要證明這些行為沒壞時使用。
---

# verify-teleop-app

Surface：瀏覽器網頁（`index.html` ＋ `app.js`），server 端用 repo 自帶的 `stub_server.py`（模擬 Leo 的 relay）。沒有 build、沒有相依套件需求，唯一前提是 python 環境有 `playwright`、`websockets`，且 Chromium 已裝（`PLAYWRIGHT_BROWSERS_PATH`）。

## 一個指令跑完

```bash
timeout 900 python .claude/skills/verify-teleop-app/scripts/verify.py                 # 全部五個情境
timeout 900 python .claude/skills/verify-teleop-app/scripts/verify.py --scenario 1    # 單一情境
timeout 900 python .claude/skills/verify-teleop-app/scripts/verify.py --doctor-only   # 只做 Doctor
timeout 900 python .claude/skills/verify-teleop-app/scripts/mutation.py               # 反向測試
```

參數：`--scenario all|1|2|3|4|5`、`--app-dir`（預設 repo 根目錄）、`--out`（預設 `$VERIFY_TELEOP_OUT/<時間戳>`，沒設環境變數就用系統暫存目錄下的 `verify-teleop/`）、`--down-delay-ms`（預設 400，見 Gotchas）。任一檢查失敗 exit 1。

## Launch

腳本自己做，不用手動起：
- `python stub_server.py`（cwd＝app-dir，ws **8765 寫死**）。每個情境重啟一次，確保 stub 狀態（急停旗標、手臂位置）乾淨。Ready＝能連上 ws 8765。
- `python -m http.server <port> --bind 127.0.0.1`（port 從 8800 起找沒被占用的）。
- 內建錄音 WebSocket proxy（port 從 http port+1 起找空的）：頁面 `#serverUrl` 填 proxy，proxy 轉給 stub，雙向訊息寫進 wire。
- headless Chromium，viewport 420x1100、`has_touch`。

不能與另一個正在跑的 stub 並行（8765 寫死）。Doctor 發現 8765 被占就失敗，不要去殺別人的行程。

## Doctor

唯讀檢查，先跑；情境前自動執行：app-dir 有 `index.html`／`app.js`／`stub_server.py`；ws 8765 沒被占用；http 伺服器回 200；頁面存在所有要用的 selector（`#serverUrl #connectBtn #gate #stateBar #estopBtn #estopResetBtn #resetSceneBtn` 與 z+、x+ 方向鍵）。任一項失敗就不跑情境。

## Drive

- 連線：`goto index.html` → `fill #serverUrl` → `click #connectBtn` → 等 `#gate` 文字＝「可操作」。
- 按住：用 CDP `Input.dispatchTouchEvent` 做真的觸控 pointerdown，`touchStart` 一根手指壓 `.hold.move[data-axis="z"][data-dir="1"]`；第二根手指壓 `#estopBtn`（app 設計上就是另一指還按著方向鍵時也要能急停）。放手用 `touchEnd`。
- 一次性按鈕（`#estopResetBtn`、`#resetSceneBtn`、急停前置）用滑鼠 click。
- 斷線：proxy 對頁面那條 socket 直接 abort，stub 那條關掉。
- 判準一律看 wire（`jog_start`、`jog_keepalive`、`estop`、ack 的 `reason`、`state.estop`、`state.ee_pos`）與 DOM（`#gate`、`.hold` disabled、`#estopResetBtn`、`#stateBar`）。不讀 app 內部變數。

五個情境見 `features/README.md`。

## Evidence

寫到 `<out>/`（預設 `$VERIFY_TELEOP_OUT/<時間戳>/`）：
- `wire.jsonl`：每行 `{dir: up|down, t: 毫秒時間, scn: 情境, conn: 連線編號, raw: 原始 JSON}`。`up`＝頁面送 stub，`down`＝stub 送頁面。
- `s<N>-*.png`：情境 N 的關鍵步驟截圖。
- `result.json`：doctor、每情境每項檢查的 ok／detail。
- `stub.log`、`http.log`。

證明標準：走真實使用者路徑（觸控、按鈕），不用內部 setter；同時抓動作（wire up）與結果（wire down＋DOM）；stub 是 production 邊界外的假 server，唯一的 mock。

## Cleanup

腳本結束（含失敗、例外）時只終止自己啟的 stub 與 http 行程（記 PID），關 proxy 與瀏覽器。證據資料夾不刪。`mutation.py` 的突變複本留在 `$VERIFY_TELEOP_OUT/mutant-<時間戳>/` 當證據，不在 repo 內。

## Helpers

- `scripts/verify.py`：上面全部。
- `scripts/mutation.py`：把 app 複製到 `mutant-<時間戳>/`，刪掉 `fireEstop` 裡的 `endHold({ sendStop: false });`，跑情境 1 必須 FAIL（且是「keepalive 沒停」那項失敗），再用原版跑情境 1 必須 PASS，兩者都符合 exit 0。

## Gotchas

- proxy 對 stub→頁面方向預設加 400 ms 延遲。沒有延遲時 `estopped` ack 幾乎立刻回來，`handleAck` 會順手結束按住，「先停 keepalive」拿掉後 keepalive 的空窗只剩幾毫秒，反向測試會不穩。400 ms 只是為了拉開觀察窗口，不代表真實網路（區網通常只有幾毫秒）。正常版在 `--down-delay-ms 0` 下五個情境也要全過，改了延遲或判準後兩種都要跑。
- stub 的急停旗標跨連線保留，所以情境 4 重連後仍是急停；每個情境重啟 stub 才互不污染。
