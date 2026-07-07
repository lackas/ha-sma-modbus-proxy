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

import requests
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
wlog = logging.getLogger("sma_proxy.writes")


def _install_connection_tracing() -> None:
    """Log peer IP:port + disconnect reason for every client connection.

    pymodbus labels every accepted server connection "server" with no peer
    info, so a reconnect storm can't be attributed to one client vs. many, and
    its own lifecycle lines only surface if the "pymodbus.logging" logger is
    unmuted. To make the FIRST capture conclusive, wrap the request handler's
    connect/disconnect callbacks and log the real peer + the close exception on
    our own logger (DEBUG, so silent at info/warning). Guarded so an unexpected
    pymodbus layout can never crash the proxy — worst case we lose peer detail
    and still have the unmuted pymodbus logs.
    """
    try:
        from pymodbus.server.requesthandler import ServerRequestHandler as _SRH
    except Exception as e:  # pragma: no cover - version guard
        log.debug("Connection tracing not installed (import failed): %s", e)
        return

    _orig_made = _SRH.connection_made
    _orig_lost = _SRH.connection_lost

    def _made(self, transport):
        try:
            log.debug("conn OPEN  peer=%s", transport.get_extra_info("peername"))
        except Exception:
            pass
        return _orig_made(self, transport)

    def _lost(self, exc):
        try:
            peer = self.transport.get_extra_info("peername") if self.transport else None
        except Exception:
            peer = None
        log.debug("conn CLOSE peer=%s reason=%r", peer, exc)
        return _orig_lost(self, exc)

    _SRH.connection_made = _made
    _SRH.connection_lost = _lost
    log.debug("Connection tracing installed (peer + disconnect reason)")

VERSION = "2.4.2"

# ---------------------------------------------------------------------------
# Home Assistant push (Supervisor API)
# ---------------------------------------------------------------------------
# Inside an HA add-on, SUPERVISOR_TOKEN is auto-injected by the supervisor
# when homeassistant_api: true is set in config.yaml. The proxy POSTs only
# on curtailment state-change events + 1/min heartbeat while active — no
# polling load on HA.

HA_API = "http://supervisor/core/api"
HA_TOKEN = os.environ.get("SUPERVISOR_TOKEN", "")
_ha_push_enabled = False


def _push_ha_state(entity_id: str, state, attributes: dict | None = None) -> None:
    if not _ha_push_enabled or not HA_TOKEN:
        return
    try:
        r = requests.post(
            f"{HA_API}/states/{entity_id}",
            headers={"Authorization": f"Bearer {HA_TOKEN}",
                     "Content-Type": "application/json"},
            json={"state": state, "attributes": attributes or {}},
            timeout=2,
        )
        if not r.ok:
            log.warning("HA push %s returned HTTP %d: %s",
                        entity_id, r.status_code, r.text[:120])
    except Exception as e:
        log.warning("HA push failed (%s): %s", entity_id, e)


# Active curtailment mode, set in main(). Published as an attribute so the HA
# lost-energy sensor knows if the VX3 is also curtailed (modbus) or only the SMA.
_curtail_mode = "off"


def _push_curtailment(pct_int: int, curtailed: bool) -> None:
    _push_ha_state(
        "sensor.sma_curtailment_setpoint",
        pct_int,
        {"unit_of_measurement": "%",
         "friendly_name": "SMA Curtailment Setpoint",
         "icon": "mdi:transmission-tower-off",
         "mode": _curtail_mode},
    )
    _push_ha_state(
        "binary_sensor.sma_curtailed",
        "on" if curtailed else "off",
        {"friendly_name": "SMA Curtailed",
         "device_class": "problem",
         "setpoint_pct": pct_int,
         "icon": "mdi:transmission-tower-off"},
    )


# SunSpec Model 103 St (Operating State) enum — see SunSpec Inverter Model spec.
OP_STATE_NAMES = {
    1: "off",
    2: "sleeping",
    3: "starting",
    4: "mppt",
    5: "throttled",
    6: "shutting_down",
    7: "fault",
    8: "standby",
}

# Push dedup + heartbeat for operating state. Single-threaded poller, no lock.
_last_op_state_pushed: int | None = None
_last_op_state_push_t: float = 0.0
OP_STATE_HEARTBEAT_S = 300   # re-seed every 5 min so HA restarts recover quickly


def _push_operating_state(st: int) -> None:
    name = OP_STATE_NAMES.get(st, f"unknown_{st}")
    _push_ha_state(
        "sensor.sma_operating_state",
        name,
        {"friendly_name": "SMA Operating State",
         "icon": "mdi:solar-power",
         "sunspec_st": st},
    )


def _maybe_push_op_state(st: int) -> None:
    global _last_op_state_pushed, _last_op_state_push_t
    now_t = time.monotonic()
    if st != _last_op_state_pushed or (now_t - _last_op_state_push_t) >= OP_STATE_HEARTBEAT_S:
        _push_operating_state(st)
        _last_op_state_pushed = st
        _last_op_state_push_t = now_t


# Last-read Model 103 inverter temperatures (°C). Updated each poll, logged
# at DEBUG every 5 min by inverter_poll_loop. None = not implemented by inverter.
_last_temps: dict[str, float | None] = {"cab": None, "snk": None, "trns": None, "oth": None}

# Last-read SMA AC power (W) + monotonic timestamp, read by the curtail feedforward.
_latest_sma: dict[str, float] = {"power_w": 0.0, "ts": 0.0}

# ---------------------------------------------------------------------------
# External-write handling: log + translate + forward (Modbus FC6 / FC16 from
# clients like gridX / Gridbox)
# ---------------------------------------------------------------------------
#
# gridX speaks the *legacy* SMA Modbus profile (40024 WMaxLimPct%, 40212 WMod
# enum, 43091 Grid Guard). The STP X 12-50 with ennexOS firmware does NOT
# expose those addresses — instead it offers SunSpec Model 123 (Immediate
# Controls) for active power limit / cos φ.
#
# This proxy translates incoming legacy writes into the equivalent SunSpec
# Model 123 writes on the real inverter. Discovered on startup; the actual
# wire addresses depend on the inverter firmware's model chain layout.

# SMA TAGLIST values for WMod (legacy enum register; we log these but do not
# need to forward — Model 123 has its own Ena bit).
WMOD_ENUM = {
    303: "Off",
    1077: "Active power limit P in W",
    1078: "Active power limit P in %",
    1079: "Active power limit P by plant control",
    1080: "Active power setpoint",
}

