## 2.4.1
- Fix negative integral windup while free (spotted in the 2.4.0 live trace: the
  trim wound to -786 W under the cap). The "stay free" guard only clamped the
  trim to ≤0 instead of resetting it, so it accumulated negative while under the
  cap — which would have delayed the next engagement until export exceeded the
  cap by |i|, a deadband in the other direction. Now reset to 0 when free.

## 2.4.0
- Feedforward curtailment controller — kills the engagement transient the debug
  trace exposed on 2026-07-07 (~45 s at +500..+844 W over cap while the PI
  integral wound up through the deadband, then a -795 W undershoot; steady state
  was already fine). WMaxLimPct is % of *rated*, but the SMA runs well below
  rated, so small cuts didn't bind and the integral had to crawl c down before
  anything happened.
  - New law drives the limit straight from the SMA's measured AC power (already
    polled 1 Hz): `target_sma = sma_now - (export - cap)`, `WMaxLimPct =
    100*target_sma/rated`. Feedforward gain is exactly 1 (the second inverter and
    house are disturbances), so it binds in one step and tracks the SMA ramp
    without windup or overshoot.
  - Light integral trims residual bias only (Watts, ±10 % rated clamp); release
    stays asymmetric (fast down, slow up). Stale SMA read → rated fallback
    (conservative over-cut). "Free unless actually cutting" guard avoids phantom
    curtailment under the cap.
  - Debug trace now shows measured `sma=` and the integral trim in Watts.

