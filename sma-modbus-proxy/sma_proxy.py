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

# ---------------------------------------------------------------------------
# SunSpec register helpers
# ---------------------------------------------------------------------------

# Addressing: pymodbus ModbusDeviceContext adds +1 internally in setValues/getValues.
# SunSpec registers use 1-based numbering (40001 = first), so we subtract 1 to convert
# to 0-based before pymodbus adds its +1. SMA proprietary registers (30xxx/35xxx) use
# the raw wire address, so setValues +1 and getValues +1 cancel out — no correction needed.


def str_to_regs(s: str, num_regs: int) -> list[int]:
    b = s.encode("ascii")[:num_regs * 2]
    b = b.ljust(num_regs * 2, b"\x00")
    return [int.from_bytes(b[i:i + 2], "big") for i in range(0, len(b), 2)]


def sunssf(val: int) -> int:
    return val & 0xFFFF


def not_impl_u16():
    return 0xFFFF


def not_impl_s16():
    return 0x8000


# ---------------------------------------------------------------------------
# Build static SunSpec register map (addresses 40001-40124)
# ---------------------------------------------------------------------------


def build_register_map(serial: int) -> list[int]:
    regs = [0] * 124

    def w(addr, val):
        regs[addr - 40001] = val & 0xFFFF

    def w_str(addr, s, num_regs):
        for i, v in enumerate(str_to_regs(s, num_regs)):
            w(addr + i, v)

    w(40001, 0x5375); w(40002, 0x6E53)  # "SunS"

    # Model 1: Common (66 registers)
    w(40003, 1); w(40004, 66)
    w_str(40005, "SMA", 16)
    w_str(40021, "STP 10.0-3AV-40", 16)
    w_str(40037, "", 8)
    w_str(40045, "04.00.02.R", 8)
    w_str(40053, str(serial), 16)
    w(40069, 3); w(40070, 0x8000)

    # Model 103: Three Phase Inverter (50 registers)
    w(40071, 103); w(40072, 50)
    # Currents (SF=-2)
    w(40073, 0); w(40074, 0); w(40075, 0); w(40076, 0)
    w(40077, sunssf(-2))
    # Phase-to-phase voltages (not available)
    w(40078, not_impl_u16()); w(40079, not_impl_u16()); w(40080, not_impl_u16())
    # Phase-to-neutral voltages (SF=-1)
    w(40081, 0); w(40082, 0); w(40083, 0)
    w(40084, sunssf(-1))
    # AC Power (SF=0)
    w(40085, 0); w(40086, sunssf(0))
    # Frequency (SF=-2)
    w(40087, 0); w(40088, sunssf(-2))
    # Apparent power VA (SF=0)
    w(40089, 0); w(40090, sunssf(0))
    # Reactive power VAr (SF=0)
    w(40091, 0); w(40092, sunssf(0))
    # Power factor (SF=-2)
    w(40093, 0); w(40094, sunssf(-2))
    # Total yield (acc32, SF=0, 1 Wh)
    w(40095, 0); w(40096, 0); w(40097, sunssf(0))
    # DC current (SF=-2)
    w(40098, 0); w(40099, sunssf(-2))
    # DC voltage (SF=-1)
    w(40100, 0); w(40101, sunssf(-1))
    # DC power (SF=0)
    w(40102, 0); w(40103, sunssf(0))
    # Temperatures (not available)
    w(40104, not_impl_s16()); w(40105, not_impl_s16())
    w(40106, not_impl_s16()); w(40107, not_impl_s16())
    w(40108, not_impl_s16())
    # Operating state
    w(40109, 4); w(40110, 0)
    # Event flags (all clear)
    for r in range(40111, 40123):
        w(r, 0)
    # End marker
    w(40123, 0xFFFF); w(40124, 0x0000)

    return regs


# ---------------------------------------------------------------------------
# SMA register helpers (U32/S32)
# ---------------------------------------------------------------------------


def _u32_words(val: int) -> list[int]:
    val = max(0, int(val))
    return [(val >> 16) & 0xFFFF, val & 0xFFFF]


def _s32_words(val: float) -> list[int]:
    v = int(val)
    if v < 0:
        v += 0x100000000
    return [(v >> 16) & 0xFFFF, v & 0xFFFF]


# ---------------------------------------------------------------------------
# Inverter register parsing helpers
# ---------------------------------------------------------------------------


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


# ---------------------------------------------------------------------------
# Inverter poller
# ---------------------------------------------------------------------------


