# SO-ARM 測試問題：是不是 Thomas App？

對照三層程式後的診斷結論與對同學的核對清單。

- 本 repo（Thomas 手機 App）：[`app.js`](../app.js)、[`docs/hold-protocol.md`](hold-protocol.md)
- 筆電鍵盤：[Teleopration_laptop](https://github.com/LEO5166888/Teleopration_laptop)
- Isaac S4：[Teleopration_SO-101/output/s4_pick_place](https://github.com/LEO5166888/Teleopration_SO-101/tree/main/output/s4_pick_place)
- Phone relay：SO-101 `output/a3_move_delta/mac_phone_relay.py`（純轉發，不改 axis／frame）

**總結論：同學回報的四點都不像 App 軸向／映射 bug。** 主因在 S4 的 tool frame、三關節 IK、與 watchdog／斷線急停設計。

靜態合約檢查（本機可跑）：

```bash
python3 analysis/verify_axis_contract.py
```

---

## 系統分層

```text
手機 App :8765 ──► mac_phone_relay（透明）──┐
                                            ├─► a3_transport Bridge ──► s4_sim_pick_place
筆電 keyboard :8766（SSH tunnel）───────────┘
```

| 層 | 做什麼 |
|---|---|
| Thomas App | 按住方向鍵送 `jog_start`，**永遠** `frame:"base"`；**不用**手機 IMU |
| 學生鍵盤 | 同樣 hold jog，鎖定 `fine` + `base` |
| Phone relay | 純轉發文字 |
| Isaac S4 | 解 IK、套 tool frame、workspace、急停 |

協定／App／鍵盤軸向一致：左 `x+1`、右 `x-1`、前 `y-1`、後 `y+1`、上 `z+1`、下 `z-1`。

`orientation.html` 只是感測器 demo，沒接手臂。描述「手機平移／傾斜打架」時，要先確認是不是走了 Leo 的 `pose_*`／`orient_*`，或進階 Mac 切到了 `frame:"tool"`。

---

## 四點判定

### 1. 用夾爪末端當移動中心（避開關節限制）

**不是 App bug。** S4 末端本來就是 `EE_BODY_NAME = "gripper"`，Cartesian IK 只解 `shoulder_pan`／`shoulder_lift`／`elbow_flex`。App 與學生鍵盤都送 `frame:"base"`。進階 `mac_hold_teleop.py` 可用 `M` 切 `tool`；學生鍵盤關掉這類鍵。切 tool 會讓方向隨夾爪姿態轉，關節限制仍在，還會加重第 3、4 點。

### 2. 安全機制很容易觸發急停

**主要是 Bridge 設計。** 真急停：使用者按急停、按住中斷線（`disconnect_estop`）、750 ms 沒 keepalive（watchdog）、指令與實際 EE 差 > 10 cm（`tracking_safety_stop`）、IK 非有限值。workspace ±8 cm clamp **不是**急停，但會像卡住。App 進背景／失焦會停 hold，斷線時 server 再升級成 estop——這是協定要求。

### 3. 平移跟傾斜打架、斜移很誇張

**按鈕 App 做不到平移＋傾斜混送。** 一次只能 hold 一鍵；S4 三關節位置 IK 會讓末端姿態在平移時漂。若是 `frame:"tool"`，或走 `pose_*`／`orient_*`，才會明顯耦合。

### 4. 方向顛倒、左移卻往上跑

**不像 App 軸表寫反。** 典型成因：`tool` frame 且夾爪已傾；WebRTC 視角不是主操作視角（協定假設 +X ≈ 螢幕左）；同時開鍵盤＋App。

| 同學現象 | 歸責 |
|---|---|
| 1 改夾爪末端當中心 | S4／操作（tool frame） |
| 2 急停太容易 | Bridge + 網路／焦點 |
| 3 平移傾斜打架 | S4 IK／tool／或 pose 模式 |
| 4 方向錯／失控 | 視角或 tool 或雙控制端 |

---

## Todo：確認當時客戶端

跟同學問這四題，並對照下面指紋：

1. 用的是 Thomas App、學生鍵盤（`start_keyboard.sh`），還是進階 Mac hold？
2. 有沒有按過 `M`（切 base／tool）？學生鍵盤不應能切。
3. 有沒有同時開鍵盤＋App？
4. 看的 WebRTC 是主操作視角，還是跟隨夾爪／側視？

| 客戶端 | 典型指紋 |
|---|---|
| Thomas App | `hello.payload.name = "thomas-app"`；每次 `jog_start.frame = "base"`；無 `M` |
| 學生鍵盤 | 連 `ws://127.0.0.1:8766`；`frame:"base"`；進階鍵被擋 |
| 進階 mac_hold | 可按 `M`；log 可能出現 `frame=tool` |
| pose／orient | 訊息型別是 `pose_*`／`orient_*`，不是 `jog_*` |

把答案記在下方（填完就把這次現象對上表）：

```text
日期：
操作者：
客戶端：
有無按 M / tool：
有無雙開鍵盤+App：
Sim 視角：
備註：
```

---

## Todo：對一次「左移」對 log

在**單一客戶端、frame=base、主視角**下重現一次：

1. 解除急停，閘門「可操作」。
2. 短暫按住「左」（App）或 `A`（鍵盤）。
3. 對三邊 log：

| 來源 | 預期 |
|---|---|
| App 訊息紀錄／鍵盤 Terminal | `jog_start`：`axis:"x"`, `dir:1`, `step:"fine"`, `frame:"base"` |
| relay jsonl（若走手機） | 同上，原文轉發 |
| S4／Bridge log | `frame=base`；EE 的 **x 變大**（朝綠目標／主視角左），不是 z 為主 |
| 若觸發急停 | reason 應是 `disconnect_estop`／`*_watchdog_timeout`／`tracking_safety_stop`／使用者 `estop` 之一 |

判讀：

- App／鍵盤送出正確，但 Sim 往上跑 → **不是 App**；查 `frame=tool`、視角、或 IK／safety。
- App 送出就不是 `x/+1/base` → 才回頭查本 repo（目前靜態檢查應擋下這種回歸）。
- 急停 reason 是 watchdog／disconnect → 網路、背景、失焦、雙控，不是軸向 bug。

本機可先確認 App 合約沒漂：

```bash
python3 analysis/verify_axis_contract.py
```

本 repo 已用上述腳本確認六方向與 `frame:"base"` 無漂移；實機「左移」的 EE Δ 仍需在實驗室對 S4 log（本環境沒有 Isaac／SO-ARM）。

---

## Todo：App UX（預設不做）

計畫預設**不改** `app.js`／UI。方向錯與急停敏感度不靠 App 改 frame 或放寬安全能修好；那些在 Leo 的 S4／Bridge。

若之後真的要降低誤用，再考慮（另開票）：紀錄區標出每次 `jog_start` 的 axis／frame、連線說明加「勿同時開鍵盤」、或明確寫死／顯示 `frame=base`。
