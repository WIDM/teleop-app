# thesis-teleop-app

手機語音遙操作碩論的 Web App 前端。目前為 A-1 骨架：連線狀態、WebSocket client、信封格式（介面協定 v0）。

## 本地測試

1. 啟動 echo stub（`hello` 回 `welcome`，其他原樣 echo）：

   ```bash
   pip install websockets
   python stub_server.py
   ```

2. 起靜態頁伺服器：

   ```bash
   python -m http.server 8000
   ```

3. 手機連同一個 Wi-Fi，瀏覽器開 `http://<電腦IP>:8000`，server 欄填 `ws://<電腦IP>:8765`，按連線。

4. 驗收：連上自動送 `hello` 收 `welcome`；按「送 test」收原樣 echo；關掉 stub 看狀態變「斷線重連中」，重開 stub 自動連回。

## 對接 Leo 的 server

把 server 欄的網址換成他的 server 即可，訊息格式同一份合約（vault：《介面協定 v0 Message Schema》）。
