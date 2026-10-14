# 給 Leo：姿態模式與 HTTPS 轉接的對接說明

更新：2026-10-14（Thomas）

分支：`claude/elegant-hopper-shmlgp`，還沒併進 `main`。

## 這次 App 改了什麼

1. **姿態模式（陀螺儀）**：按住「按住轉手機」時，用手機轉動控制末端轉向；平移照舊用方向鍵。新增三個訊息 `orient_start`／`orient_update`／`orient_stop`，格式見 [hold-protocol.md](hold-protocol.md) 的「Orientation (IMU) mode — DRAFT」一節。**這是草案，請你看過再定。**
2. **HTTPS＋wss 轉接**：手機只在 HTTPS 頁面給陀螺儀資料，而 HTTPS 頁面不能連 `ws://`。所以 `serve_https.py` 在同一個 port 8443 同時提供頁面和 `wss://…/relay`，把訊息原封不動轉給你的 `ws://` server。**你的 server 不用支援 TLS。**
3. **`xr-test.html`**：WebXR 6 自由度追蹤測試頁，評估 AR 對照組用的，跟你的 server 無關。

## 你的 server 要改的

| 項目 | 現況 | 要改成 |
|---|---|---|
| 協定版本 | 只收 `v=0`，App 送 `v=1` 會回 `unsupported_version` | 收 `v=1`（09-19 Sprint 3 review 定案） |
| 姿態模式 | 不認得 `orient_*` | 照草案實作，見下方 |

`orient_*` 的行為，`stub_server.py` 有一份可以跑的參考實作（搜尋 `orient`）：

- `orient_start`：跟其他 `*_start` 一樣佔用「唯一的按住」；急停中回 `rejected`。這時記下末端姿態 `e0`。
- `orient_update`：約 30 Hz，**同時當 keepalive**（不會另外送 `jog_keepalive`），750 ms 看門狗照舊；不用回 ack。`payload.q_rel` 是 `[x, y, z, w]`，代表按下後手機轉了多少，用手機自己的座標軸表示。
- `orient_stop`：跟其他 stop 一樣，先回 stop 的 ack（`orient_stop_requested`），再回 start 的最終 ack（`orient_stopped`）；看門狗逾時則是 `orient_watchdog_timeout`。

**需要你決定的**（草案只是提議）：

1. 手機座標對到手臂底座座標的固定旋轉 `R`。
2. 3 軸轉動怎麼投影到 SO-101：草案提議俯仰對應 `wrist_flex`、翻滾對應 `wrist_roll`、水平轉向先不用。
3. IK 解不出來時怎麼處理：草案提議停在最後一個解得出來的姿態，並在 `state.ik` 回報。

## 在你自己電腦上測（不需要 Thomas 在場）

手機和電腦要連同一個 Wi-Fi。

### 1. 拿程式碼

```bash
git clone https://github.com/WIDM/teleop-app.git   # 已經 clone 過就跳過這行
cd teleop-app
git fetch origin
git checkout claude/elegant-hopper-shmlgp
pip3 install "websockets>=13"
```

### 2. 讓你的手機信任你電腦的憑證（只要做一次）

手機的陀螺儀只給 HTTPS 頁面，所以要一張手機信任的憑證。macOS 的做法：

```bash
brew install mkcert
mkcert -install
mkdir -p .cert
mkcert -cert-file .cert/cert.pem -key-file .cert/key.pem $(ipconfig getifaddr en0) localhost
open "$(mkcert -CAROOT)"
```

- 把開出來的資料夾裡的 **`rootCA.pem`** 用 AirDrop 傳到 iPhone。**不要傳 `rootCA-key.pem`**，那是私鑰。
- iPhone：「設定 → 一般 → VPN 與裝置管理」安裝描述檔，再到「設定 → 一般 → 關於本機 → 憑證信任設定」把 mkcert 那一項打開。
- 換網路、IP 變了，就重跑 `mkcert -cert-file …` 那一行。手機上的根憑證不用重裝。
- 確認方法：Safari 開 `https://<你電腦IP>:8443/`，網址列有鎖頭就成功了。

### 3. 啟動

```bash
# 終端機 1：你的 server，照常開在 8765
# （還沒改好之前，可以先用 python3 stub_server.py 代替）

# 終端機 2：頁面＋wss 轉接，預設轉給本機 ws://127.0.0.1:8765
python3 serve_https.py
```

你的 server 不在本機的話，改成 `python3 serve_https.py --upstream ws://<IP>:8765`。

### 4. 手機

開 `https://<你電腦IP>:8443/`，server 欄會自動帶入 `wss://<你電腦IP>:8443/relay`，按「連線」。

## 驗收清單

- [ ] 送 `hello` 收到 `welcome`，右上角變「可操作」（代表 `v=1` 通了）
- [ ] 按住方向鍵：`jog_start`、keepalive、`jog_stop` 和兩個 ack 都正常（這是既有功能的回歸測試）
- [ ] 按「啟用陀螺儀」並允許，紫色鈕的小字變成「就緒，按住才生效」
- [ ] 按住紫色鈕轉手機，手臂轉向會跟著動；放開就停
- [ ] 按住紫色鈕時按急停，手臂立刻停，之後不再收到 `orient_update`
- [ ] 按住時把 App 切到背景，750 ms 內停止
- [ ] 想看每一則 `orient_update`：在「訊息紀錄與測試」勾「顯示高頻訊息」

有問題的話，把「訊息紀錄與測試」的畫面截圖，連同你 server 的 log 一起傳給 Thomas。
