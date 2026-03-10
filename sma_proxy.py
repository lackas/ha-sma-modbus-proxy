#!/usr/bin/env python3
"""SMA Modbus TCP Proxy — emulates an SMA Sunny Tripower (SunSpec Model 103).

Reads real data from Home Assistant via WebSocket (live state changes)
and serves it via Modbus TCP so energy management systems (e.g., Viessmann
Gridbox) can discover it as a supported SMA inverter.

Designed to run as a Home Assistant add-on (uses Supervisor WebSocket)
or standalone with --ha-url and --ha-token.
"""

import argparse
import asyncio
import json
import logging
import os
import threading
from pathlib import Path

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

# ---------------------------------------------------------------------------
# Sensor key names (mapped to entity IDs via config)
# ---------------------------------------------------------------------------

SENSOR_KEYS = [
    "power", "current_l1", "current_l2", "current_l3",
    "voltage_l1", "voltage_l2", "voltage_l3",
    "frequency", "reactive", "yield_total", "health",
    "dc_i_a", "dc_v_a", "dc_w_a",
    "dc_i_b", "dc_v_b", "dc_w_b",
]

# ---------------------------------------------------------------------------
# SunSpec register helpers
# ---------------------------------------------------------------------------


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
# Sensor state store + Modbus updater
# ---------------------------------------------------------------------------


class SensorStore:
    def __init__(self, modbus_store: ModbusDeviceContext):
        self.values: dict[str, float | None] = {k: None for k in SENSOR_KEYS}
        self.modbus_store = modbus_store
        self.update_count = 0

    def update(self, key: str, value):
        try:
            self.values[key] = float(value)
        except (ValueError, TypeError):
            self.values[key] = value
        self._refresh_registers()

    def get(self, key: str, default: float = 0.0) -> float:
        v = self.values.get(key)
        return v if isinstance(v, (int, float)) else default

    def _refresh_registers(self):
        store = self.modbus_store

        def w(addr, val):
            store.setValues(3, addr - 1, [val & 0xFFFF])

        def w_s16(addr, val):
            v = max(-32768, min(32767, int(val)))
            store.setValues(3, addr - 1, [v & 0xFFFF])

        def w_u32(addr, val):
            store.setValues(3, addr, _u32_words(val))

        def w_s32(addr, val):
            store.setValues(3, addr, _s32_words(val))

        # --- AC side ---
        power = self.get("power", 0)
        health = self.values.get("health")

        if health is None:
            state = 2  # Sleeping
        elif power > 0:
            state = 4  # MPPT
        else:
            state = 3  # Standby

        sma_status = 307 if state == 4 else 303  # Ok / Aus

        # Currents (SF=-2)
        i_l1 = self.get("current_l1", 0)
        i_l2 = self.get("current_l2", 0)
        i_l3 = self.get("current_l3", 0)
        w(40073, int((i_l1 + i_l2 + i_l3) * 100))
        w(40074, int(i_l1 * 100))
        w(40075, int(i_l2 * 100))
        w(40076, int(i_l3 * 100))

        # Voltages (SF=-1)
        v_l1 = self.get("voltage_l1", 0)
        v_l2 = self.get("voltage_l2", 0)
        v_l3 = self.get("voltage_l3", 0)
        w(40081, int(v_l1 * 10))
        w(40082, int(v_l2 * 10))
        w(40083, int(v_l3 * 10))

        # AC Power (SF=0)
        w_s16(40085, power)

        # Frequency (SF=-2)
        freq = self.get("frequency", 0)
        w(40087, int(freq * 100))

        # Apparent power VA = sum(V*I per phase) (SF=0)
        va = v_l1 * i_l1 + v_l2 * i_l2 + v_l3 * i_l3
        w_s16(40089, va)

        # Reactive power (SF=0)
        reactive = self.get("reactive", 0)
        w_s16(40091, reactive)

        # Power factor = W / VA (SF=-2)
        if va > 0:
            pf = max(-1.0, min(1.0, power / va))
            w_s16(40093, int(pf * 100))
        else:
            w_s16(40093, 0)

        # Total yield (acc32, SF=0, 1 Wh)
        total_wh = int(self.get("yield_total", 0))
        w(40095, (total_wh >> 16) & 0xFFFF)
        w(40096, total_wh & 0xFFFF)

        # --- DC side ---
        dc_i_a = self.get("dc_i_a", 0)
        dc_i_b = self.get("dc_i_b", 0)
        dc_v_a = self.get("dc_v_a", 0)
        dc_v_b = self.get("dc_v_b", 0)
        dc_w_a = self.get("dc_w_a", 0)
        dc_w_b = self.get("dc_w_b", 0)

        # DC current total (SF=-2)
        w(40098, int((dc_i_a + dc_i_b) * 100))

        # DC voltage — power-weighted average (SF=-1)
        dc_w_total = dc_w_a + dc_w_b
        if dc_w_total > 0:
            dc_v_avg = (dc_v_a * dc_w_a + dc_v_b * dc_w_b) / dc_w_total
        elif dc_v_a > 0 or dc_v_b > 0:
            dc_v_avg = max(dc_v_a, dc_v_b)
        else:
            dc_v_avg = 0
        w(40100, int(dc_v_avg * 10))

        # DC power total (SF=0)
        w_s16(40102, dc_w_total)

        # Operating state
        w(40109, state)

        # --- SMA proprietary registers (30xxx/35xxx) ---

        w_u32(30531, total_wh)                     # Total yield (Wh)

        w_s32(30769, power)                        # DC power
        w_s32(30771, v_l1 * i_l1)                  # AC power L1
        w_s32(30773, v_l2 * i_l2)                  # AC power L2
        w_s32(30775, power)                        # Total AC active power

        w_u32(30783, v_l1 * 100)                   # Voltage L1 (0.01 V)
        w_u32(30785, v_l2 * 100)                   # Voltage L2
        w_u32(30787, v_l3 * 100)                   # Voltage L3

        w_u32(30795, freq * 100)                   # Frequency (0.01 Hz)
        w_s32(30797, reactive)                     # Reactive power (var)

        # DC per MPPT string
        w_s32(30803, dc_i_a * 1000)                # String A current (mA)
        w_s32(30805, dc_v_a * 100)                 # String A voltage (0.01 V)
        w_s32(30807, dc_w_a)                       # String A power (W)
        w_s32(30811, dc_i_b * 1000)                # String B current (mA)
        w_s32(30813, dc_v_b * 100)                 # String B voltage (0.01 V)
        w_s32(30815, dc_w_b)                       # String B power (W)

        w_u32(30835, sma_status)                   # Operating status

        # MPPT tracker registers (repeated for Gridbox compatibility)
        w_s32(35377, dc_w_a)                       # MPPT 1 power
        w_s32(35379, dc_v_a * 100)                 # MPPT 1 voltage
        w_s32(35381, dc_i_a * 1000)                # MPPT 1 current
        w_s32(35383, dc_w_b)                       # MPPT 2 power
        w_s32(35385, dc_v_b * 100)                 # MPPT 2 voltage
        w_s32(35387, dc_i_b * 1000)                # MPPT 2 current

        self.update_count += 1
        if self.update_count % 50 == 1:
            log.info(
                "AC: P=%dW VA=%dVA PF=%.2f V=%.0f/%.0f/%.0fV I=%.1f/%.1f/%.1fA Hz=%.2f",
                int(power), int(va), power / va if va > 0 else 0,
                v_l1, v_l2, v_l3, i_l1, i_l2, i_l3, freq,
            )
            log.info(
                "DC: A=%.0fW(%.1fA/%.0fV) B=%.0fW(%.1fA/%.0fV) Tot=%dW Yield=%dWh St=%d(%d)",
                dc_w_a, dc_i_a, dc_v_a, dc_w_b, dc_i_b, dc_v_b,
                int(dc_w_total), total_wh, state, sma_status,
            )


