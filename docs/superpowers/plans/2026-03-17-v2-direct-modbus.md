# SMA Modbus Proxy v2.0 — Direct Modbus Polling Implementation Plan

> **For agentic workers:** REQUIRED: Use superpowers:subagent-driven-development (if subagents available) or superpowers:executing-plans to implement this plan. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace the HA WebSocket data source with direct Modbus TCP polling of the SMA Tripower X inverter, reducing data staleness from ~60s to ~1s and simplifying config from 17+ fields to 2.

**Architecture:** A poll thread reads Model 103 (50 regs) and Model 160 (70 regs) from the inverter every 1s via synchronous pymodbus ModbusTcpClient. Values are parsed, nighttime markers replaced with zeros, and written to an in-memory register map. A pymodbus TCP server serves these registers to Gridbox clients at standard SunSpec addresses.

**Tech Stack:** Python 3, pymodbus 3.12.1 (client + server), s6-overlay (HA add-on), Docker

**Spec:** `docs/superpowers/specs/2026-03-17-v2-direct-modbus-design.md`

---

## File Map

| File | Action | Responsibility |
|------|--------|----------------|
| `sma-modbus-proxy/sma_proxy.py` | Rewrite | Main proxy: inverter poller, register updater, Modbus server, logging |
| `sma-modbus-proxy/config.yaml` | Modify | Simplified add-on config: `inverter_ip`, `serial`, `max_power_w` |
| `sma-modbus-proxy/Dockerfile` | Modify | Remove `websockets` dependency |
| `docker-compose.yml` | Create | Standalone deployment option |

Files unchanged: `build.yaml`, `run.sh` (still passes `--options /data/options.json` which the new main() parses for `inverter_ip`), `icon.png`, `logo.png`, `repository.yaml`

Note: `hassio_api: true` and `homeassistant_api: true` are deliberately removed from `config.yaml` — v2 no longer uses the HA API.

---

### Task 1: Update add-on config and Dockerfile

**Files:**
- Modify: `sma-modbus-proxy/config.yaml`
- Modify: `sma-modbus-proxy/Dockerfile`

- [ ] **Step 1: Rewrite config.yaml with new options**

```yaml
name: SMA Modbus Proxy
version: "2.0.0"
slug: sma_modbus_proxy
description: >-
  Modbus TCP proxy for SMA SunSpec inverters. Reads SunSpec Model 103/160
  from the inverter and serves them at standard addresses for energy
  management systems (e.g., Viessmann Gridbox) that can't discover the
  inverter directly.
url: "https://github.com/lackas/ha-sma-modbus-proxy"
arch:
  - aarch64
  - amd64
  - armv7
init: false
host_network: true
options:
  inverter_ip: ""
  serial: 1234567890
  max_power_w: 12000
schema:
  inverter_ip: str
  serial: int
  max_power_w: int
```

- [ ] **Step 2: Update Dockerfile — remove websockets**

```dockerfile
ARG BUILD_FROM
FROM ${BUILD_FROM}

RUN apk add --no-cache python3 py3-pip && \
    pip3 install --no-cache-dir --break-system-packages pymodbus==3.12.1

COPY sma_proxy.py /

COPY run.sh /
RUN chmod a+x /run.sh

CMD ["/run.sh"]
```

- [ ] **Step 3: Commit**

```bash
git add sma-modbus-proxy/config.yaml sma-modbus-proxy/Dockerfile
git commit -m "v2.0: update config and Dockerfile for direct Modbus polling"
```

---

### Task 2: Rewrite sma_proxy.py — static register map and helpers

This task sets up the foundation: imports, logging, static SunSpec register map (Common block, Model 103 skeleton, END marker), SMA proprietary register statics, and helper functions. This is the same code that v1 has for these parts, minus all WebSocket/sensor code.

**Files:**
- Modify: `sma-modbus-proxy/sma_proxy.py`

- [ ] **Step 1: Write the new file header, imports, and logging setup**

