#!/usr/bin/env python3
"""Prometheus exporter for IKEA DIRIGERA air-quality devices.

Polls a DIRIGERA hub's local HTTPS API on a fixed interval and exposes:

* every environment sensor (e.g. ALPSTUGA / VINDSTYRKA): temperature, humidity,
  CO2 and PM2.5;
* every STARKVIND air purifier: on/off state, fan mode, motor speed, filter
  alarm and its built-in PM2.5 reading.

Configuration is via environment variables only; no secrets are baked into the
image. The exporter is designed to never crash on hub/network errors: a failed
poll sets ``ikea_dirigera_scrape_success`` to 0, logs the error, and keeps
serving the last-known values.
"""

from __future__ import annotations

import logging
import os
import sys
import time
import warnings

import urllib3
from dirigera import Hub
from dirigera.devices.air_purifier import AirPurifier, FanModeEnum
from dirigera.devices.environment_sensor import EnvironmentSensor
from prometheus_client import Gauge, start_http_server

LOG = logging.getLogger("dirigera-exporter")

# Per-device labels. ``sensor`` is the device's custom name, ``room`` is the
# room it is assigned to in the IKEA Home smart app.
_LABELS = ("sensor", "room")

# --- environment-sensor air-quality gauges ---
TEMPERATURE = Gauge(
    "ikea_air_temperature_celsius",
    "Air temperature in degrees Celsius (TEMP_OFFSET_CELSIUS applied).",
    _LABELS,
)
HUMIDITY = Gauge(
    "ikea_air_humidity_percent",
    "Relative humidity in percent.",
    _LABELS,
)
CO2 = Gauge(
    "ikea_air_co2_ppm",
    "CO2 concentration in parts per million.",
    _LABELS,
)
PM25 = Gauge(
    "ikea_air_pm25_micrograms_per_cubic_meter",
    "PM2.5 concentration in micrograms per cubic meter (environment sensors).",
    _LABELS,
)

# --- STARKVIND air-purifier gauges ---
PURIFIER_RUNNING = Gauge(
    "ikea_air_purifier_running",
    "1 if the air purifier motor is running, 0 if it is off.",
    _LABELS,
)
PURIFIER_MOTOR_SPEED = Gauge(
    "ikea_air_purifier_motor_speed",
    "Air purifier motor speed (0-50; 0 means off).",
    _LABELS,
)
PURIFIER_FAN_MODE = Gauge(
    "ikea_air_purifier_fan_mode",
    "Active air purifier fan mode (value is 1 for the current mode, else 0).",
    ("sensor", "room", "mode"),
)
PURIFIER_FILTER_ALARM = Gauge(
    "ikea_air_purifier_filter_alarm",
    "1 if the air purifier filter needs attention/replacement, 0 otherwise.",
    _LABELS,
)
PURIFIER_PM25 = Gauge(
    "ikea_air_purifier_pm25",
    "Raw PM2.5 reading from the air purifier's built-in sensor. NOT directly "
    "comparable to ikea_air_pm25_micrograms_per_cubic_meter: the STARKVIND only "
    "samples when air flows through it, so the value can be stale or coarse.",
    _LABELS,
)

# --- global scrape-health gauges (no labels) ---
SCRAPE_SUCCESS = Gauge(
    "ikea_dirigera_scrape_success",
    "1 if the most recent poll of the DIRIGERA hub succeeded, 0 otherwise.",
)
LAST_SUCCESS = Gauge(
    "ikea_dirigera_last_success_timestamp_seconds",
    "Unix timestamp of the most recent successful poll of the DIRIGERA hub.",
)


class Config:
    """Runtime configuration, sourced entirely from environment variables."""

    def __init__(self) -> None:
        self.ip = os.environ.get("DIRIGERA_IP", "").strip()
        self.token = os.environ.get("DIRIGERA_TOKEN", "").strip()
        self.poll_interval = int(os.environ.get("POLL_INTERVAL_SECONDS", "30"))
        self.temp_offset = float(os.environ.get("TEMP_OFFSET_CELSIUS", "0.0"))
        self.listen_port = int(os.environ.get("LISTEN_PORT", "9119"))

    def validate(self) -> None:
        missing = [
            name
            for name, value in (("DIRIGERA_IP", self.ip), ("DIRIGERA_TOKEN", self.token))
            if not value
        ]
        if missing:
            raise SystemExit(f"missing required environment variable(s): {', '.join(missing)}")


def device_name(device) -> str:
    """Human-readable device name, falling back to the device id."""
    return getattr(device.attributes, "custom_name", None) or device.id