# Legacy SMA register metadata for human-friendly decode. These addresses are
# what gridX writes — they do NOT exist on the STP X (ennexOS); we translate
# them to Model 123 instead.
KNOWN_WRITE_REGS = {
    40024: {
        "name": "Legacy WMaxLimPct",
        "desc": "Active power limit % (0.01 %-units), legacy SMA profile",
        "kind": "u16_pct_sf2",  # value / 100 = percent
        "expect_count": 1,
    },
    40212: {
        "name": "Legacy WMod",
        "desc": "Active power control mode (legacy SMA enum)",
        "kind": "u32_enum",
        "enums": WMOD_ENUM,
        "expect_count": 2,
    },
    43092: {
        "name": "Legacy Grid Guard",
        "desc": "SMA Grid Guard code (legacy auth, not needed for SunSpec)",
        "kind": "u32_raw",
        "expect_count": 2,
    },
}

# Known SunSpec model IDs (for discovery logging)
SUNSPEC_MODEL_NAMES = {
    1: "Common",
    103: "Three Phase Inverter (int)",
    120: "Nameplate",
    121: "Basic Settings",
    122: "Measurements/Status",
    123: "Immediate Controls",
    160: "Multiple MPPT",
    701: "DER AC Measurement",
    702: "DER Capacity",
    703: "DER Enter Service",
    704: "DER AC Controls",
    705: "DER Volt-Var",
    706: "DER Volt-Watt",
    707: "DER Trip LV",
    708: "DER Trip HV",
    709: "DER Trip LF",
    710: "DER Trip HF",
    711: "DER Frequency Droop",
    712: "DER Watt-Var",
    714: "DER DC Measurement",
}

# SunSpec Model 123 offset table (relative to data base wire address)
M123_OFFSET = {
    "Conn":            2,   # u16 enum 0=Disconn, 1=Conn
    "WMaxLimPct":      3,   # u16, %WMax, scale by WMaxLimPct_SF (typically -2)
    "WMaxLim_Ena":     7,   # u16 enum 0=Off, 1=On
    "OutPFSet":        8,   # s16, cos φ
    "OutPFSet_Ena":   12,
    "WMaxLimPct_SF":  21,   # sunssf
    "OutPFSet_SF":    22,
}


def _format_known_write(wire_addr: int, values: list[int]) -> str | None:
    """Return human-friendly description for known register, or None if unknown."""
    meta = KNOWN_WRITE_REGS.get(wire_addr)
    if not meta:
        return None
    kind = meta["kind"]
    name = meta["name"]
    desc = meta["desc"]

    if kind == "u16_fix4" and len(values) >= 1:
        v = values[0]
        return f"{name} = {v / 10000:.4f}  (raw={v})  — {desc}"
    if kind == "u16_pct_sf2" and len(values) >= 1:
        v = values[0]
        return f"{name} = {v / 100:.2f} %  (raw={v})  — {desc}"
    if kind == "u32_enum" and len(values) >= 2:
        v = (values[0] << 16) | values[1]
        enum_name = meta["enums"].get(v, f"unknown enum {v}")
        return f"{name} = {v} ({enum_name})  — {desc}"
    if kind == "u32_raw" and len(values) >= 2:
        v = (values[0] << 16) | values[1]
        return f"{name} = 0x{v:08X} ({v})  — {desc}"
    return None


def _format_raw_write(wire_addr: int, values: list[int]) -> str:
    """Fallback raw decode for unknown registers."""
    hex_vals = " ".join(f"{v:04x}" for v in values)
    dec_vals = " ".join(str(v) for v in values)
    extra = ""
    if len(values) == 1:
        v = values[0]
        sv = v - 0x10000 if v >= 0x8000 else v
        extra = f"  u16={v}  s16={sv}"
    elif len(values) == 2:
        u32 = (values[0] << 16) | values[1]
        s32 = u32 - 0x100000000 if u32 >= 0x80000000 else u32
        extra = f"  u32={u32}  s32={s32}"
    return f"addr={wire_addr} words=[{hex_vals}] dec=[{dec_vals}]{extra}"


def discover_sunspec_models(inverter_ip: str, unit_id: int = 126,
                            port: int = 502) -> dict[int, int]:
    """Walk the SunSpec model chain on the inverter.

    Returns {model_id: data_wire_address}. Empty dict on failure.
    The data_wire_address points to the first data register of the model
    (after the 2-register header).
    """
    log.info("Discovery: connecting to inverter %s for SunSpec walk", inverter_ip)
    client = ModbusTcpClient(inverter_ip, port=port, timeout=5)
    if not client.connect():
        log.error("Discovery: cannot connect to %s — translation will be disabled", inverter_ip)
        return {}
    try:
        rr = client.read_holding_registers(40000, count=2, device_id=unit_id)
        if rr.isError() or rr.registers != [0x5375, 0x6E53]:
            log.error("Discovery: no SunS marker at wire 40001 — got %r", rr)
            return {}
        log.info("Discovery: SunS marker found ✓")

        models = {}
        addr = 40002  # PDU for first model header (wire 40003)
        for _ in range(50):  # safety cap
            hdr = client.read_holding_registers(addr, count=2, device_id=unit_id)
            if hdr.isError():
                log.warning("Discovery: read failed at PDU %d — stopping walk", addr)
                break
            model_id, model_len = hdr.registers[0], hdr.registers[1]
            if model_id == 0xFFFF:
                log.info("Discovery: end-of-chain marker at PDU %d (wire %d)",
                         addr, addr + 1)
                break
            data_wire = addr + 1 + 2  # header wire + 2 = first data wire
            models[model_id] = data_wire
            name = SUNSPEC_MODEL_NAMES.get(model_id, "<unknown>")
            log.info("Discovery: Model %3d (%-30s) data @ wire %d, len=%d",
                     model_id, name, data_wire, model_len)
            addr += 2 + model_len

        log.info("Discovery: %d SunSpec models total", len(models))
        return models
    finally:
        client.close()


