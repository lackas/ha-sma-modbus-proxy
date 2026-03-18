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