```python
#!/usr/bin/env python3
"""SMA Modbus TCP Proxy v2 — direct Modbus polling.

Reads SunSpec Model 103 + 160 from an SMA inverter via Modbus TCP
and serves them at standard SunSpec addresses for energy management
systems (e.g., Viessmann Gridbox).
"""

import argparse
import json
import logging
import os
import threading
import time
from pathlib import Path

from pymodbus.client import ModbusTcpClient
from pymodbus.datastore import ModbusDeviceContext, ModbusServerContext, ModbusSequentialDataBlock
from pymodbus.server import StartTcpServer

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)-7s [%(name)s] %(message)s")


class _SkipSetValues(logging.Filter):
    def filter(self, record):
        return "setValues" not in record.getMessage()


logging.getLogger("pymodbus.logging").setLevel(logging.INFO)
logging.getLogger("pymodbus.logging").addFilter(_SkipSetValues())
logging.getLogger("pymodbus.transport").setLevel(logging.INFO)
log = logging.getLogger("sma_proxy")

# Inverter Modbus settings
INVERTER_UNIT_ID = 126
MODEL_103_ADDR = 41258    # 0-based wire address for Model 103 data (1-based: 41259)
MODEL_160_ADDR = 41414    # 0-based wire address for Model 160 header+data (1-based: 41415)
```

- [ ] **Step 2: Write static register map builder (reuse from v1)**

Copy these functions verbatim from the current `sma_proxy.py` lines 54-155: `str_to_regs`, `sunssf`, `not_impl_u16`, `not_impl_s16`, `build_register_map`, `_u32_words`, `_s32_words`. These build the static SunSpec skeleton and SMA identification registers. No changes needed — they are identical in v2.

Note on addressing conventions (add as comment in the file):
```python
# Addressing: pymodbus ModbusDeviceContext adds +1 internally in setValues/getValues.
# SunSpec registers use 1-based numbering (40001 = first), so we subtract 1 to convert
# to 0-based before pymodbus adds its +1. SMA proprietary registers (30xxx/35xxx) use
# the raw wire address, so setValues +1 and getValues +1 cancel out — no correction needed.
```

- [ ] **Step 3: Commit**

```bash
git add sma-modbus-proxy/sma_proxy.py
git commit -m "v2.0: rewrite sma_proxy.py with static register map"
```

---

### Task 3: Inverter poller — read Model 103 + 160, parse, update registers

The core new code. A function that connects to the inverter, reads registers, parses values applying scale factors, replaces 0xFFFF/0x8000 with zeros, and writes to the Modbus server's register map. Runs in a loop in its own thread.

**Files:**
- Modify: `sma-modbus-proxy/sma_proxy.py`

- [ ] **Step 1: Write the register parsing helpers**

```python
def _safe_u16(val: int) -> int:
    """Return 0 if val is SunSpec 'not implemented' (0xFFFF), else val."""
    return 0 if val == 0xFFFF else val


def _safe_s16(val: int) -> int:
    """Return 0 if val is SunSpec 'not implemented' (0x8000), else signed value."""
    if val == 0x8000:
        return 0
    return val - 0x10000 if val >= 0x8000 else val


def _sf(val: int) -> int:
    """Convert uint16 scale factor to signed int."""
    return val - 0x10000 if val >= 0x8000 else val
```

- [ ] **Step 2: Write the poll_inverter function**

This function:
1. Reads 50 registers at MODEL_103_ADDR (Model 103 data)
2. Reads 70 registers at MODEL_160_ADDR (Model 160 header+data)
3. Parses AC values from Model 103: currents, voltages, power, frequency, yield, operating state
4. Parses DC per-MPPT values from Model 160: current, voltage, power per string
5. Applies inverter scale factors, replaces not-implemented with 0
6. Writes parsed values to SunSpec registers (40073-40122) using v1's scale factors
7. Computes and writes SMA proprietary registers (30xxx/35xxx)
8. Logs periodic AC/DC status

