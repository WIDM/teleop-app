# Thomas App press-and-hold control protocol

Updated: 2026-09-09

This is the current phone control contract for Leo's SO-101 S4 simulator. The
Mac relay is transparent: it forwards text WebSocket messages without changing
or retrying them.

## Connection

- App endpoint: `ws://<LEO_MAC_LAN_IP>:8765`
- Leo's Mac and the phone must be on a network that allows direct LAN traffic.
- The Mac LAN IP may change after reconnecting to Wi-Fi. Do not permanently
  hard-code `192.168.50.13` without first checking it.
- Only one phone App connection is accepted by the Mac relay.
- Do not run the Mac keyboard controller while Thomas's App is controlling.

Every text message uses this envelope:

```json
{
  "v": 1,
  "type": "hello",
  "ts": 1788940000000,
  "seq": 1,
  "payload": {}
}
```

`ts` is Unix epoch time in integer milliseconds. `seq` must be a non-negative,
strictly increasing integer for every outbound message on one connection,
including keepalives and stop messages. Never reuse a sequence number after
reconnect within the same socket. Payload is always a JSON object.

## Handshake

The first message must be:

```json
{
  "v": 1,
  "type": "hello",
  "ts": 1788940000000,
  "seq": 1,
  "payload": {
    "client": "phone",
    "name": "thomas-app"
  }
}
```

The server replies with `welcome`, followed by `state`. Do not enable motion
buttons until `welcome` and a state with `estop=false` have been received.

## Translation buttons: press, hold, release

On `pointerDown`/`touchDown`, send exactly one `jog_start`:

```json
{
  "v": 1,
  "type": "jog_start",
  "ts": 1788940000100,
  "seq": 2,
  "payload": {
    "axis": "z",
    "dir": 1,
    "step": "fine",
    "frame": "base"
  }
}
```

Current base-frame button mapping:

| UI meaning | axis | dir |
|---|---:|---:|
| toward green target / left in the current view | `x` | `1` |
| away from green target / right in the current view | `x` | `-1` |
| farther onto table / forward | `y` | `-1` |
| toward robot base / backward | `y` | `1` |
| up | `z` | `1` |
| down | `z` | `-1` |

Use `frame="base"` as the default for S4. `frame="tool"` is supported, but
its directions rotate with the gripper orientation and are easier to misuse
near the table.

While any translation, wrist, or gripper hold is active, send this message
every 150 ms:

```json
{
  "v": 1,
  "type": "jog_keepalive",
  "ts": 1788940000250,
  "seq": 3,
  "payload": {}
}
```

The server watchdog is 750 ms. `jog_keepalive` deliberately has no ACK. Do not
wait for an ACK before sending the next keepalive or stop message.

On `pointerUp`, `pointerCancel`, view disappearance, App backgrounding, or lost
focus, immediately send:

```json
{
  "v": 1,
  "type": "jog_stop",
  "ts": 1788940000400,
  "seq": 4,
  "payload": {}
}
```

## Wrist flex buttons

Press and hold:

```json
{
  "v": 1,
  "type": "joint_jog_start",
  "ts": 1788940001000,
  "seq": 10,
  "payload": {
    "joint": "wrist_flex",
    "dir": 1,
    "step": "fine"
  }
}
```

- `dir=1`: wrist flex positive, same behavior as Mac key `I`.
- `dir=-1`: wrist flex negative, same behavior as Mac key `K`.
- Send `jog_keepalive` every 150 ms while held.
- On release or cancellation, send `joint_jog_stop` with `{}` payload.

## Gripper buttons

Press and hold Open or Close:

```json
{
  "v": 1,
  "type": "gripper_jog_start",
  "ts": 1788940002000,
  "seq": 20,
  "payload": {
    "action": "close",
    "step": "fine"
  }
}
```

- `action` is `open` or `close`.
- Send `jog_keepalive` every 150 ms while held.
- On release or cancellation, send `gripper_jog_stop` with `{}` payload.
- Closing may stop automatically when contact is detected. The App must then
  clear its pressed state and stop sending keepalives.

## Orientation (IMU) mode — DRAFT, pending Leo

Status: proposed by Thomas 2026-10-13. The App and `stub_server.py` implement
it; Leo's server does not yet. Field names may change after review.

The phone controls end-effector **orientation only**. Translation stays on the
jog buttons, because position integrated from a phone IMU drifts. Holding the
"按住轉手機" button acts as a clutch: orientation is engaged only while held.
It is a held control like the others: only one held control at a time, and
every release, cancel, background, blur, disconnect, or estop ends it.

