# Recording Spec v1（D-5）

狀態：Draft for Leo／Thomas review｜更新：2026-10-19

## 目的與範圍

按鈕、IMU 與 WebXR 可以送不同的原始命令，但輸出的訓練資料必須使用同一份
episode contract，避免比較結果反映格式差異而非裝置差異。原始手機輸入不是
learning action；若為除錯而保留，只能放在 canonical dataset 之外的 transport log。

## Canonical episode contract

- 固定 **30 Hz**；第 `t` 列是送出 `action[t]` 前的 observation。
- Source 使用既有 S3-v1 HDF5 `data/demo_N`；轉檔後使用下表 LeRobot key。
- 所有裝置使用相同 shape、單位、關節順序與 episode 邊界。

| LeRobot key | Source | dtype／shape | 固定語意 |
|---|---|---|---|
| `observation.state` | `obs/joint_pos` | `float32[6]` | 六關節實際角度，rad |
| `observation.ee_pose` | `obs/ee_pose` | `float32[7]` | `[x,y,z,qw,qx,qy,qz]`，base frame，m |
| `observation.images.front` | `obs/front_rgb` | `uint8[H,W,3]` | RGB 固定視角影像 |
| `action` | `actions/joint_target` | `float32[6]` | **Server 完成 IK 後實際送給 actuator 的關節目標，rad** |
| `task` | episode metadata | string | 固定自然語言任務描述 |

六關節順序固定為 `shoulder_pan, shoulder_lift, elbow_flex, wrist_flex,
wrist_roll, gripper`。`p_rel_m`、`q_rel`、按鈕方向與其他裝置原始輸入不得映射成
LeRobot `action`。

## Episode 邊界與停頓幀

- Reset 完成、相機有效、手臂位於固定初始狀態後進入 armed。
- **第一個有效操作前的等待裁掉**；第一個有效操作成為 frame 0。
- **frame 0 之後的停頓全部保留**，直到 success、Estop、斷線、手動停止或 timeout。
- 此規則對 buttons／IMU／WebXR 完全相同；不得針對某個裝置另外裁切。
- 成功 episode 可進訓練集；失敗 episode 必須標原因且不得混入成功訓練集。

## 每條示範必填 metadata

| 欄位 | 規則 |
|---|---|
| `recording_spec_version` | 固定 `d5-v1` |
| `session_id`, `episode_id` | 跨 App、Server、HDF5、events 的共同識別 |
| `teleop_mode` | `buttons`、`imu` 或 `webxr` |
| `operator_id` | 匿名代碼，不存姓名、Email、IP 或手機序號 |
| `attempt_index` | 該操作者在該條件的第幾次嘗試，從 1 開始 |
| `condition_order` | 該操作者的實驗條件順序，從 1 開始 |
| `task_id`, `task_instruction` | 任務版本與固定文字描述 |
| `environment`, `robot_id` | `simulation`／`physical` 與 SO-101 實例 |
| `success`, `termination_reason` | 成功與否及 `task_success`／`estop`／`disconnect` 等原因 |
| `num_frames`, `fps` | frame 數；`fps` 固定 30 |
| `control_params` | 當次示範實際使用的增益、死區、平滑與按鈕檔位 |
| `app_version`, `protocol_version`, `controller_version`, `recorder_version`, `git_commit` | 可重現版本 |

`control_params` 不得省略。Buttons 沒有姿態增益／死區，需明寫
`rotation_gain=null`、`rotation_deadband_deg=null`、`rotation_smoothing=null`，並記錄
`step_mode=fine|coarse`。IMU／WebXR 必須記錄按下當下 App 實際送出的值；目前預設
為 `rotation_gain=0.7`、`rotation_deadband_deg=2.5`、
`rotation_smoothing=0.3`。WebXR 另記 Server 實際採用的 `position_gain`。

## 責任邊界

- **App**：送原始命令、模式與實際 control parameters。
- **Mac relay**：透明轉送，不改命令、不重試、不決定 frame。
- **Server／Controller**：建立 session／episode 關聯、執行 IK，產生唯一標準
  `joint_target[6]`。
- **Recorder**：以 30 Hz 寫 observation、Server `joint_target`、影像與 metadata。
- **Converter**：只把 Server `joint_target` 映射為 LeRobot `action`，不得改成手機原始輸入。

## D-5 驗收（DoD）

1. 本文件位於 `teleop-app/docs/recording-spec-v1.md`，Leo 與 Thomas 都 review。
2. 使用 **buttons** 模式錄製一條成功示範。
3. Source HDF5 通過欄位、shape、30 Hz、時間遞增、非空影像與 metadata 驗證。
4. 轉成 LeRobot 後 readback：上述五個 key 存在，frame 數一致，`action.shape=[N,6]`。
5. 只有完成第 2–4 項後，D-5 才能標為完成；規格與本機單元測試本身不等於實錄證據。
