# thesis-teleop-app

手機語音遙操作碩論的 Web App 前端（Thomas）。已完成 Sprint 1 的 App 骨架與 rule-based 按鈕介面：連線狀態、WebSocket client、信封格式（介面協定 v0）、六方向＋夾爪＋粗微調按鈕、ack 回饋。

- 訊息格式合約：《介面協定 v0 Message Schema》（已凍結 2026-09-02，改版走 PR）
- 純靜態頁，無 build 步驟；`stub_server.py` 是本地測試用的假 server

## Demo SOP

前置：一台電腦（跑頁面與 stub）、一支手機、兩者同一個 Wi-Fi。

1. 電腦上啟動假 server（stub）：

   ```bash
   pip install websockets
   python stub_server.py
   ```

2. 電腦上另開一個終端機，啟動靜態頁伺服器：

   ```bash
   python -m http.server 8000
   ```

3. 查電腦的區網 IP（macOS：`ipconfig getifaddr en0`）。
4. 手機瀏覽器開 `http://<電腦IP>:8000`。server 欄會自動帶 `ws://<電腦IP>:8765`，按「連線」。
5. Demo 動線：
   - 連上後右上角變綠色「已連」，紀錄區看到送 `hello`、收 `welcome`
   - 按「送 test」：收到原樣 echo
   - 按方向鍵（前後上下左右）：送 `move_delta`，收到 `ack` 該按鈕閃綠
   - 按「夾爪 開／關」：送 `gripper`
   - 按「粗調」切到「微調（0.5 cm）」：之後的 `move_delta` 帶 `step:"fine"`
   - 關掉 stub：狀態變「斷線重連中」；重開 stub 自動連回並重新握手
6. 連不上排查：macOS 防火牆詢問要按允許；確認手機跟電腦同網段（訪客 Wi-Fi 常是分開的）。

## 對接 Leo 的 server（取代 stub）

Leo 的 WebSocket server 起在跑 Isaac Sim 的那台機器上。對接當天：

1. 跟 Leo 要兩個資訊：那台機器的區網 IP 與 server port。
2. 手機連實驗室 Wi-Fi，照 Demo SOP 開頁面，server 欄改填 `ws://<那台機器IP>:<port>`，按連線。
3. 跑同一組驗收：`hello` 收 `welcome`、`test` 收 echo、按鈕指令收 `ack`，他的 server log 看得到正確 JSON。
4. App 程式碼不用改，換網址即完成對接；stub 之後只在單機開發時用。