On press, send `orient_start`. The App records the phone attitude `q0` at this
instant; the server should record the end-effector attitude `e0` at the same
time.

```json
{
  "v": 1,
  "type": "orient_start",
  "ts": 1788940005000,
  "seq": 50,
  "payload": {
    "frame": "phone_body",
    "rate_hz": 30,
    "gain": 0.7,
    "deadband_deg": 2.5,
    "smoothing": 0.3
  }
}
```

`gain`, `deadband_deg`, and `smoothing` are already applied by the App. They
are sent so the server can log them as experiment parameters.

While held, send `orient_update` about every 33 ms (30 Hz). It also serves as
the keepalive: no `jog_keepalive` is sent in this mode, and the 750 ms
watchdog applies to `orient_update`. The App does not wait for ACKs and the
server should not ACK updates.

```json
{
  "v": 1,
  "type": "orient_update",
  "ts": 1788940005033,
  "seq": 51,
  "payload": {"q_rel": [0.1672, 0.0, 0.0, 0.98592]}
}
```

- `q_rel` is a unit quaternion `[x, y, z, w]` equal to `q0⁻¹ · q`: the
  rotation since the press, expressed in the phone's own axes at press time
  (W3C DeviceOrientation: x to the screen's right, y to the screen's top, z out
  of the screen). The first update after a press is the identity.
- The App has already applied deadband, gain, and exponential smoothing.
- The App stops and sends `orient_stop` if the phone sensor produces no data
  for 300 ms.

Server side (proposal):

- Target attitude: `e_target = e0 · R · q_rel · R⁻¹`, where `R` is a fixed
  rotation aligning phone axes to the robot base frame.
- SO-101 has 5 joints, so project the 3-axis rotation: pitch → `wrist_flex`,
  roll → `wrist_roll`, yaw → ignored for now (or base rotation). The
  projection rule goes in the thesis.
- If IK fails, hold the last reachable pose and report it in `state.ik`.

On release send `orient_stop` with `{}` payload. ACKs follow the same release
sequence as the jog buttons: the stop's ACK (`orient_stop_requested`), then
the start's final ACK (`orient_stopped`, or `orient_watchdog_timeout`).

The stub additionally reports `orient_deg` (the rotation angle of the latest
`q_rel`) in `state` for debugging. It is not part of the proposed contract.

## Pose (WebXR AR) mode — DRAFT

Status: message names and the `p_rel_m` / `q_rel` fields follow Leo's
proposal of 2026-10-14; `gripper_delta` was added by Thomas. The App and
`stub_server.py` implement it.

The phone's 6-DoF pose comes from WebXR (`immersive-ar`, `local` reference
space): XRViewer on iPhone, Chrome on Android. Moving the phone moves the
end-effector, so IK drives the whole arm. Holding the control area is the
clutch, exactly like the other held controls (one at a time; release, cancel,
background, blur, disconnect, estop all end it).

On press, send `pose_start`. The App records the phone position `p0` and
attitude `q0`; the server should record the end-effector pose and gripper
position at the same instant.

```json
{
  "v": 1,
  "type": "pose_start",
  "ts": 1788940006000,
  "seq": 60,
  "payload": {
    "frame": "webxr_local",
    "rate_hz": 30,
    "gain": 0.7,
    "deadband_deg": 2.5,
    "smoothing": 0.3
  }
}
```

`gain`, `deadband_deg`, and `smoothing` apply to **rotation only** (same
processing as Orientation mode) and are sent for logging. Translation is sent
raw; scaling is the server's job.

While held, send `pose_update` about every 33 ms (30 Hz). It is also the
keepalive (750 ms watchdog) and is not ACKed.

```json
{
  "v": 1,
  "type": "pose_update",
  "ts": 1788940006033,
  "seq": 61,
  "payload": {
    "p_rel_m": [0.1, 0.0, -0.2],
    "q_rel": [0.0, 0.0, 0.0, 1.0],
    "gripper_delta": 0.5
  }
}
```

- `p_rel_m` = `p − p0` in metres, in the WebXR `local` frame: x right, y up,
  z back (toward the user). The frame is fixed when AR starts, with −z along
  the direction the phone camera pointed then. The first update after a press
  is `[0, 0, 0]`.
- `q_rel` = `q0⁻¹ · q`, identical in meaning and processing to Orientation
  mode's `q_rel` (phone axes at press time), so the wrist calibration carries
  over.
- `gripper_delta` ∈ [−1, 1]: while the clutch is held, holding the AR
  screen's "夾爪 開" button ramps it up at 1.0 per second, "夾爪 關" ramps it
  down (±1 = fully open / fully closed); releasing the button keeps the value.
  The server adds it to the gripper position recorded at `pose_start` and
  clamps. (Changed 2026-10-15 from a thumb slide, which was hard to use.)
