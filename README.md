# dirigera-exporter

A small Prometheus exporter for the **IKEA DIRIGERA** smart-home hub. It polls
the hub's local HTTPS API on an interval and exposes the readings of every
environment sensor (e.g. **VINDSTYRKA** / **ALPSTUGA**) and the state of every
**STARKVIND** air purifier as Prometheus metrics.

- Pure local polling — no IKEA cloud, no internet round-trip.
- Configuration via environment variables only; the API token is never baked
  into the image.
- Survives hub/network errors: a failed poll is reported via
  `ikea_dirigera_scrape_success` instead of crashing the process.

## Metrics

| Metric | Labels | Description |
| --- | --- | --- |
| `ikea_air_temperature_celsius` | `sensor`, `room` | Temperature in °C (`TEMP_OFFSET_CELSIUS` applied) |
| `ikea_air_humidity_percent` | `sensor`, `room` | Relative humidity in % |
| `ikea_air_co2_ppm` | `sensor`, `room` | CO₂ in ppm |
| `ikea_air_pm25_micrograms_per_cubic_meter` | `sensor`, `room` | PM2.5 in µg/m³ (environment sensors) |
| `ikea_air_purifier_running` | `sensor`, `room` | `1` if the STARKVIND motor is running, else `0` |
| `ikea_air_purifier_motor_speed` | `sensor`, `room` | Motor speed (0–50; `0` = off) |
| `ikea_air_purifier_fan_mode` | `sensor`, `room`, `mode` | `1` for the active fan mode (`off`/`on`/`low`/`medium`/`high`/`auto`), else `0` |
| `ikea_air_purifier_filter_alarm` | `sensor`, `room` | `1` if the filter needs attention/replacement |
| `ikea_air_purifier_pm25` | `sensor`, `room` | Air purifier's raw PM2.5 reading — **not** comparable to the µg/m³ metric (the STARKVIND samples only with airflow, so it can be stale/coarse) |
| `ikea_dirigera_scrape_success` | — | `1` if the most recent hub poll succeeded, else `0` |
| `ikea_dirigera_last_success_timestamp_seconds` | — | Unix time of the most recent successful poll |

Only fields a device actually reports are emitted, so a sensor without a CO₂ or
PM2.5 channel does not publish a misleading `0`.

## Configuration

| Variable | Default | Description |
| --- | --- | --- |
| `DIRIGERA_IP` | _(required)_ | Hub IP address |
| `DIRIGERA_TOKEN` | _(required)_ | Hub API token (see below) |
| `POLL_INTERVAL_SECONDS` | `30` | Seconds between polls |
| `TEMP_OFFSET_CELSIUS` | `0.0` | Offset added to the **temperature** gauge only. The ALPSTUGA tends to read a couple of degrees warm; set to e.g. `-2` to compensate. |
| `LISTEN_PORT` | `9119` | Port the `/metrics` endpoint listens on |

## Getting a hub token

Token generation requires a physical button press on the hub and is a one-time
step. Using the bundled `dirigera` CLI:

```bash
pip install dirigera
generate-token <HUB_IP>
# When prompted, press the action button on the underside of the hub, then
# press ENTER. The token is printed once — store it somewhere safe.
```

## Run with Docker

```bash
docker run --rm -p 9119:9119 \
  -e DIRIGERA_IP=192.168.1.62 \
  -e DIRIGERA_TOKEN="$DIRIGERA_TOKEN" \
  ghcr.io/yornik/dirigera-exporter:v0.2.0

curl -s localhost:9119/metrics | grep ikea_
```

## Kubernetes

The exporter runs in my homelab as a Deployment + Service + ServiceMonitor
(kube-prometheus-stack) with the token sourced from a SOPS-encrypted Secret, and
a companion Grafana dashboard. The manifests live in the GitOps repo, not here —
this repo only builds the image. A matching dashboard is published at
[`dirigera-grafana-dashboard`](https://github.com/Yornik/dirigera-grafana-dashboard).

## Development

```bash
python -m venv .venv && . .venv/bin/activate
pip install -r requirements-dev.txt
ruff check . && ruff format --check . && pytest
```

Tests run entirely against mocked hub/device objects — no live hub required.

## Disclaimer & trademarks

This is an independent, unofficial project. It is **not** affiliated with,
authorized, sponsored, or endorsed by Inter IKEA Systems B.V. or any IKEA
entity.

**IKEA®**, **DIRIGERA**, **STARKVIND**, **VINDSTYRKA** and **ALPSTUGA** are
trademarks of Inter IKEA Systems B.V. They are used here solely for
identification and descriptive purposes (nominative fair use) to indicate
compatibility; their use does not imply any affiliation with or endorsement by
the trademark owner.

This project talks to the hub's own local API via the third-party
[`dirigera`](https://github.com/Leggin/dirigera) library and does not include
any IKEA software.

## License

[MIT](LICENSE)