def room_name(device) -> str:
    """Room name, or ``unknown`` for devices not assigned to a room."""
    room = getattr(device, "room", None)
    name = getattr(room, "name", None) if room is not None else None
    return name or "unknown"


def update_sensor_metrics(sensor: EnvironmentSensor, temp_offset: float) -> None:
    """Map one environment sensor's readings onto the gauges.

    Only fields the sensor actually reports are emitted, so a sensor that lacks
    a CO2 or PM2.5 channel does not publish a misleading ``0``.
    """
    attrs = sensor.attributes
    labels = {"sensor": device_name(sensor), "room": room_name(sensor)}

    if attrs.current_temperature is not None:
        TEMPERATURE.labels(**labels).set(attrs.current_temperature + temp_offset)
    if attrs.current_r_h is not None:
        HUMIDITY.labels(**labels).set(attrs.current_r_h)
    if attrs.current_c_o2 is not None:
        CO2.labels(**labels).set(attrs.current_c_o2)
    if attrs.current_p_m25 is not None:
        PM25.labels(**labels).set(attrs.current_p_m25)


def update_air_purifier_metrics(purifier: AirPurifier) -> None:
    """Map one STARKVIND air purifier's state onto the gauges."""
    attrs = purifier.attributes
    labels = {"sensor": device_name(purifier), "room": room_name(purifier)}

    PURIFIER_MOTOR_SPEED.labels(**labels).set(attrs.motor_state)
    PURIFIER_RUNNING.labels(**labels).set(1 if attrs.motor_state > 0 else 0)
    PURIFIER_FILTER_ALARM.labels(**labels).set(1 if attrs.filter_alarm_status else 0)

    # Enum-as-gauge: set the active mode to 1 and every other mode to 0 so a
    # mode change is reflected immediately without leaving stale series.
    current_mode = getattr(attrs.fan_mode, "value", str(attrs.fan_mode))
    for mode in FanModeEnum:
        PURIFIER_FAN_MODE.labels(sensor=labels["sensor"], room=labels["room"], mode=mode.value).set(
            1 if mode.value == current_mode else 0
        )

    # The STARKVIND's PM2.5 is NOT comparable to the environment sensors' µg/m³
    # (it samples only with airflow, so it reads stale/coarse). Publish it on its
    # own metric, never on the shared environment-sensor metric.
    if attrs.current_p_m25 is not None:
        PURIFIER_PM25.labels(**labels).set(attrs.current_p_m25)


def poll_once(hub: Hub, temp_offset: float) -> int | None:
    """Poll the hub once.

    Returns the number of devices scraped on success, or ``None`` on failure.
    Never raises: any error is recorded as ``scrape_success=0`` and logged.
    """
    try:
        sensors = hub.get_environment_sensors()
        for sensor in sensors:
            update_sensor_metrics(sensor, temp_offset)
        purifiers = hub.get_air_purifiers()
        for purifier in purifiers:
            update_air_purifier_metrics(purifier)
    except Exception as exc:  # noqa: BLE001 - the exporter must survive any hub error
        SCRAPE_SUCCESS.set(0)
        LOG.error("failed to poll DIRIGERA hub: %s", exc)
        return None

    SCRAPE_SUCCESS.set(1)
    LAST_SUCCESS.set(time.time())
    LOG.info(
        "polled %d environment sensor(s) and %d air purifier(s)",
        len(sensors),
        len(purifiers),
    )
    return len(sensors) + len(purifiers)


def run(hub: Hub, config: Config) -> None:  # pragma: no cover - infinite loop
    while True:
        poll_once(hub, config.temp_offset)
        time.sleep(config.poll_interval)


def main() -> None:  # pragma: no cover - process wiring
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
        stream=sys.stdout,
    )
    # The hub serves its local API behind a self-signed certificate; the
    # dirigera client talks to it with verify=False, which would otherwise emit
    # an InsecureRequestWarning on every poll.
    warnings.simplefilter("ignore", urllib3.exceptions.InsecureRequestWarning)

    config = Config()
    config.validate()

    # Report unhealthy until the first poll succeeds.
    SCRAPE_SUCCESS.set(0)
    start_http_server(config.listen_port)
    LOG.info(
        "serving metrics on :%d, polling %s every %ds (temp offset %+.2f C)",
        config.listen_port,
        config.ip,
        config.poll_interval,
        config.temp_offset,
    )

    hub = Hub(token=config.token, ip_address=config.ip)
    run(hub, config)


if __name__ == "__main__":
    main()