# ---------------------------------------------------------------------------
# WebSocket client
# ---------------------------------------------------------------------------


async def ws_listener(ws_url: str, token: str, sensor_store: SensorStore,
                      entity_to_key: dict[str, str]):
    import websockets

    entity_ids = set(entity_to_key.keys())
    msg_id = 1

    while True:
        try:
            log.info("Connecting to %s", ws_url)
            headers = {"Authorization": f"Bearer {token}"}
            async with websockets.connect(ws_url, additional_headers=headers) as ws:
                msg = json.loads(await ws.recv())
                if msg.get("type") == "auth_required":
                    await ws.send(json.dumps({
                        "type": "auth",
                        "access_token": token,
                    }))
                    msg = json.loads(await ws.recv())
                if msg.get("type") != "auth_ok":
                    log.error("Auth failed: %s", msg)
                    await asyncio.sleep(10)
                    continue
                log.info("Authenticated (HA %s)", msg.get("ha_version", "?"))

                # Get current states
                await ws.send(json.dumps({"id": msg_id, "type": "get_states"}))
                msg_id += 1
                states_msg = json.loads(await ws.recv())
                if states_msg.get("type") == "result" and states_msg.get("success"):
                    for s in states_msg.get("result", []):
                        eid = s.get("entity_id")
                        if eid in entity_to_key:
                            sensor_store.update(entity_to_key[eid], s.get("state"))
                    loaded = {
                        k: sensor_store.values[k]
                        for k in SENSOR_KEYS
                        if sensor_store.values[k] is not None
                    }
                    log.info(
                        "Loaded %d/%d sensors: %s", len(loaded), len(entity_ids),
                        {k: v for k, v in loaded.items()
                         if k in ("power", "dc_w_a", "dc_w_b", "yield_total", "health")},
                    )

                # Subscribe to state changes
                await ws.send(json.dumps({
                    "id": msg_id,
                    "type": "subscribe_events",
                    "event_type": "state_changed",
                }))
                msg_id += 1

                async for raw in ws:
                    msg = json.loads(raw)
                    if msg.get("type") != "event":
                        continue
                    data = msg.get("event", {}).get("data", {})
                    entity_id = data.get("entity_id")
                    if entity_id not in entity_to_key:
                        continue
                    new_state = data.get("new_state", {})
                    value = new_state.get("state")
                    if value not in (None, "unknown", "unavailable"):
                        sensor_store.update(entity_to_key[entity_id], value)

        except Exception as e:
            log.warning("WebSocket error: %s — reconnecting in 5s", e)
            await asyncio.sleep(5)


