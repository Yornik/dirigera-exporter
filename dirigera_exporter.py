#!/usr/bin/env python3
"""Prometheus exporter for IKEA DIRIGERA air-quality devices.

Polls a DIRIGERA hub's local HTTPS API on a fixed interval and exposes:

* every environment sensor (e.g. ALPSTUGA / VINDSTYRKA): temperature, humidity,
  CO2 and PM2.5;
* every STARKVIND air purifier: on/off state, fan mode, motor speed, filter
  alarm and its built-in PM2.5 reading.

Configuration is via environment variables only; no secrets are baked into the
image. The exporter is designed to never crash on hub/network errors: a failed
poll sets ``ikea_dirigera_scrape_success`` to 0 and logs the error. The last
values stay up for up to 3 poll intervals, then the device series are removed,
so a dashboard never shows an old reading as current.

Each successful poll publishes exactly what it saw. A reading the hub does not
send, a device it cannot reach, and a renamed or removed device all drop out
instead of keeping their last value.
"""

from __future__ import annotations

import logging
import os
import sys
import time
import warnings

import urllib3
from dirigera import Hub
from dirigera.devices.air_purifier import AirPurifier, FanModeEnum, dict_to_air_purifier
from dirigera.devices.environment_sensor import EnvironmentSensor, dict_to_environment_sensor
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

# --- per-device health, for environment sensors and air purifiers ---
DEVICE_REACHABLE = Gauge(
    "ikea_dirigera_device_reachable",
    "1 if the hub can reach the device, 0 if not. An unreachable device publishes no readings.",
    _LABELS,
)
DEVICE_LAST_SEEN = Gauge(
    "ikea_dirigera_device_last_seen_timestamp_seconds",
    "Unix time the hub last heard from the device.",
    _LABELS,
)

