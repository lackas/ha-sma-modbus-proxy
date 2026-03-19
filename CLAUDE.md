# CLAUDE.md — SMA Modbus Proxy

## Purpose

This proxy fakes a supported SMA inverter (STP 10.0-3AV-40) so that energy
management systems like Viessmann Gridbox / gridX can discover it. The
hardcoded model name is **critical** — changing it breaks the entire purpose.

## Architecture

- Single file: `sma-modbus-proxy/sma_proxy.py`
- HA add-on config: `sma-modbus-proxy/config.yaml`
- Changelog: `sma-modbus-proxy/CHANGELOG.md`
- Polls real inverter (SMA STP X, ennexOS) at `MODEL_103_ADDR` / `MODEL_160_ADDR`
- Serves faked SunSpec Model 1 + 103 at standard addresses for the Gridbox

## Inverter Details (SMA STP 12-50, ennexOS)

### Addressing

- SMA 30xxx proprietary registers are **NOT available** on STP X / ennexOS
- Only SunSpec models are accessible via Modbus TCP
- Unit ID: 126
- pymodbus `read_holding_registers` uses **0-based** addresses
- SMA docs and mbpoll use **1-based** addresses (subtract 1 for pymodbus)
- Existing code: `MODEL_103_ADDR = 41258` is 0-based (SMA doc: 41259)

### SunSpec Model Chain (discovered via mbpoll)

```
40003: Model 1   (Common, len 66)     — Manufacturer, Model, Serial, Version
40071: Model 123 (len 24)
40097: Model 701 (len 153)            — DER AC Measurement
40252: Model 702 (len 50)             — DER Capacity
40304: Model 703 (len 17)
40323: Model 704 (len 65)
40390: Model 705 (len 65)
40457: Model 706 (len 55)
40514: Model 707 (len 141)
40657: Model 708 (len 141)
40800: Model 709 (len 135)
40937: Model 710 (len 135)
41074: Model 711 (len 32)
41108: Model 712 (len 52)
41162: Model 714 (len 93)
41257: Model 103 (len 50)             — Three Phase Inverter (what we poll)
41309: Model 120 (len 26)             — Nameplate (has WRtg = max power)
41337: Model 121 (len 30)
41369: Model 122 (len 44)
41415: Model 160 (len 68)             — MPPT (what we poll)
41485: END marker (0xFFFF)
```

### Serial Number

- Available in SunSpec Common model at register **40053** (16-reg ASCII string)
- pymodbus 0-based address: **40052**
- On this inverter: `"3020142525"` → int `3020142525`

### Max Power (WRtg)

- Available in SunSpec Model 120 (Nameplate) at address **41309**
- WRtg at data offset 1 (register 41312), WRtg_SF at data offset 22 (41333)
- Values: WRtg=1200, SF=1 → 12000W
- Finding Model 120 requires walking the model chain from a known position
- Walking is fragile (address arithmetic, varies by firmware)
- Currently kept as config option `max_power_w` (default 12000) — good enough

### What NOT to Touch

- **Model name** (`"STP 10.0-3AV-40"`): hardcoded fake identity for Gridbox discovery
- **SMA proprietary registers** (30003, 30051, 30053, etc.): written to local store only, never read from inverter (they don't exist on STP X)

## Testing

Use `mbpoll` from local machine to test register reads:
```bash
# Read SunSpec Common serial (1-based addr, mbpoll handles it)
mbpoll -a 126 -r 40053 -c 16 -t 4:hex -1 192.168.1.216

# Read Model 120 WRtg
mbpoll -a 126 -r 41312 -c 1 -t 4 -1 192.168.1.216

# Verify Model 103 header
mbpoll -a 126 -r 41258 -c 2 -t 4 -1 192.168.1.216
```

Inverter IP: `192.168.1.216`