class _CurtailmentTracker:
    """Track curtailment state for clean INFO-level logging.

    Detects state transitions (free ↔ curtailed) and emits a one-line INFO
    event on each change. A 60-second heartbeat dumps the current state +
    setpoint range so the log shows "things are working" without spam.
    """

    # Free-running heartbeat: re-push (100, False) every N 60-s ticks so HA
    # restarts at night recover the entity within 5 min instead of staying
    # ghost until the next curtailment event.
    FREE_HEARTBEAT_TICKS = 5

    # No Modbus write for this long while "curtailed" -> assume the inverter
    # went idle (night) and the last write left us pinned. Release to free.
    # Active curtailment writes far more often than this, so it never trips
    # during a genuine cap.
    CURTAIL_STALE_S = 600

    def __init__(self):
        self._lock = threading.Lock()
        self._state = "unknown"          # "free" | "curtailed" | "unknown"
        self._last_pct: float | None = None
        self._writes_in_interval = 0
        self._pct_min: float | None = None
        self._pct_max: float | None = None
        self._last_write_ts: float = 0.0
        # HA push dedup: track last pushed (pct_int, curtailed_bool). Push
        # only when either changes. Both checks are needed: gridX can write
        # (pct=100, ena=1) before releasing, so a release at pct=100 still
        # needs to flip the binary_sensor even though pct didn't move.
        self._last_pushed: tuple[int, bool] | None = None
        self._free_tick_counter = 0

    def record(self, pct_raw: int, ena: int) -> None:
        """Record one translate event and emit state-change INFO if needed."""
        pct = pct_raw / 100.0
        new_state = "free" if ena == 0 else "curtailed"

        with self._lock:
            self._writes_in_interval += 1
            self._last_write_ts = time.time()
            self._last_pct = pct
            if self._pct_min is None or pct < self._pct_min:
                self._pct_min = pct
            if self._pct_max is None or pct > self._pct_max:
                self._pct_max = pct
            old_state = self._state
            self._state = new_state

        if old_state != new_state and old_state != "unknown":
            if new_state == "curtailed":
                log.info("Curtailment STARTED — setpoint %.2f %%", pct)
            else:
                log.info("Curtailment RELEASED — setpoint %.2f %%", pct)
        elif old_state == "unknown":
            label = "curtailed" if new_state == "curtailed" else "free"
            log.info("First translate: STP X %s (setpoint %.2f %%)", label, pct)

        # Push when either setpoint or curtailment state changes. "Free"
        # always reports as 100 % regardless of last raw pct write.
        curtailed = (new_state == "curtailed")
        display_pct_int = round(pct) if curtailed else 100
        snapshot = (display_pct_int, curtailed)
        if snapshot != self._last_pushed:
            _push_curtailment(display_pct_int, curtailed)
            self._last_pushed = snapshot

    def emit_heartbeat(self) -> None:
        """Called every 60 s — log INFO summary only while curtailment is active.

        Free / unknown states stay silent: state-change events (STARTED /
        RELEASED) cover transitions, and the existing 5-min Modbus read
        stats prove the proxy is alive.
        """
        with self._lock:
            count = self._writes_in_interval
            pct_min = self._pct_min
            pct_max = self._pct_max
            state = self._state
            last = self._last_pct
            last_write_ts = self._last_write_ts
            # reset for next interval
            self._writes_in_interval = 0
            self._pct_min = None
            self._pct_max = None

        # state only flips on a write; when the inverter goes idle (night) it
        # stops writing and we stay pinned at "curtailed" from the last daytime
        # write, holding a ghost setpoint. Real curtailment writes sub-second,
        # so a long write-silence means no active control: release to free.
        if state == "curtailed" and (time.time() - last_write_ts) > self.CURTAIL_STALE_S:
            with self._lock:
                self._state = "free"
            log.info("Curtailment RELEASED (stale, no writes for %.0f s)",
                     time.time() - last_write_ts)
            state = "free"

        if state != "curtailed":
            # Free-running 5-min heartbeat: re-push (100, off) so HA restarts
            # recover the entity even at night when no state changes happen.
            self._free_tick_counter += 1
            if self._free_tick_counter >= self.FREE_HEARTBEAT_TICKS:
                self._free_tick_counter = 0
                _push_curtailment(100, False)
                with self._lock:
                    self._last_pushed = (100, False)
            return

        # Active curtailment: reset free-heartbeat counter so a new free run
        # starts its 5-min clock fresh.
        self._free_tick_counter = 0

        if count == 0 or pct_min is None:
            # Still curtailed but no fresh write this interval: report the held
            # setpoint instead of formatting None.
            log.info("Curtailment: 0 writes, holding %s %% (steady)",
                     "?" if last is None else "%.2f" % last)
        elif pct_min == pct_max:
            log.info("Curtailment: %d writes, setpoint %.2f %% (steady)",
                     count, pct_min)
        else:
            log.info("Curtailment: %d writes, %.2f–%.2f %% (now %.2f %%)",
                     count, pct_min, pct_max, last)
        # 1/min heartbeat-refresh to HA while actively curtailed — recovers
        # the sensor state if HA restarted or lost the original push.
        if last is not None:
            last_int = round(last)
            _push_curtailment(last_int, True)
            with self._lock:
                self._last_pushed = (last_int, True)


_curtailment_tracker = _CurtailmentTracker()


def _heartbeat_loop():
    """Background thread: emit curtailment heartbeat every 60 s."""
    while True:
        time.sleep(60)
        try:
            _curtailment_tracker.emit_heartbeat()
        except Exception as e:
            log.warning("Heartbeat error: %s", e)


