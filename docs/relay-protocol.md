# Container ↔ ESP32 relay protocol

Status: **implemented on the container side** (input events in Step 3b;
heartbeat, print jobs, job status and cancellation in Step 3c). The firmware in
`firmware/` (Step 6) implements the device side: heartbeats, print jobs, status
and input events (EC11 rotary encoder with push button). `testing/relay_simulator.py` is a reference
implementation of the device side and is what the container tests run against.

| Direction | Requests |
| --- | --- |
| Device → container | `POST /api/device/heartbeat`, `POST /api/device/events`, `POST /api/device/jobs/{id}/status` |
| Container → device | `POST /jobs`, `PUT /jobs/{id}/chunks/{n}`, `POST /jobs/{id}/cancel`, `GET /jobs/{id}` |

## Authentication

One device, one shared bearer token, used in both directions:

```
Authorization: Bearer <token>
```

- Container: `PC1_RELAY_TOKEN`, or `PC1_RELAY_TOKEN_FILE` (path to a file, e.g. a
  Docker secret). At least 16 characters, no whitespace. A configured but unusable
  token stops the server at startup. If no token is set, device endpoints answer
  `503` and the server logs that device input is disabled.
- ESP32: the same token, entered in its captive portal / web UI.
- The token is separate from the settings password; neither works in place of the other.

Generate one with e.g. `openssl rand -hex 24`.

## Input events (device → container)

```
POST /api/device/events
Content-Type: application/json
Authorization: Bearer <token>
```

### Position

The channel position, 1–8, as an absolute value. The firmware decides how its
control maps to positions (a rotary encoder with push switch, for example, should
step and clamp or wrap at 1–8 locally). Send one whenever the position changes.

```json
{"event_id": "a3f9c2-41", "type": "position", "position": 5}
```

### Button gestures

The firmware measures how long the button is held and reports **gestures**, not
raw edges. All gestures of one physical press carry the same `press_id`.

