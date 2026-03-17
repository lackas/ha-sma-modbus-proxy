# SMA Modbus Proxy v2.0 — Direct Modbus Polling Design

## Problem

The Gridbox cannot discover the SMA Tripower X on the network because:
- The Tripower X serves Model 103 at address 41257 (deep in a long SunSpec model chain)
- The Tripower X uses Modbus unit ID 126
- The Gridbox expects Model 103 at the standard position (40071) with unit ID 1 or 3
- The Tripower X does not serve SMA proprietary registers (30xxx/35xxx)

The v1 proxy solves this by reading HA sensors (via WebSocket, updated every 60s by the ennexOS integration) and serving them as a standard SMA inverter. This works but introduces up to 60s data staleness, requires 17 sensor entity IDs in the config, and depends on the HA API for the data path.

## Solution

Replace the HA WebSocket data source with direct Modbus TCP polling of the inverter. The proxy reads SunSpec registers from the Tripower X every 1 second and serves them at the standard addresses the Gridbox expects.

## Architecture

```
SMA Tripower X        SMA Modbus Proxy         Gridbox
192.168.x.216:502     (HA add-on, port 502)
unit 126              unit 1/3

Model 1  @ 40003  ──► Model 1  @ 40003
Model 103 @ 41257 ──► Model 103 @ 40071
Model 160 @ 41415 ──► (computed 30xxx/35xxx)
                      END       @ 40123
```

Two async components running in parallel:
- **Poll thread**: synchronous pymodbus `ModbusTcpClient`, reads inverter every 1s (~18ms per cycle), parses values, replaces "not implemented" markers with zeros, writes to in-memory register map
- **Modbus server**: serves pre-fetched registers to Gridbox clients instantly

No explicit locking. Individual register writes are effectively atomic under Python's GIL. A Gridbox read spanning multiple registers could theoretically see a partial update (high word old, low word new for a U32), but the window is ~18ms out of every 1000ms and the consequence is a single slightly inconsistent reading — acceptable for this use case.

## Config

### HA Add-on (options in UI)

```yaml
inverter_ip: "192.168.1.216"
serial: 1234567890
max_power_w: 12000
```

Removed from v1: `ha_token`, `port`, all 17 `sensor_*` fields.

Kept from v1: `serial` (emulated inverter identity, default 1234567890), `max_power_w` (for 30231/30233 registers).

### Standalone Docker

Same image, reads config from environment variables:
- `INVERTER_IP` (required)
- `SERIAL` (default: 1234567890)
- `MAX_POWER_W` (default: 12000)

## Data Source: What We Read

Two Modbus reads per poll cycle from the inverter (unit 126). All addresses below are 1-based SunSpec convention; subtract 1 for 0-based wire addresses.

| Read | Address (1-based) | Count | Content | Frequency |
|------|-------------------|-------|---------|-----------|
| Model 103 data | 41259 | 50 | AC: power, current, voltage, freq, yield, DC summary | Every 1s |
| Model 160 (header+data) | 41415 | 70 | Per-MPPT DC: 3 strings, current/voltage/power/energy | Every 1s |

Measured read latency: ~18ms for both reads combined.

The proxy **parses** the raw register values (applying scale factors), replaces 0xFFFF/0x8000 "not implemented" markers with 0, and **re-packs** them into the standard Model 103 address range (40073+). This is not a raw byte-copy because:
1. We need the parsed values to compute SMA proprietary registers (30xxx/35xxx)
2. We need to replace nighttime "not implemented" markers with zeros
3. The inverter's scale factors must be read and applied correctly

Note: the inverter uses W_SF=1 (power in units of 10W), which differs from v1's hardcoded SF=0. The proxy normalizes to v1-compatible scale factors when re-packing.

## Data Served: What the Gridbox Gets

### SunSpec Registers (parsed, cleaned, re-packed at standard addresses)

| Target Address | Source | Content |
|----------------|--------|---------|
| 40001-40002 | Static | "SunS" header |
| 40003-40070 | Static (faked) | Common block: SMA / STP 10.0-3AV-40 / config serial |
| 40071-40122 | Model 103 from inverter | Three Phase Inverter data |
| 40123-40124 | Static | END marker |

### SMA Proprietary Registers (computed from Model 103 + 160)

The Tripower X does not serve these. The proxy computes them from SunSpec data, same as v1.