def _translate_legacy_to_m123(client, m123_data_wire: int, unit_id: int,
                              wire_addr: int, vals: list[int]) -> bool:
    """Translate a legacy SMA write to a SunSpec Model 123 write.

    Returns True if the write was recognized and handled (whether forward
    succeeded or not — caller should not also do a raw forward).
    Returns False if no translation rule applies; caller can decide what to do.
    """
    if m123_data_wire is None:
        return False

    if wire_addr == 40024 and len(vals) == 1:
        # gridX writes WMaxLimPct in 0.01 %-units (5808 → 58.08 %)
        # Model 123 WMaxLimPct uses the same 0.01 %-scale (SF=-2) — no conversion
        pct_raw = vals[0]
        pct = pct_raw / 100.0
        ena = 0 if pct_raw >= 10000 else 1  # disable limit at 100 %
        wlog.debug("TRANSLATE  WMaxLimPct = %.2f %% (raw=%d), Ena = %d → Model 123",
                   pct, pct_raw, ena)

        pdu_pct = (m123_data_wire - 1) + M123_OFFSET["WMaxLimPct"]
        pdu_ena = (m123_data_wire - 1) + M123_OFFSET["WMaxLim_Ena"]

        # We always write both. SMA's design assumes cyclic writes; skipping the
        # Ena on the assumption it's already correct breaks the self-healing
        # property — if the inverter is power-cycled or resets, its Ena returns
        # to default, and we'd never re-assert it. The 1 Hz cycle cost is well
        # under SMA's documented "cyclically OK" budget for grid-management
        # registers.
        try:
            if not client.connected:
                if not client.connect():
                    wlog.warning("  ✗ inverter connection lost — translation skipped")
                    return True
            rr = client.write_register(pdu_pct, pct_raw, device_id=unit_id)
            if rr.isError():
                wlog.warning("  ✗ write WMaxLimPct (wire %d) failed: %s",
                             m123_data_wire + M123_OFFSET["WMaxLimPct"], rr)
            else:
                wlog.debug("  ✓ wire %d = %d (M123 WMaxLimPct)",
                           m123_data_wire + M123_OFFSET["WMaxLimPct"], pct_raw)

            rr2 = client.write_register(pdu_ena, ena, device_id=unit_id)
            if rr2.isError():
                wlog.warning("  ✗ write WMaxLim_Ena (wire %d) failed: %s",
                             m123_data_wire + M123_OFFSET["WMaxLim_Ena"], rr2)
            else:
                wlog.debug("  ✓ wire %d = %d (M123 WMaxLim_Ena)",
                           m123_data_wire + M123_OFFSET["WMaxLim_Ena"], ena)
        except Exception as e:
            wlog.warning("  ✗ translate exception: %s — closing forward client for fresh reconnect", e)
            # pymodbus' `connected` flag is not always flipped on EPIPE/timeout,
            # so the next translate call would reuse the dead socket and fail
            # again. Force-close here so the `if not client.connected` guard at
            # the top of the next call sees a broken state and reconnects.
            try:
                client.close()
            except Exception:
                pass

        # Track state for INFO heartbeat / state-change events
        _curtailment_tracker.record(pct_raw, ena)
        return True

    if wire_addr == 40211 and len(vals) == 2:
        u32 = (vals[0] << 16) | vals[1]
        wlog.debug("TRANSLATE skip: WMod=%d (Model 123 has own Ena bit)", u32)
        return True

    if wire_addr == 43091:
        wlog.debug("TRANSLATE skip: Grid Guard (SunSpec needs no auth)")
        return True

    return False


class LoggingDataBlock(ModbusSequentialDataBlock):
    """Modbus data block that logs writes from external clients.

    Internal store updates (driven by poll_inverter and static identity setup)
    flip ``_silent`` to True so they don't drown the log. Any setValues() call
    while ``_silent`` is False is treated as client-originated and logged —
    and, if translation is configured, mapped to SunSpec Model 123 writes
    on the real inverter.
    """

    _silent = False
    _forward_client = None       # type: ModbusTcpClient | None
    _forward_unit_id = 126
    _m123_data_wire: int | None = None  # base wire address of Model 123 data

    @classmethod
    def silent(cls, value: bool = True):
        cls._silent = value

    @classmethod
    def enable_forwarding(cls, client, unit_id: int = 126,
                          m123_data_wire: int | None = None):
        cls._forward_client = client
        cls._forward_unit_id = unit_id
        cls._m123_data_wire = m123_data_wire

    def setValues(self, address, values):
        if not LoggingDataBlock._silent:
            _modbus_tracker.on_write()
            try:
                vals = list(values) if isinstance(values, (list, tuple)) else [values]
                # pymodbus ModbusDeviceContext has already added +1 — so ``address``
                # is the 1-based wire register.
                nice = _format_known_write(address, vals)
                alt_label = ""
                if nice is None:
                    nice = _format_known_write(address + 1, vals)
                    if nice is not None:
                        alt_label = f" (matched @ addr+1={address + 1})"

                if nice is not None:
                    wlog.debug("WRITE  %s%s  [addr=%d, count=%d]",
                               nice, alt_label, address, len(vals))
                else:
                    raw_a = _format_raw_write(address, vals)
                    raw_b = _format_raw_write(address + 1, vals)
                    # Unknown writes stay at WARNING — they need attention
                    wlog.warning("WRITE  UNKNOWN  %s  |  alt: %s",
                                 raw_a, raw_b)

                if LoggingDataBlock._forward_client is not None:
                    _translate_legacy_to_m123(
                        LoggingDataBlock._forward_client,
                        LoggingDataBlock._m123_data_wire,
                        LoggingDataBlock._forward_unit_id,
                        address, vals,
                    )
            except Exception as e:
                wlog.warning("WRITE logging/forward error addr=%d values=%r: %s",
                             address, values, e)
        return super().setValues(address, values)

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


def build_register_map(serial: int, device_identifier: str = "STP 10.0-3AV-40") -> list[int]:
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
    w_str(40021, device_identifier, 16)
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
    LoggingDataBlock.silent(True)
    try:
        return _poll_inverter_impl(client, store, poll_count)
    finally:
        LoggingDataBlock.silent(False)


