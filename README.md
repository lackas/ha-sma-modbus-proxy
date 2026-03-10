# SMA Modbus Proxy — Home Assistant Add-on

A Home Assistant add-on that emulates an SMA SunSpec-compatible inverter via Modbus TCP, using live sensor data from Home Assistant.

## Why?

Some energy management systems (e.g., Viessmann Gridbox) only discover inverters that speak **SunSpec Model 103** over Modbus TCP. Newer SMA inverters like the **Sunny Tripower X** only support **SunSpec 700** (DER models), which these systems don't understand.

This add-on bridges the gap: it reads your inverter's data from Home Assistant sensors (via WebSocket) and serves it as a classic SunSpec Model 103 device on Modbus TCP port 502.

## Features

- Full **SunSpec Model 103** (Three Phase Inverter) register map
- **SMA proprietary registers** (30xxx/35xxx) for maximum compatibility
- Live data via Home Assistant **WebSocket API** (sub-second updates)
- Configurable sensor entity mapping — works with any inverter that has HA sensors
- Supports dual MPPT strings (DC side)
- Responds on Modbus unit IDs 0, 1, 2, 3, and 247 (SMA broadcast)
- Supports both FC3 (holding registers) and FC4 (input registers)

## Prerequisites

You need your SMA inverter's data available as Home Assistant sensors. For newer SMA inverters (Sunny Tripower X, Sunny Boy Smart Energy, etc.) using the ennexOS platform, the [SMA ennexOS](https://github.com/shadow578/homeassistant_sma-ennexos) HACS integration works well.

Any other integration that provides AC power, voltage, current, frequency, and DC string data as HA sensors will also work.

## Installation

1. Add this repository to your Home Assistant add-on store:
   - Go to **Settings → Add-ons → Add-on Store → ⋮ → Repositories**
   - Add: `https://github.com/lackas/ha-sma-modbus-proxy`

2. Install "SMA Modbus Proxy" from the store

3. Configure the add-on (see below)

4. Start the add-on

## Configuration

### Required: Sensor Entity IDs

Map your inverter's Home Assistant sensor entities to the proxy's data fields. All `sensor_*` options accept a Home Assistant entity ID.

**AC side:**

| Option | Description | Unit |
|--------|-------------|------|
| `sensor_power` | Total AC active power | W |
| `sensor_current_l1/l2/l3` | AC current per phase | A |
| `sensor_voltage_l1/l2/l3` | AC voltage per phase | V |
| `sensor_frequency` | Grid frequency | Hz |
| `sensor_reactive` | Reactive power | var |
| `sensor_yield_total` | Total energy yield | Wh |
| `sensor_health` | Inverter health/status | — |

**DC side (per MPPT string):**

| Option | Description | Unit |
|--------|-------------|------|
| `sensor_dc_i_a` / `sensor_dc_i_b` | DC current string A/B | A |
| `sensor_dc_v_a` / `sensor_dc_v_b` | DC voltage string A/B | V |
| `sensor_dc_w_a` / `sensor_dc_w_b` | DC power string A/B | W |

### Other Options

| Option | Default | Description |
|--------|---------|-------------|
| `port` | `502` | Modbus TCP port |
| `serial` | `1234567890` | Emulated inverter serial number |
| `max_power_w` | `12000` | Nominal max power (W) |
| `ha_token` | `""` | HA long-lived access token (leave empty when running as add-on with `homeassistant_api: true`) |

### Example Configuration

```yaml
port: 502
serial: 1234567890
max_power_w: 12000
ha_token: ""
sensor_power: "sensor.sma_stp_x_total_power"
sensor_current_l1: "sensor.sma_stp_x_current_l1"
sensor_current_l2: "sensor.sma_stp_x_current_l2"
sensor_current_l3: "sensor.sma_stp_x_current_l3"
sensor_voltage_l1: "sensor.sma_stp_x_voltage_l1"
sensor_voltage_l2: "sensor.sma_stp_x_voltage_l2"
sensor_voltage_l3: "sensor.sma_stp_x_voltage_l3"
sensor_frequency: "sensor.sma_stp_x_frequency"
sensor_reactive: "sensor.sma_stp_x_reactive_power"
sensor_yield_total: "sensor.sma_stp_x_total_yield"
sensor_health: "sensor.sma_stp_x_health"
sensor_dc_i_a: "sensor.sma_stp_x_dc_current_a"
sensor_dc_v_a: "sensor.sma_stp_x_dc_voltage_a"
sensor_dc_w_a: "sensor.sma_stp_x_dc_power_a"
sensor_dc_i_b: "sensor.sma_stp_x_dc_current_b"
sensor_dc_v_b: "sensor.sma_stp_x_dc_voltage_b"
sensor_dc_w_b: "sensor.sma_stp_x_dc_power_b"
```

## How It Works

```
┌──────────┐  Speedwire  ┌────────────────┐  WebSocket  ┌──────────────┐  Modbus TCP  ┌─────────┐
│ SMA STP X├────────────►│ Home Assistant  ├────────────►│  SMA Modbus  ├─────────────►│ Gridbox │
│ Inverter │             │   (sensors)    │             │    Proxy     │  :502        │  (EMS)  │
└──────────┘             └────────────────┘             └──────────────┘              └─────────┘
```

1. Your inverter pushes data to Home Assistant (via Speedwire, Modbus, or any other integration)
2. The proxy subscribes to sensor state changes via the HA WebSocket API
3. On each update, it writes the values into a SunSpec-compatible Modbus register map
4. The energy management system reads these registers via standard Modbus TCP

## SunSpec Register Map

The proxy implements the following SunSpec models:

- **Model 1** (Common): Manufacturer, model, serial, firmware version (registers 40003–40070)
- **Model 103** (Three Phase Inverter): AC/DC measurements, operating state (registers 40071–40122)

Additionally, SMA proprietary registers are populated:

- **30003–30233**: Device identification, serial, device class, max power
- **30531**: Total yield
- **30769–30797**: AC power, voltages, frequency, reactive power
- **30803–30815**: DC per-string measurements
- **30835**: Operating status
- **35377–35387**: MPPT tracker data

## Requirements

- Home Assistant with the inverter's sensors available
- The add-on needs `host_network: true` to be reachable on port 502
- Python packages: `pymodbus`, `websockets` (installed automatically in the Docker image)

## Limitations

- Emulates a single three-phase inverter (STP 10.0-3AV-40 identity)
- Temperature registers are not populated (reported as "not implemented" per SunSpec)
- No write support — this is a read-only proxy

## License

MIT
