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