def _poll_inverter_impl(client: ModbusTcpClient, store: ModbusDeviceContext,
                        poll_count: list[int]):
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
    _latest_sma["power_w"] = float(power)
    _latest_sma["ts"] = time.monotonic()
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

    # Temperatures (Model 103 d[31..34], SF at d[35]). Not-implemented = 0x8000.
    tmp_sf = _sf(d[35])
    def _tmp(raw: int) -> float | None:
        return _safe_s16(raw) * 10**tmp_sf if raw != 0x8000 else None
    _last_temps["cab"]  = _tmp(d[31])
    _last_temps["snk"]  = _tmp(d[32])
    _last_temps["trns"] = _tmp(d[33])
    _last_temps["oth"]  = _tmp(d[34])

    state = _safe_u16(d[36])  # SunSpec operating state
    # Map to v1 convention if not-impl (nighttime returns 0xFFFF -> 0)
    if state == 0:
        state = 3 if power == 0 else 4  # Standby or MPPT

    # Push operating state to HA on change + 5-min heartbeat
    _maybe_push_op_state(state)

    # --- Parse Model 160 per-MPPT data ---
    # Header: m[0]=model_id, m[1]=length
    # Fixed block: m[2]=DCA_SF, m[3]=DCV_SF, m[4]=DCW_SF, m[5]=DCWH_SF
    # m[6-7]=Evt, m[8]=N, m[9]=TmsPer
    # Repeating blocks start at m[10], 20 regs each
    # Block layout: ID(1) + IDStr(8 regs) + DCA(1) + DCV(1) + DCW(1) + DCWH(2) +
    #               Tmp(1) + DCSt(1) + DCEvt(2) + pad(2) = 20 regs
    m160_dcv_sf = _sf(m[3])
    m160_dcw_sf = _sf(m[4])
    n_modules = min(m[8], 3)

    mppt = []
    for i in range(n_modules):
        base = 10 + i * 20
        mppt.append({
            "i": _safe_u16(m[base + 9]) * 0.1,  # DCA in 0.1A (DCA_SF not-impl, empirically SF=-1)
            "v": _safe_u16(m[base + 10]) * 10**m160_dcv_sf,
            "w": _safe_s16(m[base + 11]) * 10**m160_dcw_sf,
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

    # --- Periodic logging (DEBUG: per-minute AC/DC summaries) ---
    poll_count[0] += 1
    if poll_count[0] % 60 == 1:
        log.debug(
            "AC: P=%dW VA=%dVA PF=%.2f V=%.0f/%.0f/%.0fV I=%.1f/%.1f/%.1fA Hz=%.2f",
            int(power), int(va), pf_raw, v_l1, v_l2, v_l3, i_l1, i_l2, i_l3, freq,
        )
        dc_w_total = sum(m["w"] for m in mppt)
        log.debug(
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


TEMP_LOG_INTERVAL_S = 300   # log inverter temperatures every 5 min at DEBUG


def inverter_poll_loop(inverter_ip: str, store: ModbusDeviceContext):
    """Continuously poll the inverter and update the register map."""
    poll_count = [0]
    client = None
    error_backoff = POLL_ERROR_MIN
    prev_interval = None
    was_throttled = False
    throttle_start = 0
    last_temp_log_t = 0.0

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

        # Periodic temperature log (DEBUG every 5 min — silently skips fields
        # the inverter doesn't implement).
        now_t = time.monotonic()
        if now_t - last_temp_log_t >= TEMP_LOG_INTERVAL_S:
            last_temp_log_t = now_t
            parts = [f"{k}={v:.1f}°C" for k, v in _last_temps.items() if v is not None]
            if parts:
                log.debug("Temperatures: %s", " ".join(parts))

        # Throttle detection (DEBUG only — expected consequence of our own
        # curtailment writes, so state=5 is tautological with Curtailment STARTED.
        # A genuine anomaly detector for state=5 *without* a recent curtailment
        # command could be added later if needed.)
        if state == 5 and not was_throttled:
            log.debug("Inverter throttled (state=5)")
            was_throttled = True
            throttle_start = time.monotonic()
        elif state != 5 and was_throttled:
            duration = time.monotonic() - throttle_start
            log.debug("Inverter no longer throttled (state=%d) — was throttled for %ds", state, int(duration))
            was_throttled = False

        # Adaptive poll interval: 1s when producing, 60s on standby/night
        if state == 4 or state == 5:  # MPPT or Throttled
            interval = POLL_ACTIVE
        else:
            interval = POLL_STANDBY

        if interval != prev_interval:
            log.debug("Poll interval: %ds (state=%d)", interval, state)
            prev_interval = interval

        time.sleep(interval)


# ---------------------------------------------------------------------------
# Modbus server tracking
# ---------------------------------------------------------------------------


class _ModbusTracker:
    """Tracks Modbus client connections, reads, and external writes.

    Connect/disconnect events are aggregated: the first event in a calm period
    is logged normally; any additional events arriving within BURST_WINDOW_S
    of each other are held back and summarized once activity quiets down.
    """

    BURST_WINDOW_S = 5.0
    WRITE_DRY_MIN = 10.0     # min. dry minutes before announcing a write-side resume

    def __init__(self):
        self._clients_seen: set[str] = set()
        self._read_count = 0
        self._write_count = 0
        self._last_read_report = 0
        self._last_write_report = 0
        self._write_dry_since = None  # monotonic ts since external writes went dry
        # Burst aggregator state
        self._burst_first_t = 0.0
        self._burst_last_t = 0.0
        self._burst_connects = 0
        self._burst_disconnects = 0
        self._burst_active = False

    def _flush_burst(self):
        held = self._burst_connects + self._burst_disconnects
        if held > 0:
            duration = self._burst_last_t - self._burst_first_t
            log.info(
                "Modbus client churn: +%d more events in %.1fs (%d connects, %d disconnects)",
                held, duration, self._burst_connects, self._burst_disconnects,
            )
        self._burst_connects = 0
        self._burst_disconnects = 0
        self._burst_active = False

    def on_connect(self, connected: bool) -> None:
        """Called by pymodbus trace_connect (True=connect, False=disconnect)."""
        now = time.monotonic()
        if self._burst_active and (now - self._burst_last_t) >= self.BURST_WINDOW_S:
            self._flush_burst()

        if not self._burst_active:
            log.info("Modbus client %s", "connected" if connected else "disconnected")
            self._burst_active = True
            self._burst_first_t = now
        else:
            if connected:
                self._burst_connects += 1
            else:
                self._burst_disconnects += 1
        self._burst_last_t = now

    def on_read(self):
        self._read_count += 1

    def on_write(self):
        self._write_count += 1

    def report(self):
        """Called periodically to report activity."""
        # Flush any stale connection burst so its summary doesn't linger silently
        if self._burst_active and (time.monotonic() - self._burst_last_t) >= self.BURST_WINDOW_S:
            self._flush_burst()
        rc = self._read_count
        wc = self._write_count
        rd = rc - self._last_read_report
        wd = wc - self._last_write_report
        self._last_read_report = rc
        self._last_write_report = wc
        # Edge-triggered "gridX write side resumed": announce once when external
        # writes reappear after a dry spell (the gridX control-dropout signature is
        # reads alive / writes at 0). Lets the user know it's safe to switch back
        # to Modbus pass-through mode.
        now = time.monotonic()
        if wd > 0:
            if self._write_dry_since is not None:
                dry_min = (now - self._write_dry_since) / 60.0
                if dry_min >= self.WRITE_DRY_MIN:
                    log.info("gridX write side resumed after ~%.0f min of no writes "
                             "(+%d) — EMS control is back; safe to switch curtailment to Modbus",
                             dry_min, wd)
                self._write_dry_since = None
        elif self._write_dry_since is None:
            self._write_dry_since = now
        if rc == 0 and wc == 0:
            log.info("Modbus: idle — no client traffic")
        else:
            log.info("Modbus: %d reads / %d writes (+%d / +%d)", rc, wc, rd, wd)


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
            log.debug("First Modbus read (addr=%d, count=%d) — client polling", address, count)
        return super().getValues(fc_as_hex, address, count)


# ---------------------------------------------------------------------------
# Inverter identity (one-time read at startup)
# ---------------------------------------------------------------------------


def _regs_to_str(regs: list[int]) -> str:
    """Convert a list of uint16 registers to an ASCII string."""
    b = bytes(byte for w in regs for byte in [w >> 8, w & 0xFF])
    return b.split(b"\x00", 1)[0].decode("ascii", errors="replace")


def read_inverter_identity(inverter_ip: str) -> tuple[int, int]:
    """Read serial number and max power from inverter via SunSpec.

    Serial: SunSpec Common model, register 40053 (0-based: 40052), 16-reg string.
    Max power (WRtg): SunSpec Model 120 (Nameplate), found by walking the model
    chain forward from Model 103.

    Returns (serial, max_power_w). Values are 0 on failure.
    """
    client = ModbusTcpClient(inverter_ip, port=502, timeout=5)
    if not client.connect():
        return 0, 0
    try:
        # Serial from SunSpec Common model
        serial = 0
        r = client.read_holding_registers(40052, count=16, device_id=INVERTER_UNIT_ID)
        if not r.isError():
            serial_str = _regs_to_str(r.registers)
            serial = int(serial_str) if serial_str.isdigit() else 0

        # WRtg from Model 120: walk forward from Model 103 header
        # MODEL_103_ADDR points to Model 103 data (past header), so header is 2 regs before
        max_power_w = 0
        hdr_addr = MODEL_103_ADDR - 2
        r_hdr = client.read_holding_registers(hdr_addr, count=2, device_id=INVERTER_UNIT_ID)
        if not r_hdr.isError() and r_hdr.registers[0] == 103:
            addr = hdr_addr + 2 + r_hdr.registers[1]
            for _ in range(10):
                r_next = client.read_holding_registers(addr, count=2, device_id=INVERTER_UNIT_ID)
                if r_next.isError():
                    break
                model_id, model_len = r_next.registers[0], r_next.registers[1]
                if model_id == 0xFFFF:
                    break
                if model_id == 120:
                    r120 = client.read_holding_registers(addr + 2, count=model_len, device_id=INVERTER_UNIT_ID)
                    if not r120.isError():
                        wrtg = r120.registers[1]
                        wrtg_sf = _sf(r120.registers[22])
                        max_power_w = int(wrtg * 10**wrtg_sf)
                    break
                addr += 2 + model_len

        return serial, max_power_w
    except Exception as e:
        log.warning("Identity read error: %s", e)
        return 0, 0
    finally:
        client.close()


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# Self-curtailment (best-effort §9 60% cap) — optional, mutually exclusive with
# forward_writes.
# ---------------------------------------------------------------------------
# Closes a control loop the proxy owns: read the *net grid power* from a Tasmota
# SML meter at ~1 Hz and write SunSpec Model 123 WMaxLimPct on the SMA to keep
# export at/below a threshold. Because we feed back on the metered net value,
# the (uncontrolled) second inverter + house load are just disturbances the PI
# rejects — no plant model, no SMA/VX3 split needed. Only the SMA is
# controllable; that suffices as long as the second inverter alone stays under
# the cap (it charges the battery first, so in practice always).


def read_grid_export_w(tasmota_url: str, timeout: float = 2.0) -> float:
    """Net grid power in W from Tasmota SML (negative = export). Raises on error."""
    r = requests.get(f"{tasmota_url}/cm?cmnd=Status%2010", timeout=timeout)
    r.raise_for_status()
    return float(r.json()["StatusSNS"]["SML"]["Power_curr"])


def _write_m123_limit(client, m123_data_wire: int, unit_id: int,
                      pct_raw: int, ena: int) -> bool:
    """Write WMaxLimPct (0.01%-units, 0..10000) + Ena to Model 123. True on success."""
    if m123_data_wire is None:
        return False
    pdu_pct = (m123_data_wire - 1) + M123_OFFSET["WMaxLimPct"]
    pdu_ena = (m123_data_wire - 1) + M123_OFFSET["WMaxLim_Ena"]
    try:
        if not client.connected and not client.connect():
            wlog.warning("[curtail] inverter connection lost — write skipped")
            return False
        rr = client.write_register(pdu_pct, int(pct_raw), device_id=unit_id)
        rr2 = client.write_register(pdu_ena, int(ena), device_id=unit_id)
        if rr.isError() or rr2.isError():
            wlog.warning("[curtail] M123 write error: pct=%s ena=%s", rr, rr2)
            return False
        return True
    except Exception as e:
        wlog.warning("[curtail] M123 write exception: %s — closing client", e)
        try:
            client.close()
        except Exception:
            pass
        return False


class _CurtailController:
    """Feedforward export-cap controller.

        cut_w      = (export - cap) + integral_trim
        target_sma = sma_now - cut_w
        WMaxLimPct = 100 * target_sma / rated

    Drives the limit from the SMA's measured output instead of winding a PI
    integral through the deadband. Feedforward gain is 1 (dropping the SMA by X
    drops export by X), so it binds in one step and tracks the SMA ramp; the
    integral only trims residual bias. Release is rate-limited (fast down, slow
    up). Stale SMA reading falls back to rated (conservative over-cut).
    """

    def __init__(self, threshold_w: float, sma_rated_w: float):
        self.L = float(threshold_w)
        self.rated = max(1000.0, float(sma_rated_w))
        self.ki_w = 0.02                       # trim: extra cut (W) per (W·s) of error
        self.leak = 0.85                       # leaky integrator: forget stale bias in ~4 steps
        self.i_clamp = 0.10 * self.rated       # trim bounded to ±10 % rated
        self.release_max = 6.0                 # %/s max release (slow "up")
        self.wmax = 100.0                      # current commanded WMaxLimPct
        self._i = 0.0                          # integral trim, Watts

    @property
    def c(self) -> float:
        """Curtailment amount in % (0 = free, 100 = off), for logging/Simulate."""
        return 100.0 - self.wmax

    def update(self, export_w: float, sma_now_w, dt: float):
        e = export_w - self.L                              # >0 = over cap
        # Leaky integral: forgets transient excursions (e.g. a negative dip from a
        # production crash) so stale trim can't fight the feedforward for long.
        self._i = min(self.i_clamp, max(-self.i_clamp, self.leak * self._i + self.ki_w * e * dt))
        cut_w = e + self._i                                # W to shed off the SMA
        # Free when not cutting below current output. Reset the trim (don't clamp
        # ≤0) so it can't wind negative and delay the next engagement.
        if cut_w <= 0.0 and self.wmax >= 100.0:
            self._i = 0.0
            self.wmax = 100.0
            return 100.0, 0
        sma = self.rated if sma_now_w is None else max(0.0, float(sma_now_w))
        target_sma = max(0.0, sma - cut_w)                 # desired SMA output (W)
        desired = min(100.0, 100.0 * target_sma / self.rated)
        if desired > self.wmax:                            # asymmetric: rate-limit release
            desired = min(desired, self.wmax + self.release_max * dt)
        self.wmax = min(100.0, max(0.0, desired))
        return self.wmax, (1 if self.wmax < 99.5 else 0)


def curtail_control_loop(client, m123_data_wire, unit_id, tasmota_url,
                         threshold_w, sma_rated_w, dry_run,
                         interval: float = 3.0, failsafe_after: int = 8):
    """Best-effort export cap (~3 s loop). Never raises, self-heals on any error.

    3 s matches the SMA's own ramp rate: a 1 s loop rewrites the limit before the
    inverter has reached the last one, which made the setpoint hunt ~2-3 %. At 3 s
    each write settles before the next correction, and the read timeout (2 s) sits
    comfortably under the tick.
    """
    pi = _CurtailController(threshold_w, sma_rated_w)
    fails = 0
    engaged = False
    last_hb = 0.0
    last_sim = None
    log.info("[curtail] loop start: threshold=%dW dry_run=%s tasmota=%s "
             "(feedforward, rated=%.0fW ki_w=%.3f release=%.0f%%/s)",
             threshold_w, dry_run, tasmota_url, pi.rated, pi.ki_w, pi.release_max)
    while True:
        try:
            export = -read_grid_export_w(tasmota_url)   # W, positive = export
            fails = 0
            # SMA output from the poll thread; stale (>10 s) → None → rated fallback.
            sma_now = _latest_sma["power_w"]
            if time.monotonic() - _latest_sma["ts"] > 10.0:
                sma_now = None
            pct, ena = pi.update(export, sma_now, interval)
            pct_raw = min(10000, max(0, int(round(pct * 100))))
            log.debug("[curtail] export=%.0fW err=%+.0f sma=%sW → WMaxLim=%.1f%% (i=%.0fW dt=%.1fs)",
                      export, export - threshold_w,
                      "stale" if sma_now is None else f"{sma_now:.0f}", pct, pi._i, interval)
            if dry_run:
                # Simulate: log + expose to a *separate* HA entity for graphing.
                # Never writes the SMA and never touches the real curtailment
                # entities, so it can't imply a curtailment that isn't happening.
                t = time.monotonic()
                now_eng = pi.c > 0.1
                if now_eng != engaged:
                    engaged = now_eng
                    log.info("[curtail] %s (DRY) — export=%.0fW cap=%d → WMaxLimPct=%.1f%%",
                             "ENGAGE" if engaged else "RELEASE", export, threshold_w, pct)
                if engaged and t - last_hb >= 60:
                    last_hb = t
                    log.info("[curtail] DRY export=%.0fW → WMaxLimPct=%.1f%% (c=%.1f%%)",
                             export, pct, pi.c)
                # Push to sma_curtail_sim on ≥1 % change or every 30 s (graphable,
                # low HA load).
                rp = round(pct)
                if last_sim is None or abs(rp - last_sim[0]) >= 1 or t - last_sim[1] >= 30:
                    last_sim = (rp, t)
                    _push_ha_state("sensor.sma_curtail_sim", rp, {
                        "unit_of_measurement": "%",
                        "friendly_name": "SMA Curtail (Simulate)",
                        "icon": "mdi:transmission-tower-off",
                        "export_w": round(export), "curtail_pct": round(pi.c, 1),
                        "engaged": engaged, "threshold_w": threshold_w})
            else:
                # Self-Adaptive live: write the SMA and reflect the curtailment
                # value in HA via the tracker (push + STARTED/RELEASED events +
                # its 60 s heartbeat, driven by the existing _heartbeat_loop).
                _write_m123_limit(client, m123_data_wire, unit_id, pct_raw, ena)
                _curtailment_tracker.record(pct_raw, ena)
        except Exception as ex:
            fails += 1
            if fails <= 3 or fails == failsafe_after:
                log.warning("[curtail] read/control error (%d): %s", fails, ex)
            if fails == failsafe_after:
                # Measurement lost → release control, don't cut the SMA. A brief
                # over-cap on a meter outage beats killing all PV, and it's the
                # state the plant already runs in uncontrolled.
                log.warning("[curtail] FAIL-SAFE: measurement lost → release SMA%s",
                            " (DRY)" if dry_run else "")
                pi.c = 0.0
                pi._i = 0.0
                if not dry_run:
                    _write_m123_limit(client, m123_data_wire, unit_id, 10000, 0)
                    _curtailment_tracker.record(10000, 0)
        time.sleep(interval)


def main():
    parser = argparse.ArgumentParser(description="SMA Modbus TCP Proxy v2")
    parser.add_argument("--inverter-ip", default=None)
    parser.add_argument("--port", type=int, default=502)
    parser.add_argument("--options", default=None)
    args = parser.parse_args()

    device_id = os.environ.get("DEVICE_IDENTIFIER", "STP 10.0-3AV-40")
    inverter_ip = os.environ.get("INVERTER_IP", args.inverter_ip)
    forward_writes = os.environ.get("FORWARD_WRITES", "false").lower() in ("1", "true", "yes")
    curtailment = os.environ.get("CURTAILMENT", "").strip()
    curtail_threshold_w = int(os.environ.get("CURTAIL_THRESHOLD_W", "14256"))
    tasmota_url = os.environ.get("TASMOTA_URL", "http://192.168.1.61")
    log_level = os.environ.get("LOG_LEVEL", "info").lower()
    ha_push = os.environ.get("HA_PUSH", "true").lower() in ("1", "true", "yes")

    if args.options and Path(args.options).exists():
        opts = json.loads(Path(args.options).read_text())
        inverter_ip = opts.get("inverter_ip", inverter_ip)
        device_id = opts.get("device_identifier", device_id)
        forward_writes = opts.get("forward_writes", forward_writes)
        curtailment = str(opts.get("curtailment", curtailment))
        curtail_threshold_w = int(opts.get("curtail_threshold_w", curtail_threshold_w))
        tasmota_url = opts.get("tasmota_url", tasmota_url)
        log_level = str(opts.get("log_level", log_level)).lower()
        ha_push = opts.get("ha_push", ha_push)

    global _ha_push_enabled
    _ha_push_enabled = bool(ha_push) and bool(HA_TOKEN)

    # Apply log level (debug shows per-minute AC/DC + poll-interval changes;
    # info/warning keeps just startup, writes, errors)
    level = {"debug": logging.DEBUG, "info": logging.INFO,
             "warning": logging.WARNING}.get(log_level, logging.INFO)
    logging.getLogger("sma_proxy").setLevel(level)
    logging.getLogger("sma_proxy.writes").setLevel(level)

    # In debug mode, install per-connection tracing (peer IP + disconnect
    # reason on our own logger) on top of the normal sma_proxy debug output
    # (per-minute AC/DC summaries, poll-interval, temperatures). That is the
    # useful signal for diagnosing client reconnect storms.
    #
    # pymodbus's own logger is deliberately left at its normal (muted) level:
    # at DEBUG it dumps every decoded PDU, and the proxy's own 1 Hz inverter
    # poll alone floods the log (~2 PDUs/s) and buries the connection events.
    # The tracing wrapper already gives peer + disconnect reason, which is all
    # we need. (For a rare raw-packet dive, set the "pymodbus.logging" logger to
    # DEBUG here — one line — but expect a firehose.)
    if level == logging.DEBUG:
        _install_connection_tracing()

    if not inverter_ip:
        log.error("No inverter_ip configured. Set it in the add-on config or INVERTER_IP env var.")
        return

    log.info("SMA Modbus Proxy v%s  (log_level=%s, ha_push=%s)",
             VERSION, log_level,
             "on" if _ha_push_enabled else ("off" if not ha_push else "no SUPERVISOR_TOKEN"))

    # Seed HA entities at startup so they exist before the first curtailment.
    _push_curtailment(100, False)

    # Auto-detect serial and max power from inverter
    log.info("Reading identity from inverter at %s...", inverter_ip)
    serial, max_power_w = read_inverter_identity(inverter_ip)
    if serial:
        log.info("Auto-detected serial: %d", serial)
    else:
        log.warning("Could not read serial from inverter, using default 1234567890")
        serial = 1234567890
    if max_power_w:
        log.info("Auto-detected max power: %dW", max_power_w)
    else:
        log.warning("Could not read max power from inverter, using default 12000W")
        max_power_w = 12000

    log.info("Inverter: %s, Serial: %d, Max power: %dW, Device ID: %s",
             inverter_ip, serial, max_power_w, device_id)

    regs = build_register_map(serial, device_id)
    block = LoggingDataBlock(0, [0] * 40001 + regs + [0] * 25000)
    store = _TrackingDeviceContext(hr=block, ir=block)

    # Static SMA identification registers (silent: not from external client)
    LoggingDataBlock.silent(True)
    try:
        store.setValues(3, 30003, [0, 378])
        store.setValues(3, 30005, [(serial >> 16) & 0xFFFF, serial & 0xFFFF])
        store.setValues(3, 30051, [0, 8001])
        store.setValues(3, 30053, [0, 9348])
        store.setValues(3, 30057, [(serial >> 16) & 0xFFFF, serial & 0xFFFF])
        store.setValues(3, 30059, [0x0400, 0x0002])
        store.setValues(3, 30201, [0, 307])
        store.setValues(3, 30231, _u32_words(max_power_w))
        store.setValues(3, 30233, _u32_words(max_power_w))
    finally:
        LoggingDataBlock.silent(False)

    context = ModbusServerContext(
        devices={0: store, 1: store, 2: store, 3: store, 247: store},
        single=False,
    )

    # Discovery: walk SunSpec model chain on the real inverter to find Model 123
    # (the writable control model). Required for translating legacy gridX writes.
    m123_data_wire = None
    discovered = discover_sunspec_models(inverter_ip, INVERTER_UNIT_ID)
    if 123 in discovered:
        m123_data_wire = discovered[123]
        log.info("✓ SunSpec Model 123 (Immediate Controls) discovered @ wire %d",
                 m123_data_wire)
        log.info("  → WMaxLimPct  @ wire %d", m123_data_wire + M123_OFFSET["WMaxLimPct"])
        log.info("  → WMaxLim_Ena @ wire %d", m123_data_wire + M123_OFFSET["WMaxLim_Ena"])
    else:
        log.warning("⚠ Model 123 not found on inverter — write translation disabled")

    # Curtailment mode: Off | Modbus | Self-Adaptive | Simulate (mutually
    # exclusive by construction). Legacy fallback: if `curtailment` is unset,
    # derive from the old forward_writes bool (true→Modbus, false→Off).
    mode = curtailment.lower().replace("_", "-").replace(" ", "-")
    if not mode:
        mode = "modbus" if forward_writes else "off"
    if mode not in ("off", "modbus", "self-adaptive", "simulate"):
        log.warning("Unknown curtailment mode %r — falling back to 'off'", curtailment)
        mode = "off"
    if m123_data_wire is None and mode != "off":
        log.warning("curtailment=%s but Model 123 not discovered — disabled", mode)
        mode = "off"

    global _curtail_mode
    _curtail_mode = mode

    if mode == "modbus":
        # Pass-through: translate gridX legacy writes (40024 …) → Model 123.
        log.info("Curtailment=Modbus — gridX writes translated & forwarded to Model 123")
        fwd_client = ModbusTcpClient(inverter_ip, port=502, timeout=5)
        if fwd_client.connect():
            LoggingDataBlock.enable_forwarding(fwd_client, INVERTER_UNIT_ID, m123_data_wire)
            log.info("Forwarding client connected (unit=%d, M123 base wire=%d)",
                     INVERTER_UNIT_ID, m123_data_wire)
        else:
            log.error("Forwarding client could not connect — write-through disabled")
    elif mode in ("self-adaptive", "simulate"):
        dry = (mode == "simulate")
        if not tasmota_url:
            log.error("curtailment=%s needs tasmota_url — disabled", mode)
        else:
            log.info("Curtailment=%s ENABLED — threshold=%dW, tasmota=%s%s",
                     mode, curtail_threshold_w, tasmota_url,
                     " (SIMULATE: computes+logs, no SMA writes/HA push)" if dry else "")
            curtail_client = None
            if not dry:
                curtail_client = ModbusTcpClient(inverter_ip, port=502, timeout=5)
                curtail_client.connect()
            threading.Thread(
                target=curtail_control_loop,
                args=(curtail_client, m123_data_wire, INVERTER_UNIT_ID, tasmota_url,
                      curtail_threshold_w, max_power_w, dry),
                daemon=True,
            ).start()
    else:
        log.info("Curtailment=Off — proxy serves reads only, no writes")

    # Start inverter poll thread
    threading.Thread(
        target=inverter_poll_loop, args=(inverter_ip, store), daemon=True
    ).start()

    # Start curtailment heartbeat thread (one INFO summary line per minute)
    threading.Thread(target=_heartbeat_loop, daemon=True).start()

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