# Every gauge with per-device series. publish() keeps each of them equal to the
# latest poll.
DEVICE_GAUGES = (
    TEMPERATURE,
    HUMIDITY,
    CO2,
    PM25,
    PURIFIER_RUNNING,
    PURIFIER_MOTOR_SPEED,
    PURIFIER_FAN_MODE,
    PURIFIER_FILTER_ALARM,
    PURIFIER_PM25,
    DEVICE_REACHABLE,
    DEVICE_LAST_SEEN,
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

    @property
    def stale_after(self) -> float:
        """Seconds without a successful poll before the device series are removed."""
        return 3 * self.poll_interval

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


# One poll's readings: gauge -> label values -> value.
Samples = dict[Gauge, dict[tuple[str, ...], float]]


class _State:
    """What the exporter has published, and when the hub last answered."""

    def __init__(self) -> None:
        self.published: dict[Gauge, set[tuple[str, ...]]] = {g: set() for g in DEVICE_GAUGES}
        # time.monotonic() of the last successful poll, so a wall-clock step
        # (NTP, a VM resume) cannot make the values look fresh or stale.
        self.last_success: float | None = None


STATE = _State()


def publish(samples: Samples) -> None:
    """Make every device gauge show exactly ``samples``.

    A label set that is not in ``samples`` is removed. A prometheus_client gauge
    otherwise keeps its last value for ever, so a reading that stops arriving
    would look current.
    """
    for gauge in DEVICE_GAUGES:
        new = samples.get(gauge, {})
        for labels in STATE.published[gauge] - new.keys():
            gauge.remove(*labels)
        for labels, val in new.items():
            gauge.labels(*labels).set(val)
        STATE.published[gauge] = set(new)


def _add(samples: Samples, gauge: Gauge, labels: tuple[str, ...], val: float) -> None:
    # float() here, inside the per-device try, so a value that is not a number
    # skips that device instead of failing publish() for every device.
    samples.setdefault(gauge, {})[labels] = float(val)


# Two devices with the same name in the same room share their series. Their
# health is then the worse of the two: unreachable, and the older last-seen time.
_COMBINE = {DEVICE_REACHABLE: min, DEVICE_LAST_SEEN: min}


def _merge(samples: Samples, own: Samples) -> None:
    for gauge, series in own.items():
        target = samples.setdefault(gauge, {})
        combine = _COMBINE.get(gauge)
        for labels, val in series.items():
            if combine is not None and labels in target:
                val = combine(target[labels], val)
            target[labels] = val


def _health_samples(device, samples: Samples) -> bool:
    """Add reachability and last-seen; return True if the device is reachable."""
    labels = (device_name(device), room_name(device))
    reachable = bool(getattr(device, "is_reachable", True))
    _add(samples, DEVICE_REACHABLE, labels, 1 if reachable else 0)
    last_seen = getattr(device, "last_seen", None)
    if last_seen is not None:
        _add(samples, DEVICE_LAST_SEEN, labels, last_seen.timestamp())
    return reachable


def sensor_samples(sensor: EnvironmentSensor, temp_offset: float, samples: Samples) -> None:
    """Add one environment sensor's valid readings to ``samples``.

    Only fields the sensor actually reports are emitted, so a sensor that lacks
    a CO2 or PM2.5 channel does not publish a misleading ``0``.
    """
    if not _health_samples(sensor, samples):
        return
    attrs = sensor.attributes
    labels = (device_name(sensor), room_name(sensor))

    # When a sensor drops out, the hub reports 0 for every reading (seen for 23 h
    # on 2026-09-14). 0 % humidity cannot happen indoors, so it marks the whole
    # set as invalid. A PM2.5 of 0 is real clean air and cannot mark anything.
    if attrs.current_r_h == 0:
        return
    if attrs.current_temperature is not None:
        # The offset goes on a valid reading only, never on a dropout.
        _add(samples, TEMPERATURE, labels, attrs.current_temperature + temp_offset)
    if attrs.current_r_h is not None:
        _add(samples, HUMIDITY, labels, attrs.current_r_h)
    # 0 ppm CO2 cannot happen either: outdoor air has about 420 ppm.
    if attrs.current_c_o2:
        _add(samples, CO2, labels, attrs.current_c_o2)
    if attrs.current_p_m25 is not None:
        _add(samples, PM25, labels, attrs.current_p_m25)


def purifier_samples(purifier: AirPurifier, samples: Samples) -> None:
    """Add one STARKVIND air purifier's state to ``samples``."""
    if not _health_samples(purifier, samples):
        return
    attrs = purifier.attributes
    labels = (device_name(purifier), room_name(purifier))

    _add(samples, PURIFIER_MOTOR_SPEED, labels, attrs.motor_state)
    _add(samples, PURIFIER_RUNNING, labels, 1 if attrs.motor_state > 0 else 0)
    _add(samples, PURIFIER_FILTER_ALARM, labels, 1 if attrs.filter_alarm_status else 0)

    # Enum-as-gauge: set the active mode to 1 and every other mode to 0 so a
    # mode change is reflected immediately without leaving stale series.
    current_mode = getattr(attrs.fan_mode, "value", str(attrs.fan_mode))
    for mode in FanModeEnum:
        _add(
            samples,
            PURIFIER_FAN_MODE,
            (*labels, mode.value),
            1 if mode.value == current_mode else 0,
        )

    # The STARKVIND's PM2.5 is NOT comparable to the environment sensors' µg/m³
    # (it samples only with airflow, so it reads stale/coarse). Publish it on its
    # own metric, never on the shared environment-sensor metric.
    if attrs.current_p_m25 is not None:
        _add(samples, PURIFIER_PM25, labels, attrs.current_p_m25)


def poll_once(hub: Hub, temp_offset: float, stale_after: float = 90.0) -> int | None:
    """Poll the hub once.

    Returns the number of devices scraped on success, or ``None`` on failure.
    Never raises: any error is recorded as ``scrape_success=0`` and logged. After
    a failure, the last values stay up until ``stale_after`` seconds have passed
    since the last successful poll; then the device series are removed. The
    default is 3 times the default poll interval; see ``Config.stale_after``.
    """
    try:
        # One request for all devices. The library's get_environment_sensors()
        # and get_air_purifiers() each fetch /devices and parse the whole list,
        # so one malformed device would fail the poll for every device.
        devices = hub.get("/devices")
        if not isinstance(devices, list):
            raise TypeError(f"/devices returned {type(devices).__name__}, not a list")
    except Exception as exc:  # noqa: BLE001 - the exporter must survive any hub error
        SCRAPE_SUCCESS.set(0)
        LOG.error("failed to poll DIRIGERA hub: %s", exc)
        if STATE.last_success is None or time.monotonic() - STATE.last_success > stale_after:
            publish({})
        return None

    samples: Samples = {}
    owners: dict[tuple[str, str], str] = {}
    sensors = purifiers = skipped = 0
    for raw in devices:
        if not isinstance(raw, dict):
            skipped += 1
            LOG.warning("skipped a /devices entry that is a %s, not an object", type(raw).__name__)
            continue
        if raw.get("deviceType") == "environmentSensor":
            kind = "sensor"
        elif raw.get("type") == "airPurifier":
            kind = "purifier"
        else:
            continue
        own: Samples = {}
        try:
            if kind == "sensor":
                device = dict_to_environment_sensor(raw, hub)
                sensor_samples(device, temp_offset, own)
            else:
                device = dict_to_air_purifier(raw, hub)
                purifier_samples(device, own)
        except Exception as exc:  # noqa: BLE001 - one bad device must not stop the poll
            skipped += 1
            LOG.warning("skipped device %s: %s", raw.get("id", "?"), exc)
            continue
        labels = (device_name(device), room_name(device))
        if labels in owners:
            LOG.warning(
                "devices %s and %s are both named %r in room %r, so they share their "
                "series; give each device a unique name in its room",
                owners[labels],
                device.id,
                *labels,
            )
        owners[labels] = device.id
        _merge(samples, own)
        if kind == "sensor":
            sensors += 1
        else:
            purifiers += 1

    publish(samples)
    STATE.last_success = time.monotonic()
    SCRAPE_SUCCESS.set(1)
    LAST_SUCCESS.set(time.time())
    LOG.info(
        "polled %d environment sensor(s) and %d air purifier(s)%s",
        sensors,
        purifiers,
        f", skipped {skipped}" if skipped else "",
    )
    return sensors + purifiers


def run(hub: Hub, config: Config) -> None:  # pragma: no cover - infinite loop
    while True:
        poll_once(hub, config.temp_offset, stale_after=config.stale_after)
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