```python
def poll_inverter(client: ModbusTcpClient, store: ModbusDeviceContext,
                  poll_count: list[int]):
    """Read inverter registers and update the Modbus server store.

    Returns True on success, False on error.
    """
    # --- Read Model 103 (50 data registers) ---
    r103 = client.read_holding_registers(MODEL_103_ADDR, count=50, device_id=INVERTER_UNIT_ID)
    if r103.isError():
        log.warning("Failed to read Model 103: %s", r103)
        return False

    # --- Read Model 160 (header + data, 70 registers) ---
    r160 = client.read_holding_registers(MODEL_160_ADDR, count=70, device_id=INVERTER_UNIT_ID)
    if r160.isError():
        log.warning("Failed to read Model 160: %s", r160)
        return False

    d = r103.registers
    m = r160.registers

    # --- Parse Model 103 scale factors ---
    a_sf = _sf(d[4])     # Current SF
    v_sf = _sf(d[11])    # Voltage SF
    w_sf = _sf(d[13])    # Power SF
    hz_sf = _sf(d[15])   # Frequency SF
    va_sf = _sf(d[17])   # Apparent power SF
    var_sf = _sf(d[19])  # Reactive power SF
    pf_sf = _sf(d[21])   # Power factor SF
    wh_sf = _sf(d[24])   # Energy SF
    dca_sf = _sf(d[26])  # DC current SF
    dcv_sf = _sf(d[28])  # DC voltage SF
    dcw_sf = _sf(d[30])  # DC power SF

    # --- Parse Model 103 values (apply SF, replace not-impl with 0) ---
    i_total = _safe_u16(d[0]) * 10**a_sf
    i_l1 = _safe_u16(d[1]) * 10**a_sf
    i_l2 = _safe_u16(d[2]) * 10**a_sf
    i_l3 = _safe_u16(d[3]) * 10**a_sf

    v_l1 = _safe_u16(d[8]) * 10**v_sf
    v_l2 = _safe_u16(d[9]) * 10**v_sf
    v_l3 = _safe_u16(d[10]) * 10**v_sf

    power = _safe_s16(d[12]) * 10**w_sf
    freq = _safe_u16(d[14]) * 10**hz_sf
    va = _safe_s16(d[16]) * 10**va_sf
    reactive = _safe_s16(d[18]) * 10**var_sf
    pf_raw = _safe_s16(d[20]) * 10**pf_sf

    wh_hi = d[22]
    wh_lo = d[23]
    total_wh = ((wh_hi << 16) | wh_lo) * 10**wh_sf

    dc_i = _safe_u16(d[25]) * 10**dca_sf
    dc_v = _safe_u16(d[27]) * 10**dcv_sf
    dc_w = _safe_s16(d[29]) * 10**dcw_sf

    state = _safe_u16(d[36])  # SunSpec operating state
    # Map to v1 convention if not-impl (nighttime returns 0xFFFF → 0)
    if state == 0:
        state = 3 if power == 0 else 4  # Standby or MPPT

    # --- Parse Model 160 per-MPPT data ---
    # Header: m[0]=model_id, m[1]=length
    # Fixed block: m[2]=DCA_SF, m[3]=DCV_SF, m[4]=DCW_SF, m[5]=DCWH_SF
    # m[6-7]=Evt, m[8]=N, m[9]=TmsPer
    # Repeating blocks start at m[10], 20 regs each
    m160_dca_sf = _sf(m[2])
    m160_dcv_sf = _sf(m[3])
    m160_dcw_sf = _sf(m[4])
    n_modules = min(m[8], 3)

    mppt = []
    for i in range(n_modules):
        base = 10 + i * 20
        mppt.append({
            "i": _safe_u16(m[base + 5]) * 10**m160_dca_sf,
            "v": _safe_u16(m[base + 6]) * 10**m160_dcv_sf,
            "w": _safe_s16(m[base + 7]) * 10**m160_dcw_sf,
        })

    # Pad to 3 entries
    while len(mppt) < 3:
        mppt.append({"i": 0, "v": 0, "w": 0})

    # --- Write SunSpec Model 103 registers (v1-compatible scale factors) ---
    # Helper: write to SunSpec 1-based address
    def w(addr, val):
        store.setValues(3, addr - 1, [int(val) & 0xFFFF])

    def w_s16(addr, val):
        v = max(-32768, min(32767, int(val)))
        store.setValues(3, addr - 1, [v & 0xFFFF])

    def w_u32(addr, val):
        store.setValues(3, addr, _u32_words(max(0, int(val))))

    def w_s32(addr, val):
        store.setValues(3, addr, _s32_words(val))

    # Currents (SF=-2)
    w(40073, int(i_total * 100))
    w(40074, int(i_l1 * 100))
    w(40075, int(i_l2 * 100))
    w(40076, int(i_l3 * 100))
    w(40077, sunssf(-2))

    # Phase-to-phase voltages (not available)
    w(40078, 0xFFFF); w(40079, 0xFFFF); w(40080, 0xFFFF)

    # Phase-to-neutral voltages (SF=-1)
    w(40081, int(v_l1 * 10))
    w(40082, int(v_l2 * 10))
    w(40083, int(v_l3 * 10))
    w(40084, sunssf(-1))

    # AC Power (SF=0)
    w_s16(40085, power)
    w(40086, sunssf(0))

    # Frequency (SF=-2)
    w(40087, int(freq * 100))
    w(40088, sunssf(-2))

    # Apparent power VA (SF=0)
    w_s16(40089, va)
    w(40090, sunssf(0))

    # Reactive power VAr (SF=0)
    w_s16(40091, reactive)
    w(40092, sunssf(0))

    # Power factor (SF=-2)
    if va != 0:
        pf = max(-1.0, min(1.0, power / va))
        w_s16(40093, int(pf * 100))
    else:
        w_s16(40093, 0)
    w(40094, sunssf(-2))

    # Total yield (acc32, SF=0, Wh)
    total_wh_int = int(total_wh)
    w(40095, (total_wh_int >> 16) & 0xFFFF)
    w(40096, total_wh_int & 0xFFFF)
    w(40097, sunssf(0))

    # DC current (SF=-2)
    w(40098, int(dc_i * 100))
    w(40099, sunssf(-2))

    # DC voltage (SF=-1)
    w(40100, int(dc_v * 10))
    w(40101, sunssf(-1))

    # DC power (SF=0)
    w_s16(40102, dc_w)
    w(40103, sunssf(0))

    # Temperatures (not available)
    for r in range(40104, 40109):
        w(r, 0x8000)

    # Operating state
    w(40109, state)
    w(40110, 0)

    # Event flags (all clear)
    for r in range(40111, 40123):
        w(r, 0)

    # --- SMA proprietary registers ---
    sma_status = {0: 303, 1: 35, 2: 303, 3: 303, 4: 307, 5: 307, 6: 303, 7: 35}.get(state, 303)

    w_u32(30531, total_wh_int)
    w_s32(30769, dc_w)
    w_s32(30771, v_l1 * i_l1)
    w_s32(30773, v_l2 * i_l2)
    w_s32(30775, power)
    w_u32(30783, v_l1 * 100)
    w_u32(30785, v_l2 * 100)
    w_u32(30787, v_l3 * 100)
    w_u32(30795, freq * 100)
    w_s32(30797, reactive)

    # DC per MPPT string (A + B)
    w_s32(30803, mppt[0]["i"] * 1000)
    w_s32(30805, mppt[0]["v"] * 100)
    w_s32(30807, mppt[0]["w"])
    w_s32(30811, mppt[1]["i"] * 1000)
    w_s32(30813, mppt[1]["v"] * 100)
    w_s32(30815, mppt[1]["w"])

    w_u32(30835, sma_status)

    # MPPT tracker registers (3 strings)
    for idx, base_addr in enumerate([35377, 35383, 35389]):
        w_s32(base_addr, mppt[idx]["w"])
        w_s32(base_addr + 2, mppt[idx]["v"] * 100)
        w_s32(base_addr + 4, mppt[idx]["i"] * 1000)

    # --- Periodic logging ---
    poll_count[0] += 1
    if poll_count[0] % 60 == 1:  # Every 60 polls (~1 min)
        log.info(
            "AC: P=%dW VA=%dVA PF=%.2f V=%.0f/%.0f/%.0fV I=%.1f/%.1f/%.1fA Hz=%.2f",
            int(power), int(va), pf_raw, v_l1, v_l2, v_l3, i_l1, i_l2, i_l3, freq,
        )
        dc_w_total = sum(m["w"] for m in mppt)
        log.info(
            "DC: A=%.0fW(%.1fA/%.0fV) B=%.0fW(%.1fA/%.0fV) C=%.0fW(%.1fA/%.0fV) Tot=%dW Yield=%dWh St=%d(%d)",
            mppt[0]["w"], mppt[0]["i"], mppt[0]["v"],
            mppt[1]["w"], mppt[1]["i"], mppt[1]["v"],
            mppt[2]["w"], mppt[2]["i"], mppt[2]["v"],
            int(dc_w_total), total_wh_int, state, sma_status,
        )

    return True
```

