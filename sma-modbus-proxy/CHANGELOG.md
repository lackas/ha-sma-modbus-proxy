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