def start_ws_thread(ws_url: str, token: str, sensor_store: SensorStore,
                    entity_to_key: dict[str, str]):
    def run():
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        loop.run_until_complete(ws_listener(ws_url, token, sensor_store, entity_to_key))

    t = threading.Thread(target=run, daemon=True)
    t.start()
    return t


# ---------------------------------------------------------------------------
# Config helpers
# ---------------------------------------------------------------------------


def build_sensor_map(opts: dict) -> dict[str, str]:
    """Build {entity_id: key} mapping from add-on options."""
    sensors = {}
    for key in SENSOR_KEYS:
        entity_id = opts.get(f"sensor_{key}", "")
        if entity_id:
            sensors[entity_id] = key
    return sensors


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


def main():
    parser = argparse.ArgumentParser(description="SMA Modbus TCP Proxy")
    parser.add_argument("--port", type=int, default=502)
    parser.add_argument("--ha-url", default=None)
    parser.add_argument("--ha-token", default=None)
    parser.add_argument("--options", default=None)
    args = parser.parse_args()

    serial = 1234567890
    max_power_w = 12000
    entity_to_key = {}

    if args.options and Path(args.options).exists():
        opts = json.loads(Path(args.options).read_text())
        args.port = opts.get("port", args.port)
        serial = opts.get("serial", serial)
        max_power_w = opts.get("max_power_w", max_power_w)
        if opts.get("ha_token"):
            args.ha_token = opts["ha_token"]
        entity_to_key = build_sensor_map(opts)

    if not entity_to_key:
        log.error("No sensor entity IDs configured. Set sensor_* options in the add-on config.")
        return

    log.info("Tracking %d sensors: %s", len(entity_to_key),
             {v: k for k, v in entity_to_key.items()})

    if args.ha_token:
        ha_url = args.ha_url or "http://supervisor/core"
        ws_url = ha_url.replace("http://", "ws://").replace("https://", "wss://") + "/api/websocket"
        token = args.ha_token
        log.info("Using configured token, WS: %s", ws_url)
    else:
        supervisor_token = os.environ.get("SUPERVISOR_TOKEN")
        if supervisor_token:
            ws_url = "ws://supervisor/core/websocket"
            token = supervisor_token
            log.info("Using Supervisor token")
        else:
            log.error("No HA token available. Set ha_token in add-on config or run as HA add-on.")
            return

    regs = build_register_map(serial)
    block = ModbusSequentialDataBlock(0, [0] * 40001 + regs + [0] * 25000)
    store = ModbusDeviceContext(hr=block, ir=block)

    # Static SMA identification registers
    store.setValues(3, 30003, [0, 378])                                    # SusyID
    store.setValues(3, 30005, [(serial >> 16) & 0xFFFF, serial & 0xFFFF])  # Serial
    store.setValues(3, 30051, [0, 8001])                                   # Device class: Solar Inverter
    store.setValues(3, 30053, [0, 9348])                                   # Device type: STP 10.0-3AV-40
    store.setValues(3, 30057, [(serial >> 16) & 0xFFFF, serial & 0xFFFF])  # Serial (repeated)
    store.setValues(3, 30059, [0x0400, 0x0002])                            # Software package
    store.setValues(3, 30201, [0, 307])                                    # Grid relay: Closed
    store.setValues(3, 30231, _u32_words(max_power_w))                     # Max active power (W)
    store.setValues(3, 30233, _u32_words(max_power_w))                     # Max apparent power (VA)

    context = ModbusServerContext(
        devices={0: store, 1: store, 2: store, 3: store, 247: store},
        single=False,
    )
    sensor_store = SensorStore(store)

    start_ws_thread(ws_url, token, sensor_store, entity_to_key)

    log.info("Starting Modbus TCP on port %d (serial: %d, max: %dW)", args.port, serial, max_power_w)
    StartTcpServer(context=context, address=("0.0.0.0", args.port))


if __name__ == "__main__":
    main()