## 2.3.3
- Curtailment loop tuning + observability, from the first real over-cap hold
  (06.07., battery full → export held on 14256 W, setpoint 85-90 %):
  - **Control interval 1 s → 3 s.** A 1 s loop rewrites the SMA limit before the
    inverter has ramped to the previous one, which made the setpoint hunt ~2-3 %.
    3 s matches the SMA ramp; the read timeout (2 s) now sits under the tick.
  - **Per-step DEBUG tuning trace**: `export / err / WMaxLimPct / c / i` per loop,
    silent at info. The only way to tune kp/ki against real closed-loop data on an
    over-cap day (the offline sim has no feedback, so it can't).
  - **"gridX write side resumed" INFO line**: edge-triggered once when external
    writes reappear after >=10 min dry. The control-dropout signature is reads
    alive / writes at 0; this announces when the EMS write side is back so it's
    clear when to switch curtailment back to Modbus pass-through.

## 2.3.2
- Fail-safe now releases control instead of cutting the SMA. On lost meter
  reads it hands the inverter back to normal operation (WMaxLim_Ena=0) rather
  than forcing WMaxLimPct=0 — a brief over-cap during a meter outage beats
  killing all PV, and it matches how the plant runs uncontrolled anyway.
  The PI state is reset so control resumes clean when the meter returns.

## 2.3.1
- Curtailment controller fixes found while validating in Simulate:
  - **Anti-windup fix:** the integral used back-calculation that wound up while
    below the cap, which would have slammed the SMA to 0 % the instant export
    first crossed the threshold (cloudy → sunny). Now the integral *state* is
    clamped to 0..100 %, so no hidden offset builds up while released.
  - **ki tuned** kp/6 → kp/3 so the integral can hold the steady-state cut;
    with a realistic inverter lag the loop now sits on the cap instead of
    hovering above it. (Gains still provisional — final tune against Simulate
    data on a real over-cap day.)
  - **Simulate now graphable:** publishes `sensor.sma_curtail_sim` (WMaxLimPct,
    with export/curtail/threshold attributes) — a *separate* entity so it never
    implies real curtailment. The live `sma_curtailment_setpoint` is still only
    written in Self-Adaptive.

## 2.3.0
- Self-adaptive curtailment. New `curtailment` mode replaces `forward_writes`:
  **Off | Modbus | Self-Adaptive | Simulate** (mutually exclusive by design;
  legacy `forward_writes: true/false` still maps to Modbus/Off).
  - **Modbus** — unchanged pass-through: translate EMS `WMaxLimPct` writes → M123.
  - **Self-Adaptive** — the proxy closes its own control loop: reads net grid
    power from a Tasmota SML meter (`tasmota_url`, ~1 Hz HTTP) and drives the SMA
    via M123 `WMaxLimPct` to keep export ≤ `curtail_threshold_w` (default 14256 W
    = §9 60 %). PI controller with anti-windup, asymmetric (fast down / slow
    release), no static margin. Because it feeds back on the metered *net* value,
    the uncontrolled second inverter + house load are just disturbances it
    rejects — and it will curtail the SMA below its own share (down to 0 %) when
    the second inverter alone pushes toward the cap. Pushes the curtailment value
    to HA via the existing tracker. Fail-safe cuts the SMA to 0 % if the meter
    read fails.
  - **Simulate** — same loop, computes + logs what it *would* set, no SMA writes,
    no HA push (validate against real data before going live).
  - Config: `curtailment`, `curtail_threshold_w` (default 14256), `tasmota_url`.

## 2.2.10
- Debugging: cut the `debug` firehose. 2.2.9 unmuted pymodbus's own logger,
  which at DEBUG dumps every decoded PDU — and the proxy's own 1 Hz inverter
  poll alone floods the log (~2 PDUs/s) and buries the client-connection
  events. `debug` is back to its normal, useful output (per-minute AC/DC
  summaries, poll-interval, temperatures) plus the connection-tracing wrapper
  (peer IP + disconnect reason), which is the actual signal for diagnosing
  client reconnect storms. pymodbus's own logger stays muted.

## 2.2.9
- Debugging: make client reconnect storms diagnosable. In `debug` log level
  the proxy now unmutes pymodbus's own logger (`pymodbus.logging`), so
  connection lifecycle and disconnect reasons ("Connection lost server due to
  ...") become visible, and wraps the request handler to log the real peer
  (`conn OPEN peer=...` / `conn CLOSE peer=... reason=...`). Together these tell
  us whether a connect burst is one client flapping or many, and why each
  connection drops. Both are debug-gated; `info`/`warning` output is unchanged.
- The 5-minute inverter temperature line dropped from INFO to DEBUG (not useful
  at INFO).

## 2.2.8
- Release stale curtailment at night. `state` only flips on a Modbus write, so
  when the inverter goes idle the tracker stays pinned at `curtailed` from the
  last daytime write. That crashed the steady heartbeat (`%.2f` on a None pct)
  and held `sma_curtailment_setpoint` stale, so energy_free saw a phantom
  curtailment all night. Now `curtailed` with no write for 600 s
  (`CURTAIL_STALE_S`) releases to free and pushes `(100, off)`; real
  curtailment writes far more often, so a genuine cap never trips it.

## 2.2.7
- Self-heal forward client on persistent error. Any exception during a
  Model 123 write (broken pipe, timeout, connection reset) now closes the
  forward client so the next call reconnects fresh. Previously the proxy
  would log a warning per failed write but keep reusing the dead socket
  indefinitely, requiring an add-on restart. Triggered in practice when
  the HA host got a new DHCP lease: the proxy's forward TCP socket was
  bound to the old source IP and silently rejected by the inverter, while
  the read-side `inverter_poll_loop` (which already had self-heal) kept
  working.

## 2.2.6
- Push SunSpec operating state to HA as `sensor.sma_operating_state` (text:
  off/sleeping/starting/mppt/throttled/shutting_down/fault/standby; raw int
  in `sunspec_st` attribute). Pushes on every change plus a 5-min heartbeat
  so HA restarts recover within 5 min.
- Free-running curtailment heartbeat: re-push `(setpoint=100, off)` every
  5 min even when not curtailed. Fixes the night/HA-restart gap where the
  pushed entities went ghost until the next curtailment event.
- Parse Model 103 temperatures (Tmp_Cab/Snk/Trns/Oth) at scale factor d[35]
  and log them at INFO every 5 min. Fields the inverter doesn't implement
  are silently skipped.
- `_push_ha_state` now checks `response.ok` — previously HTTP 4xx/5xx
  responses (e.g. expired SUPERVISOR_TOKEN, supervisor unreachable) were
  silently swallowed. Now they log as WARNING with status code + body.

## 2.2.5
- Fix `binary_sensor.sma_curtailed` getting stuck "on" after a release at
  pct=100 (gridX can write `pct=100, ena=1` before releasing — display pct
  didn't move, so the dedup skipped the off-push). Now dedup tracks
  (pct, curtailed) together; either flip triggers a push.

## 2.2.4
- Push curtailment state to Home Assistant via Supervisor API. Creates
  `sensor.sma_curtailment_setpoint` (% integer) and
  `binary_sensor.sma_curtailed` on first event. Pushes on setpoint change
  plus a 1/min heartbeat while curtailed. Disable via `ha_push: false`.

## 2.2.3
- Inverter throttle state=5 demoted from WARNING to DEBUG. Since v2.2.0
  curtailment is commanded by the proxy itself, so state=5 is just the
  inverter's acknowledgment of our own write — not an anomaly.
- "Inverter no longer throttled" also moved to DEBUG (was INFO).
- Aggregate connect/disconnect storms: rapid bursts (>1 event within 5s)
  collapse into a single `Modbus client churn: +N more events …` summary
  emitted when activity quiets. Sparse events still log normally.
- WARNING level is now reserved for actual problems only (forward failures,
  exceptions, inverter connection loss, unknown register writes).

## 2.2.2
- Heartbeat only emits while curtailment is active; free-running stays silent.
  (State-change events + 5-min Modbus stats already cover liveness.)
- 5-min Modbus stats now report writes alongside reads.
- Compacted INFO message wording.

## 2.2.1
- Restructured log levels so INFO shows "things are working" without spam:
  - **INFO**: state-change events (Curtailment STARTED / RELEASED), plus a
    60-second heartbeat summary (write count + setpoint range/state)
  - **DEBUG**: every individual write, translate, and per-register success
  - **WARNING**: only real problems (forward failures, exceptions,
    inverter connection loss, unknown register writes)
- Drops log volume from ~90 lines/min to ~1-2 INFO lines/min during normal
  operation; full firehose available via `log_level: debug`.

## 2.2.0
- **SunSpec model discovery on startup** — walks the inverter's model chain
  and identifies all exposed models. Logs each one with its base wire address.
  Required for the translation layer; works on STP X 12-50 (ennexOS) where
  SunSpec Model 123 lives at firmware-dependent addresses.
- **Translation layer for gridX legacy writes**: incoming writes on legacy
  SMA addresses are mapped to SunSpec Model 123 writes on the real inverter.
  - `wire 40024` (legacy WMaxLimPct, 0.01 %-units) → Model 123 WMaxLimPct +
    WMaxLim_Ena (auto-enabled when limit < 100 %)
  - `wire 40212` (WMod enum) → ignored (Model 123 has its own Ena)
  - `wire 43091` (Grid Guard) → ignored (SunSpec needs no auth)
  Confirmed working via live test on STP X 12-50 firmware 03.14.22.R.
- New `log_level` config option (`debug` / `info` / `warning`, default `info`).
  `info` keeps startup, writes, and errors visible; `debug` adds per-minute
  AC/DC summaries and poll-interval changes.
- `forward_writes` default changed back to `false` — the user enables it
  after verifying discovery output on first deploy.
- Several chatty INFO log lines moved to DEBUG (per-minute AC/DC summary,
  first-Modbus-read, poll-interval changes).

## 2.1.0
- Human-friendly decode for known control registers:
  - `40024` (PFCtlComCfg.PF) → cos φ setpoint (FIX4, 10000 = 1.0)
  - `40212` (WCtlComCfg.WMod) → active power control mode (U32 enum, 1079 = "plant control")
  - `43092` → vendor-specific (raw u32 fallback)
- Unknown registers fall back to raw u16/s16/u32/s32 dump (unchanged behavior)
- New `forward_writes` config flag (default `true`).
  External writes are forwarded 1:1 to the real inverter via a dedicated
  Modbus client. This is the safer default: without forwarding the STP X
  runs at default cos φ=1 and ignores gridX's grid-code Q(P) commands.
  Inverter must be configured for "Externe Vorgabe durch Kommunikation"
  in its WebUI — otherwise writes are rejected but logged.
  Set `forward_writes: false` to disable (legacy 2.0.6 behavior, logging only).

## 2.0.6
- Log incoming external Modbus writes (FC6 / FC16) from clients like gridX / Gridbox
- New `sma_proxy.writes` logger at WARNING level — decodes register, hex/dec values, u16/s16/u32/s32 interpretation, and hints for known SunSpec Model 123 + SMA proprietary control registers
- Internal store updates (poll_inverter, identity setup) stay silent so external writes are clearly visible
- Logging-only: writes are NOT yet forwarded to the real inverter (planned for a future version)

## 2.0.5
- Auto-detect serial number from inverter (SunSpec Common model)
- Auto-detect max power from inverter (SunSpec Model 120 WRtg)
- Remove serial and max_power_w from config (auto-detected, fall back to defaults)
- Add device_identifier config option (default "STP 10.0-3AV-40") for faked model name

## 2.0.3
- Show actual version in log output
- Add throttle detection (state=5) with duration logging
- Adaptive polling: 1s when producing, 60s on standby/night
- Exponential backoff on poll errors (5s to 5min)

## 2.0.2
- Fix Model 160 MPPT per-string parsing (IDStr is 8 registers on STP X)
- DC per-string values now correct (verified: East+West matches AC output)

## 2.0.0
- Major rewrite: direct Modbus polling replaces HA WebSocket
- Poll SMA inverter every 1s via Modbus TCP (~18ms per cycle)
- Config simplified to: inverter_ip, serial, max_power_w
- No longer requires HA API access or sensor entity configuration
- Support 3 MPPT strings (from Model 160)
- Add docker-compose.yml for standalone deployment

## 1.1.9
- Remove port and ha_token from UI config
- Fix health "ok" incorrectly mapped to Fault state

## 1.1.8
- Log Modbus client connect/disconnect
- Log first Modbus read with address details
- Report read count every 5 minutes

## 1.1.6
- Fix: init must be false for s6-overlay v3 (SUPERVISOR_TOKEN now works)

## 1.1.4
- Add icon.png and logo.png for add-on branding
- Prefer SUPERVISOR_TOKEN over ha_token

## 1.1.3
- Fix s6-overlay v3 with-contenv path for SUPERVISOR_TOKEN

## 1.0.0
- Initial release: SunSpec Model 103 emulation via HA WebSocket sensors
