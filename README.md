# SMA Modbus Proxy — Home Assistant Add-on

A Modbus TCP proxy that reads SunSpec registers directly from an SMA inverter and serves them at standard addresses for energy management systems.

## Why?

Some energy management systems only discover inverters that speak **SunSpec Model 103** at the standard register position. Newer SMA inverters like the **Sunny Tripower X** (ennexOS platform) serve Model 103 deep in a long SunSpec model chain (address 41257, unit ID 126) where these systems can't find it.

Known affected systems:
- **Viessmann Gridbox**
- **E.ON Home Manager** (successor, same hardware)
- **gridX** energy management (powers both of the above)

This add-on bridges the gap: it polls your inverter directly via Modbus TCP every second and serves the data at the standard SunSpec addresses (40071+, unit ID 1/3).

## Features

- **Direct Modbus polling** — reads from the inverter every 1s (~18ms per cycle)
- Full **SunSpec Model 103** (Three Phase Inverter) at standard addresses
- **SMA proprietary registers** (30xxx/35xxx) for maximum compatibility
- **3 MPPT strings** from SunSpec Model 160
- **Adaptive polling** — 1s when producing, 60s on standby/night
- **Exponential backoff** on connection errors (5s to 5min)
- Throttle detection (SunSpec state 5) with duration logging
- Responds on Modbus unit IDs 0, 1, 2, 3, and 247
- No HA API dependency — communicates directly with the inverter

## Installation

### Home Assistant Add-on

1. Add this repository to your Home Assistant add-on store:
   - Go to **Settings → Add-ons → Add-on Store → ⋮ → Repositories**
   - Add: `https://github.com/lackas/ha-sma-modbus-proxy`

2. Install "SMA Modbus Proxy" from the store

3. Configure the add-on with your inverter's IP address

4. Start the add-on

### Standalone Docker

```bash
docker compose up -d
```

Edit `docker-compose.yml` to set your inverter IP:

```yaml
environment:
  - INVERTER_IP=192.168.1.216
  - SERIAL=1234567890
  - MAX_POWER_W=12000
```

## Configuration

| Option | Default | Description |
|--------|---------|-------------|
| `inverter_ip` | `""` | IP address of the SMA inverter (required) |
| `serial` | `1234567890` | Emulated inverter serial number |
| `max_power_w` | `12000` | Nominal max power (W) for SMA registers 30231/30233 |

## How It Works

```
┌──────────┐  Modbus TCP   ┌──────────────┐  Modbus TCP   ┌─────────┐
│ SMA STP X├◄── poll 1s ───┤  SMA Modbus  ├◄── poll ~1s ──┤ Gridbox │
│ Inverter │  unit 126     │    Proxy     │  unit 1/3     │  (EMS)  │
│ :502     │  addr 41257+  │  (add-on)   │  addr 40071+  │         │
└──────────┘               │  :502       │               └─────────┘
                           └──────────────┘
```

1. The proxy connects to the inverter via Modbus TCP and polls SunSpec Model 103 (50 regs) + Model 160 (70 regs) every second
2. It parses the values, applies scale factors, replaces nighttime "not implemented" markers with zeros
3. It writes them to a standard SunSpec register map at the addresses the Gridbox expects
4. It also computes SMA proprietary registers (30xxx/35xxx) from the SunSpec data
5. The energy management system reads these registers via standard Modbus TCP

## SunSpec Register Map

The proxy serves the following:

**SunSpec models:**
- **Model 1** (Common): Manufacturer, model, serial, firmware version (40003–40070)
- **Model 103** (Three Phase Inverter): AC/DC measurements, operating state (40071–40122)

**SMA proprietary registers:**
- **30003–30233**: Device identification, serial, device class, max power
- **30531**: Total yield
- **30769–30797**: AC power, voltages, frequency, reactive power
- **30803–30815**: DC per-string measurements (2 strings)
- **30835**: Operating status
- **35377–35393**: MPPT tracker data (3 strings)

## Log Output

```
SMA Modbus Proxy v2.0.3
Inverter: 192.168.1.216, Serial: 1234567890, Max power: 12000W
Connected to inverter at 192.168.1.216
Poll interval: 1s (state=4)
AC: P=2350W VA=2450VA PF=-1.00 V=227/227/228V I=3.6/3.6/3.6A Hz=50.04
DC: A=1830W(3.9A/471V) B=590W(1.5A/387V) C=0W(0.0A/0V) Tot=2420W Yield=205600Wh St=4(307)
Modbus client connected
First Modbus read (addr=30053, count=2) — client polling
Modbus: 255 reads total (255 since last report)
```

## Verified With

- **Inverter**: SMA Sunny Tripower X 12 (STP 12-50), firmware 03.14.22.R
- **EMS**: Viessmann Gridbox (gridX platform)
- **SunSpec**: Model 103 at address 41257, Model 160 at 41415, unit ID 126

## Limitations

- Designed for SMA Sunny Tripower X (ennexOS) — other SMA inverters may use different register addresses or unit IDs
- Emulates STP 10.0-3AV-40 identity (required for Gridbox discovery)
- Temperature registers are not populated (SunSpec "not implemented")
- No write support — read-only proxy

## License

MIT