- [ ] **Step 2: Write the poll loop thread function**

```python
def inverter_poll_loop(inverter_ip: str, store: ModbusDeviceContext):
    """Continuously poll the inverter and update the register map."""
    poll_count = [0]
    client = None

    while True:
        if client is None:
            log.info("Connecting to inverter at %s:502 (unit %d)", inverter_ip, INVERTER_UNIT_ID)
            client = ModbusTcpClient(inverter_ip, port=502, timeout=5)
            if not client.connect():
                log.warning("Cannot reach inverter at %s — retrying in 5s", inverter_ip)
                client = None
                time.sleep(5)
                continue
            log.info("Connected to inverter at %s", inverter_ip)

        try:
            if not poll_inverter(client, store, poll_count):
                log.warning("Poll failed — reconnecting in 5s")
                client.close()
                client = None
                time.sleep(5)
                continue
        except Exception as e:
            log.warning("Poll error: %s — reconnecting in 5s", e)
            try:
                client.close()
            except Exception:
                pass
            client = None
            time.sleep(5)
            continue

        time.sleep(1)
```

- [ ] **Step 3: Commit**

```bash
git add sma-modbus-proxy/sma_proxy.py
git commit -m "v2.0: add inverter poller with Model 103+160 parsing"
```

---

### Task 4: Modbus server tracking and main function