- When the clutch is *not* held, the AR gripper buttons send the ordinary
  `gripper_jog_start` / `gripper_jog_stop`, so only one held control is ever
  active.
- The App stops (sends `pose_stop`) when tracking is lost or no pose arrives
  for 300 ms, or when the phone moves faster than 0.8 m/s or 229°/s (COBALT's
  threshold of 4 cm or 0.2 rad per 50 ms, e.g. a dropped phone).

Server side (Leo, 2026-10-14): map `p_rel_m` to the base frame, apply position
gain, workspace and speed limits, feed end-effector IK, keep the two-axis
rotation mapping, and handle IK failure.

On release send `pose_stop` with `{}` payload. ACKs follow the usual release
sequence: `pose_stop_requested`, then the start's final ACK `pose_stopped`
(or `pose_watchdog_timeout`).

The stub adds `p_rel_m` directly to `ee_pos` (no frame alignment) and reports
`orient_deg` and `gripper_delta` in `state` for debugging only.

## Step modes and current speeds

| Control | fine | coarse |
|---|---:|---:|
| free Cartesian jog | 0.09 m/s | 0.18 m/s |
| closed-gripper carry jog | 0.03 m/s | 0.06 m/s |
| free wrist flex | 45 deg/s | 90 deg/s |
| carrying wrist flex | 15 deg/s | 30 deg/s |
| gripper open | 90 deg/s | 180 deg/s |
| gripper close | 30 deg/s | 60 deg/s |

Use `fine` by default near the table, cube, or goal. The selected step is sent
inside every start payload. The optional discrete message below updates the
server's displayed mode:

```json
{
  "v": 1,
  "type": "mode",
  "ts": 1788940003000,
  "seq": 30,
  "payload": {"step": "fine"}
}
```

## ACK behavior

The start command remains active until a stop, contact stop, watchdog timeout,
workspace limit, or failure. Its final ACK therefore may arrive only after the
button is released. The App must not wait for the start ACK before sending
keepalives or the stop command.

The release sequence is:

1. Send the matching stop message with a new `seq`.
2. Receive the stop message's ACK, normally `status="ok"` and a reason ending
   in `_stop_requested`.
3. Receive the original start message's final ACK, normally `status="ok"` and
   a reason ending in `_stopped`.
4. Use `seq` to match every ACK to its request. Messages can arrive interleaved
   with state broadcasts.

An ACK has this form:

```json
{
  "v": 1,
  "type": "ack",
  "ts": 1788940003100,
  "seq": 30,
  "payload": {
    "seq": 30,
    "status": "ok",
    "reason": "mode_updated"
  }
}
```

If `status="rejected"`, do not automatically resend the command. Display the
reason and wait for a new user action.

## State messages

The server broadcasts state at approximately 5 Hz. The payload can include:

```json
{
  "ee_pos": [0.02, -0.14, 0.16],
  "gripper": "open",
  "step": "fine",
  "ik": "ok",
  "estop": false,
  "object_pos": [0.005, -0.2, 0.1025],
  "goal_pos": [0.075, -0.2, 0.1035],
  "task_success": false,
  "episode": 0
}
```

Treat `ik` and `gripper` as display/status strings. Use the Boolean `estop` to
enable or block motion controls. Unknown future state fields must be ignored.

## Reset and emergency stop

Scene reset:

```json
{"v":1,"type":"reset_scene","ts":1788940004000,"seq":40,"payload":{}}
```

Emergency stop:

```json
{"v":1,"type":"estop","ts":1788940004100,"seq":41,"payload":{}}
```

Reset emergency stop only after the server has frozen motion:

```json
{"v":1,"type":"estop_reset","ts":1788940004200,"seq":42,"payload":{}}
```

If the controlling WebSocket disconnects during an active command, the server
engages emergency stop. After reconnecting, read `state.estop`; do not silently
resume the old held button. Require a fresh user press after reset.

## UI/event requirements

- Use touch-down and touch-up/cancel events. A tap-only callback cannot provide
  press-and-hold control.
- Allow only one active held control at a time.
- Highlight a button only while its hold session is active.
- Always stop on release, cancel, background, navigation away, socket error, or
  loss of control ownership.
- Never buffer or automatically retry motion commands.
- Disable motion buttons while disconnected, before `welcome`, or while
  `state.estop=true`.
- Keep Estop available as a discrete, prominent button.

The old one-shot `move_delta` and `gripper` messages remain supported for
backward compatibility, but the new App control surface should use the hold
messages above.
