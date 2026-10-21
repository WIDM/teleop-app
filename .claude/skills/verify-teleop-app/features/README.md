# teleop-app 功能地圖（急停與重置）

每個檔對應 `scripts/verify.py --scenario N` 的一個情境。每個情境各用全新 stub 與瀏覽器 context。

| # | 功能 | 檔案 |
| --- | --- | --- |
| 1 | 急停：按住中按急停，keepalive 立刻停 | [estop.md](estop.md) |
| 2 | 急停中不能動 | [estop-blocks-motion.md](estop-blocks-motion.md) |
| 3 | 解除急停（不自動恢復動作） | [estop-reset.md](estop-reset.md) |
| 4 | 斷線急停與重連 | [disconnect-estop.md](disconnect-estop.md) |
| 5 | 重置場景 | [reset-scene.md](reset-scene.md) |

尚未收錄：手腕鍵、夾爪鍵、粗調／微調、`hello`／`test` 按鈕、使用者主動斷線。