Wire everything together: Modbus tracker (carried from v1), config loading (simplified), main function that starts poll thread + Modbus server.

**Files:**
- Modify: `sma-modbus-proxy/sma_proxy.py`

- [ ] **Step 1: Keep _ModbusTracker and _TrackingDeviceContext from v1**

Copy these classes verbatim from the current `sma_proxy.py` lines 163-207: `_ModbusTracker` (with `on_connect`, `on_read`, `report` methods) and `_TrackingDeviceContext` (subclass of `ModbusDeviceContext` with first-read logging). Also copy the module-level `_modbus_tracker = _ModbusTracker()` instance. No changes needed.

- [ ] **Step 2: Write the new main function**

```python
def main():
    parser = argparse.ArgumentParser(description="SMA Modbus TCP Proxy v2")
    parser.add_argument("--inverter-ip", default=None)
    parser.add_argument("--port", type=int, default=502)
    parser.add_argument("--options", default=None)
    args = parser.parse_args()

    serial = int(os.environ.get("SERIAL", "1234567890"))
    max_power_w = int(os.environ.get("MAX_POWER_W", "12000"))
    inverter_ip = os.environ.get("INVERTER_IP", args.inverter_ip)

    if args.options and Path(args.options).exists():
        opts = json.loads(Path(args.options).read_text())
        inverter_ip = opts.get("inverter_ip", inverter_ip)
        serial = opts.get("serial", serial)
        max_power_w = opts.get("max_power_w", max_power_w)

    if not inverter_ip:
        log.error("No inverter_ip configured. Set it in the add-on config or INVERTER_IP env var.")
        return

    log.info("SMA Modbus Proxy v2.0")
    log.info("Inverter: %s, Serial: %d, Max power: %dW", inverter_ip, serial, max_power_w)

    regs = build_register_map(serial)
    block = ModbusSequentialDataBlock(0, [0] * 40001 + regs + [0] * 25000)
    store = _TrackingDeviceContext(hr=block, ir=block)

    # Static SMA identification registers
    store.setValues(3, 30003, [0, 378])
    store.setValues(3, 30005, [(serial >> 16) & 0xFFFF, serial & 0xFFFF])
    store.setValues(3, 30051, [0, 8001])
    store.setValues(3, 30053, [0, 9348])
    store.setValues(3, 30057, [(serial >> 16) & 0xFFFF, serial & 0xFFFF])
    store.setValues(3, 30059, [0x0400, 0x0002])
    store.setValues(3, 30201, [0, 307])
    store.setValues(3, 30231, _u32_words(max_power_w))
    store.setValues(3, 30233, _u32_words(max_power_w))

    context = ModbusServerContext(
        devices={0: store, 1: store, 2: store, 3: store, 247: store},
        single=False,
    )

    # Start inverter poll thread
    threading.Thread(
        target=inverter_poll_loop, args=(inverter_ip, store), daemon=True
    ).start()

    # Start Modbus activity reporter
    def modbus_reporter():
        while True:
            time.sleep(300)
            _modbus_tracker.report()

    threading.Thread(target=modbus_reporter, daemon=True).start()

    log.info("Starting Modbus TCP server on port %d", args.port)
    StartTcpServer(
        context=context,
        address=("0.0.0.0", args.port),
        trace_connect=_modbus_tracker.on_connect,
    )


if __name__ == "__main__":
    main()
```