| Gesture | When the firmware sends it |
| --- | --- |
| `press` | Released before 5 s. |
| `hold` | 5 s reached while still held (send once, immediately). |
| `hold_release` | Released after `hold`, regardless of how long it remains held. |
| `hold_cancel` | The device kept a held press for a local action (the reference firmware's reset sequence, at 10 s). Sent instead of `hold_release`; never followed by one. |

```json
{"event_id": "a3f9c2-42", "type": "button", "gesture": "press", "press_id": "a3f9c2-17", "position": 5}
```

`position` is optional on button events but recommended: the container applies it
before the gesture, so a lost position event cannot make a press act on the wrong
channel.

### Fields

| Field | Type | Rules |
| --- | --- | --- |
| `event_id` | string | 1–64 chars. Unique per event **across reboots** (e.g. a random boot id plus a counter). |
| `type` | `"position"` \| `"button"` | |
| `position` | integer 1–8 | Required for `position`; optional for `button`. |
| `gesture` | see above | Required for `button`. |
| `press_id` | string or integer | Required for `button`, 1–64 chars. Same value for every gesture of one press; new value per press. |

Unknown fields are ignored. Malformed events get `422`.

### What the container does

| Gesture | Container state | Result |
| --- | --- | --- |
| `press` | Menu open (app picker, Quick Actions, Adventure) | `selection`, or `selection_ignored` for a position that is not a choice (nothing prints) |
| `press` | Otherwise | `printing_channel` — prints the channel at the current position |
| `hold` | A print is running | `cancel_requested` — the print stops at the next bitmap-band boundary (up to about 3.2 s at 9600 baud). The rest of this press is ignored. |
| `hold` | Idle | `hold_ready` — a short paper feed tells the user they can release |
| `hold_release` | After `hold_ready` | `quick_actions` — prints the Quick Actions menu |
| `hold_cancel` | Any | `hold_cancelled` — releases the printer reservation armed by `hold` (a running print is unaffected) and ignores any later `hold_release` of this press |

Other results, all with HTTP `200`:

| Result | Meaning |
| --- | --- |
| `position_set` | Position stored. |
| `busy` | Another job holds the printer, or a press arrived within 3 s of the last print. Nothing happens; the event is not queued. |
| `printer_unavailable` | The print output cannot accept a receipt. Nothing happens. |
| `ignored_consumed` | `hold_release` for a press that already cancelled a print or was `hold_cancel`led. |
| `not_ready` | The server is still starting. |
| `error` | The gesture failed on the server (logged). |

### Response

```json
{"event_id": "a3f9c2-42", "result": "printing_channel", "position": 5, "duplicate": false}
```

The response returns as soon as the gesture has been interpreted; printing
continues in the background. The firmware may use `result` for local feedback
(e.g. an LED blink on `busy`).

### Delivery rules for the firmware

- Send events **one at a time, in order**, waiting for each response.
- On a timeout or connection error, retry the **same** event with the same
  `event_id`. The container remembers the last 256 event ids and answers a retry
  with the original result and `"duplicate": true`, without acting again.
- On `401`, stop and show "token rejected" in the device UI; on `503`, show "server
  not configured". Do not retry these in a tight loop.
- Drop events that are older than a few seconds instead of replaying them late; a
  stale `press` would print something the user no longer expects.
- Physical debouncing of the switch and the 5 s hold threshold are firmware
  responsibilities.

### Development

Without firmware, send the same events from the command line:

```bash
export PC1_RELAY_TOKEN=dev-relay-token-0123456789   # same value as the server's
just device position 5
just device press
just device quick-actions    # hold, then hold_release
just device hold             # during a print: cancel
just device hold-cancel      # hold, then hold_cancel (no Quick Actions)
```

### Reference firmware controls

The XIAO firmware starts at position 1 on every boot (the position is not
saved) and sends it whenever the link to the container comes up. Holding the
button 10 s, then turning one detent clockwise, one anticlockwise and clicking
(press under 2 s) erases the device settings and reopens setup, like holding
BOOT 5 s. The 5 s `hold` is still sent on the way; at 10 s the device sends
`hold_cancel` and keeps the rest of the sequence local (no position or button
events). The user LED (GPIO21) is lit while the sequence is active and blinks
on each accepted step; a wrong turn, an early press or 10 s without the next
step cancels it.

## Heartbeat (device → container)

```
POST /api/device/heartbeat
Authorization: Bearer <token>
```

```json
{
  "device_id": "pc1-a1b2c3",
  "boot_id": "7f3e91c0d2aa",
  "firmware": "0.1.0",
  "callback_url": "http://192.168.1.50",
  "printer": {"state": "ready", "detail": null, "width_dots": 384, "baudrate": 9600},
  "limits": {"max_chunk_bytes": 8192, "max_job_bytes": 1048576},
  "active_job_id": null
}
```

| Field | Rules |
| --- | --- |
| `device_id` | 1–64 chars, stable for the device. |
| `boot_id` | 1–64 chars, new random value on every boot. A change tells the container the device restarted. |
| `callback_url` | Base URL of the device's HTTP server, as reachable **from the container** (http/https, no credentials, query or fragment). The container can pin it instead with `PC1_RELAY_URL`, in which case this value is only displayed. |
| `printer.state` | `ready`, `busy` (printing), `paper_out`, `error` or `offline` (no response on the UART). Only `ready`/`busy` accept jobs. `detail` (≤200 chars) is shown in the UI. |
| `printer.width_dots` | 8–2048; receipts wider than this are rejected. |
| `printer.baudrate` | UART speed, 1200–921600; the container uses it to size job timeouts. |
| `limits.max_chunk_bytes` | 256 B–1 MiB: the largest chunk body the device accepts. A framed 64-row downloaded bitmap is 3,081 bytes at 384 dots. The container shortens bands in multiples of eight rows for smaller limits; at least 393 bytes are needed for eight rows at that width. |
| `limits.max_job_bytes` | 1 KiB–64 MiB: larger jobs are refused by the container. |
| `active_job_id` | Job the device is currently processing, if any. |

Response: `{"heartbeat_interval_s": 10, "heartbeat_timeout_s": 30,
"channels_hash": "3f1c9a0b7d2e4c61"}`. Send a
heartbeat at that interval, and immediately after boot, after reconnecting WiFi
and whenever `printer.state` changes. The container considers the device offline
after `heartbeat_timeout_s` without one, or as soon as a push to `callback_url`
fails; the next heartbeat brings it back. This is the link status shown in the
settings UI.

## Channel names (device ← container)

Channels can have a display name (set in the settings UI; up to 24 characters,
single line, `""` = unnamed). `channels_hash` in the heartbeat response is a
16-hex-character hash of the ordered list of the eight names: it changes if and
only if a name changes (modules and schedules do not affect it). When it
differs from the hash of the copy the device holds (or the device has none,
e.g. after boot), fetch the names:

```
GET /api/device/channels
Authorization: Bearer <token>
```

```json
{"hash": "3f1c9a0b7d2e4c61",
 "channels": [{"position": 1, "name": ""}, {"position": 2, "name": "Work update"}, ...]}
```

Store the names together with this `hash` (not the heartbeat's: a name may
change between the two requests, and the next heartbeat then fetches again).
On a failed fetch keep the old copy and retry on the next heartbeat. Names are
UTF-8. A container without this feature omits `channels_hash`; keep no names.

## Print jobs (container → device)

The container renders a receipt to a 1-bit bitmap and turns it into **records**,
each a self-contained piece of printer data:

1. preamble (`FS .`),
2. one record per 64-row RAM bitmap band (`GS *` definition plus `GS / 0` print,
   see [printer-uart-reference.md](printer-uart-reference.md)); the last band is
   padded with up to seven white rows to reach an eight-row boundary,
3. the cutter feed (`ESC J n`, split if necessary).

The device writes each record to the UART **unchanged and in order**. It never
needs to parse printer commands. Cancellation takes effect only **between**
records, so the printer is never left inside a bitmap definition or between its
definition and print command. Records are opaque to the firmware; the HTTP
framing is unchanged. Native USB output still uses 24-row `GS v 0` raster strips.

Tactile feeds (`kind: "feed"`) are tiny jobs of the same form. The container
does not wait for them to finish. The firmware initialises its printer UART
on device boot, independently of container startup.

All requests carry `Authorization: Bearer <token>`. The device must answer `401`
to a missing or wrong token.

### 1. Create

```
POST /jobs
Content-Type: application/json

{"job_id": "3f2a…", "kind": "receipt", "chunks": 3, "records": 24, "bytes": 25184}
```

`bytes` is the total size of all chunk bodies (records plus their 2-byte
headers). `job_id` is 1–64 characters from `[A-Za-z0-9_-]`.

| Response | When |
| --- | --- |
| `201` + job status | Created. |
| `200` + job status | A job with this id already exists. Creating it again must never start a second job. |
| `409` | Another job is still active. The container retries for up to 3 s (typically a feed finishing), then gives up. |
| `413` | `bytes` exceeds the device limit. |
| `503` | Printer not ready (`paper_out`, `error`, `offline`). |
| `400` | Malformed request. |

### 2. Chunks

```
PUT /jobs/{job_id}/chunks/{index}
Content-Type: application/octet-stream
X-PC1-Final-Chunk: 0 | 1

<records: [2-byte big-endian length][record bytes] repeated>
```

Chunks are sent in order from index 0. Records never span chunks, and each body
is at most `max_chunk_bytes`. The device may start printing as soon as the first
chunk arrives.

| Response | When |
| --- | --- |
| `200` | Accepted. Re-sending an index already received returns `200` with `"duplicate": true` and is not printed again. |
| `429` + `Retry-After-Ms` | Buffer full. The container waits (each back-off capped at 2 s) and resends the same chunk, for at most 15 s; then it fails and requests cancellation. Use this for flow control instead of blocking the request while the UART drains. |
| `409` | Index out of order, or the job already finished. |
| `404` | Unknown job (e.g. lost in a reboot). The container fails the job. |
| `413` / `400` | Body too large / malformed framing. |

### 3. Status (device → container)

```
POST /api/device/jobs/{job_id}/status
Authorization: Bearer <token>

{"state": "printing", "boot_id": "7f3e91c0d2aa", "records_written": 1, "bytes_written": 2}
```

| State | Meaning |
| --- | --- |
| `receiving` | Created; waiting for data. |
| `printing` | Records are being written to the UART. Report once when writing starts. |
| `completed` | Every record was written and the UART has drained. This proves delivery to the printer, **not** that paper came out. |
| `cancelled` | Stopped between records after a cancel request. Skip the cutter feed. |
| `failed` | Could not finish (UART error, printer went offline). Include `error` (≤300 chars). |

`bytes_written` counts printer bytes (excluding record headers). The container
answers `404` for an unknown job; stop reporting that job. Final states are
fixed: a later report cannot change them.

### 4. Cancel

```
POST /jobs/{job_id}/cancel
```

Stop after the record being written, discard the rest, report `cancelled`.
Answer `200` with the current status, even if the job had already finished.

### 5. Job status (polling)

```
GET /jobs/{job_id}
```

Returns the same body as a status report (`200`), or `404` if unknown. The
container polls every 5 s while waiting, in case a status callback was lost.
Keep the last few finished jobs (the simulator keeps 32) so that late polls get
an answer.

### Timing, concurrency and failure rules

- **One job at a time.** The container holds its print reservation until the job
  reaches a final state, so jobs never overlap. Input events, heartbeats and
  status requests must still be served while a job prints: run the UART writer
  separately from the HTTP server.
- **Timeouts.** The container waits up to 30 s plus twice the theoretical UART
  time (`bytes × 10 / baudrate`). After that it marks the job failed and sends a
  cancel.
- **No replay.** If the device reboots mid-job (new `boot_id`), the container
  marks the job failed and does not resend it. The device must not resume jobs
  after a reboot either. A job the container no longer knows about (it
  restarted) may finish normally; its status reports get `404`. A status report
  whose `boot_id` differs from the job's boot id is ignored.
- **Offline.** When the device is unreachable or reports a printer state other
  than `ready`/`busy`, the container skips prints instead of queueing them:
  UI/API prints return `503`; scheduled, email and Slack prints are logged and
  dropped. The first print after the device silently disappears is accepted and
  then fails when the push fails. Until the heartbeat timeout, that failed push
  is how the container notices.

## Development

```bash
just dev-esp32                            # terminal 1: backend + settings UI
just relay-sim                            # terminal 2: simulated ESP32 at 9600 baud
just device position 5 && just device press   # terminal 3: the device's button
```

These recipes share the development relay token `12345678901234567890` unless
`PC1_RELAY_TOKEN` is exported (then export the same value in each terminal).

The simulator saves each receipt it receives, decoded from the raster bytes, as
a PNG under `testing/artifacts/relay/`. Use `--baud 0` for instant printing, and
`--port`/`--advertise-url` if the container cannot reach `127.0.0.1:8090`.