def poll_inverter(client: ModbusTcpClient, store: ModbusDeviceContext,
                  poll_count: list[int]):
    """Read inverter registers and update the Modbus server store.

    Returns the SunSpec operating state on success (>= 0), or -1 on error.
    """
    # --- Read Model 103 (50 data registers) ---
    r103 = client.read_holding_registers(MODEL_103_ADDR, count=50, device_id=INVERTER_UNIT_ID)
    if r103.isError():
        log.warning("Failed to read Model 103: %s", r103)
        return -1

    # --- Read Model 160 (header + data, 70 registers) ---
    r160 = client.read_holding_registers(MODEL_160_ADDR, count=70, device_id=INVERTER_UNIT_ID)
    if r160.isError():
        log.warning("Failed to read Model 160: %s", r160)
        return -1

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
    # Map to v1 convention if not-impl (nighttime returns 0xFFFF -> 0)
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
    def w(addr, val):
        """Write single SunSpec register (1-based addressing)."""
        store.setValues(3, addr - 1, [int(val) & 0xFFFF])

    def w_s16(addr, val):
        """Write signed int16 SunSpec register (1-based addressing)."""
        v = max(-32768, min(32767, int(val)))
        store.setValues(3, addr - 1, [v & 0xFFFF])

    def w_u32(addr, val):
        """Write unsigned 32-bit SMA register (raw wire addressing)."""
        store.setValues(3, addr, _u32_words(max(0, int(val))))

    def w_s32(addr, val):
        """Write signed 32-bit SMA register (raw wire addressing)."""
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

    return state


POLL_ACTIVE = 1       # Producing power: poll every 1s
POLL_STANDBY = 60     # Standby/night: poll every 60s
POLL_ERROR_MIN = 5    # First error retry: 5s
POLL_ERROR_MAX = 300  # Max error backoff: 5 min


def inverter_poll_loop(inverter_ip: str, store: ModbusDeviceContext):
    """Continuously poll the inverter and update the register map."""
    poll_count = [0]
    client = None
    error_backoff = POLL_ERROR_MIN
    prev_interval = None

    while True:
        if client is None:
            log.info("Connecting to inverter at %s:502 (unit %d)", inverter_ip, INVERTER_UNIT_ID)
            client = ModbusTcpClient(inverter_ip, port=502, timeout=5)
            if not client.connect():
                log.warning("Cannot reach inverter at %s — retrying in %ds", inverter_ip, error_backoff)
                client = None
                time.sleep(error_backoff)
                error_backoff = min(error_backoff * 2, POLL_ERROR_MAX)
                continue
            log.info("Connected to inverter at %s", inverter_ip)
            error_backoff = POLL_ERROR_MIN

        try:
            state = poll_inverter(client, store, poll_count)
        except Exception as e:
            state = -1
            log.warning("Poll error: %s", e)

        if state < 0:
            log.warning("Poll failed — retrying in %ds", error_backoff)
            try:
                client.close()
            except Exception:
                pass
            client = None
            time.sleep(error_backoff)
            error_backoff = min(error_backoff * 2, POLL_ERROR_MAX)
            continue

        # Reset error backoff on success
        error_backoff = POLL_ERROR_MIN

        # Adaptive poll interval: 1s when producing, 60s on standby/night
        if state == 4 or state == 5:  # MPPT or Throttled
            interval = POLL_ACTIVE
        else:
            interval = POLL_STANDBY

        if interval != prev_interval:
            log.info("Poll interval: %ds (state=%d)", interval, state)
            prev_interval = interval

        time.sleep(interval)


# ---------------------------------------------------------------------------
# Modbus server tracking
# ---------------------------------------------------------------------------


class _ModbusTracker:
    """Tracks Modbus client connections and read activity."""

    def __init__(self):
        self._clients_seen: set[str] = set()
        self._read_count = 0
        self._last_report_count = 0

    def on_connect(self, connected: bool) -> None:
        """Called by pymodbus trace_connect (True=connect, False=disconnect)."""
        if connected:
            log.info("Modbus client connected")
        else:
            log.info("Modbus client disconnected")

    def on_read(self):
        self._read_count += 1

    def report(self):
        """Called periodically to report activity."""
        count = self._read_count
        delta = count - self._last_report_count
        self._last_report_count = count
        if count == 0:
            log.info("Modbus: no reads received yet — waiting for client")
        else:
            log.info("Modbus: %d reads total (%d since last report)", count, delta)


_modbus_tracker = _ModbusTracker()


class _TrackingDeviceContext(ModbusDeviceContext):
    """Wraps ModbusDeviceContext to log first Modbus read per startup."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._first_read_logged = False

    def getValues(self, fc_as_hex, address, count=1):
        _modbus_tracker.on_read()
        if not self._first_read_logged:
            self._first_read_logged = True
            log.info("First Modbus read (addr=%d, count=%d) — client polling", address, count)
        return super().getValues(fc_as_hex, address, count)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


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