- [ ] **Step 3: Verify syntax**

```bash
python3 -c "import py_compile; py_compile.compile('sma-modbus-proxy/sma_proxy.py', doraise=True)"
```

Expected: no errors.

- [ ] **Step 4: Commit**

```bash
git add sma-modbus-proxy/sma_proxy.py
git commit -m "v2.0: wire up main with poll thread and Modbus server"
```

---

### Task 5: Add standalone Docker support

**Files:**
- Create: `docker-compose.yml`

- [ ] **Step 1: Create docker-compose.yml**

```yaml
services:
  sma-modbus-proxy:
    build: sma-modbus-proxy
    network_mode: host
    restart: unless-stopped
    environment:
      - INVERTER_IP=192.168.1.216
      - SERIAL=1234567890
      - MAX_POWER_W=12000
```

- [ ] **Step 2: Commit**

```bash
git add docker-compose.yml
git commit -m "v2.0: add docker-compose.yml for standalone deployment"
```

---

### Task 6: Smoke test on live inverter

Test the proxy locally against the real SMA Tripower X at 192.168.1.216.

- [ ] **Step 1: Run proxy locally pointing at inverter**

```bash
cd sma-modbus-proxy
python3 sma_proxy.py --inverter-ip 192.168.1.216 --port 5020
```

Expected log output:
- `SMA Modbus Proxy v2.0`
- `Inverter: 192.168.1.216, Serial: ...`
- `Connected to inverter at 192.168.1.216`
- `AC: P=...` / `DC: A=...` lines after ~1s

- [ ] **Step 2: Query proxy with mbpoll or pymodbus to verify SunSpec chain**

```bash
python3 -c "
from pymodbus.client import ModbusTcpClient
c = ModbusTcpClient('127.0.0.1', port=5020)
c.connect()
# Check SunS header
r = c.read_holding_registers(40000, count=2, device_id=3)
print('SunS:', hex(r.registers[0]), hex(r.registers[1]))
# Check Model 103 power
r = c.read_holding_registers(40084, count=2, device_id=3)
print('AC Power:', r.registers[0], 'SF:', r.registers[1])
# Check yield
r = c.read_holding_registers(40094, count=3, device_id=3)
wh = (r.registers[0] << 16) | r.registers[1]
print('Yield:', wh, 'Wh')
c.close()
"
```

Expected: SunS = 0x5375 0x6E53, yield ~203870 Wh

- [ ] **Step 3: Stop local test, commit any fixes**

```bash
git add sma-modbus-proxy/sma_proxy.py && git commit -m "v2.0: smoke test fixes" --allow-empty
```

---

### Task 7: Deploy to HA and verify with Gridbox

- [ ] **Step 1: Push to GitHub**

```bash
git push
```

- [ ] **Step 2: Update add-on on HA**

Refresh add-on store, update to v2.0.0. Set `inverter_ip` in the add-on config.

- [ ] **Step 3: Verify logs**

Check for:
- `Connected to inverter at 192.168.1.216`
- `AC: P=...` lines every ~60s
- `Modbus client connected` when Gridbox connects
- `First Modbus read (addr=..., count=...) — client polling`

- [ ] **Step 4: Verify Gridbox sees the inverter**

Check Gridbox dashboard for PV production data.

- [ ] **Step 5: Final commit if any tweaks needed**

```bash
git add sma-modbus-proxy/ && git commit -m "v2.0: post-deployment fixes"
git push
```