| Register | Data | Source |
|----------|------|--------|
| 30003 | SusyID | Static (378) |
| 30005, 30057 | Serial | Config or Model 1 |
| 30051 | Device class | Static (8001 = Solar Inverter) |
| 30053 | Device type | Static (9348 = STP 10.0-3AV-40) |
| 30059 | Software package | Static |
| 30201 | Grid relay | Derived from operating state |
| 30231, 30233 | Max power | From config `max_power_w` |
| 30531 | Total yield (Wh) | Model 103 regs 22-23 |
| 30769 | DC power | Model 103 reg 29 |
| 30771-30775 | AC power per phase | Computed from Model 103 V * I |
| 30783-30787 | Voltages | Model 103 regs 8-10 |
| 30795 | Frequency | Model 103 reg 14 |
| 30797 | Reactive power | Model 103 reg 18 |
| 30803-30815 | DC per string (A+B) | Model 160 modules 1+2 |
| 30835 | Operating status | Derived from Model 103 state |
| 35377-35393 | MPPT trackers (3 strings) | Model 160 modules 1+2+3 (6 regs each: W, V, I) |

### Nighttime / "Not Implemented" Handling

The inverter returns 0xFFFF (uint16) or 0x8000 (int16) for unavailable real-time values at night. The proxy replaces these with 0 in the served register map, matching v1 behavior that the Gridbox already works with.

Scale factors and static fields are passed through as-is.

## Unit IDs

The proxy responds on unit IDs 0, 1, 2, 3, and 247 (same as v1). The Gridbox uses 1 or 3.

## Logging

Carried over from v1.1.9:
- Startup: inverter IP, serial, tracking info
- Modbus client connect/disconnect via `trace_connect`
- First Modbus read from Gridbox
- Periodic (5 min) Modbus read count
- Periodic AC/DC status summary (every 60th poll, ~1 min)
- Inverter connection errors and reconnects

New in v2:
- Poll cycle errors (inverter unreachable)
- Inverter connection status

## Dependencies

- `pymodbus==3.12.1` — used for both Modbus TCP client (polling inverter) and server (serving Gridbox)
- `websockets` — **removed** (no longer needed, HA WebSocket replaced by direct Modbus)

## Error Handling

- **Missing `inverter_ip`**: log error and exit at startup
- **Invalid `inverter_ip`**: connection fails, retry loop with 5s backoff

- **Inverter unreachable at startup**: retry every 5s, log warning
- **Inverter unreachable during operation**: serve last known values, log warning, retry next cycle
- **Inverter returns errors for specific registers**: serve zeros for those registers
- **Gridbox connects before first successful poll**: serve zeros (same as v1 startup behavior)

## File Structure

```
ha-sma-modbus-proxy/
  sma-modbus-proxy/
    config.yaml          # HA add-on config (simplified)
    Dockerfile           # HA add-on image
    build.yaml           # Build targets
    run.sh               # s6-overlay entrypoint
    sma_proxy.py         # Main proxy (rewritten)
    icon.png             # Add-on icon
    logo.png             # Add-on logo
  docker-compose.yml     # Standalone deployment (new)
  repository.yaml
```

## Standalone Docker Support

A `docker-compose.yml` at the repo root for non-HA users:

```yaml
services:
  sma-modbus-proxy:
    build: sma-modbus-proxy
    network_mode: host
    environment:
      - INVERTER_IP=192.168.1.216
      - SERIAL=1234567890
      - MAX_POWER_W=12000
```

The Python code checks for environment variables first, then falls back to `/data/options.json` (HA add-on path).

## Migration from v1

- Remove all `sensor_*` fields from add-on config
- Remove `ha_token` (already removed in 1.1.9)
- Add `inverter_ip`
- The ennexOS HA integration continues running independently for dashboards/history
- No changes needed on the Gridbox side

## Scope Exclusions

- No SunSpec 700-series serving (the Gridbox doesn't need it)
- No HA sensor fallback (keep it simple)
- No multi-inverter support (single inverter only)
- No write/control support (read-only proxy)

## Verified Assumptions

- Tripower X firmware 03.14.22.R serves Model 103 at 41257 and Model 160 at 41415 on unit 126
- SMA proprietary registers (30xxx/35xxx) are not available on the Tripower X
- Model 103 single read: 10-32ms (avg 16ms)
- Full poll cycle (103 + 160): ~18ms
- Max Modbus PDU: 125 registers per read (inverter rejects larger)
- Gridbox polls ~1 read/sec (255 reads per 5 min)
- The ennexOS HA integration also connects to the inverter (REST API on port 443, not Modbus) — no contention on port 502
