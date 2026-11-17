#!/usr/bin/env python3
"""Prometheus exporter for IKEA DIRIGERA devices.

Polls a DIRIGERA hub's local HTTPS API on a fixed interval and exposes:

* every environment sensor (e.g. ALPSTUGA / VINDSTYRKA): temperature, humidity,
  CO2 and PM2.5;
* every STARKVIND air purifier: on/off state, fan mode, motor speed, filter
  alarm and its built-in PM2.5 reading;
* every light: on/off state, brightness and colour temperature;
* every motion sensor and light sensor: motion and illuminance;
* every remote control and motion sensor: battery level.

The exporter also listens to the hub's event stream. An event updates the
values at once, between 2 polls, and lets the exporter count every motion and
every press of a remote control. The poll stays the source of truth: it
corrects whatever the event stream missed.

The exporter only reads. It never switches a device.

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

import json
import logging
import os
import sys
import threading
import time
import warnings

import urllib3
from dirigera import Hub
from dirigera.devices.air_purifier import AirPurifier, FanModeEnum, dict_to_air_purifier
from dirigera.devices.controller import dict_to_controller
from dirigera.devices.environment_sensor import EnvironmentSensor, dict_to_environment_sensor
from dirigera.devices.light import dict_to_light
from dirigera.devices.light_sensor import dict_to_light_sensor
from dirigera.devices.motion_sensor import dict_to_motion_sensor
from prometheus_client import Counter, Gauge, start_http_server

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

# --- lights, motion sensors and light sensors ---
LIGHT_ON = Gauge(
    "ikea_light_on",
    "1 if the light is switched on, 0 if it is off.",
    _LABELS,
)
LIGHT_LEVEL = Gauge(
    "ikea_light_level_percent",
    "Brightness the light is set to, 1-100. A light that is off keeps its last level.",
    _LABELS,
)
LIGHT_COLOR_TEMPERATURE = Gauge(
    "ikea_light_color_temperature_kelvin",
    "Colour temperature the light is set to, in kelvin.",
    _LABELS,
)
MOTION_DETECTED = Gauge(
    "ikea_motion_detected",
    "1 if the motion sensor reports motion at the time of the poll, else 0.",
    _LABELS,
)
ILLUMINANCE = Gauge(
    "ikea_illuminance_lux",
    "Illuminance at the light sensor in lux, converted from the hub's Zigbee value.",
    _LABELS,
)

MOTION_LAST = Gauge(
    "ikea_motion_last_detected_timestamp_seconds",
    "Unix time the motion sensor last started to report motion, since the exporter started.",
    _LABELS,
)

# --- counted from the event stream, and from a change between 2 polls ---
# A counter keeps its series when a device is renamed or removed: rate() and
# increase() need the history, and a stale counter does not look like a reading.
MOTION_DETECTIONS = Counter(
    "ikea_motion_detections",
    "Times the motion sensor started to report motion.",
    _LABELS,
)
REMOTE_PRESSES = Counter(
    "ikea_remote_presses",
    "Presses of a remote control, by click pattern (for example singlePress).",
    (*_LABELS, "pattern"),
)

# --- per-device health, for every device the exporter reads ---
DEVICE_BATTERY = Gauge(
    "ikea_dirigera_device_battery_percent",
    "Battery level of a battery-powered device, 0-100.",
    _LABELS,
)
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

# --- rooms ---
ROOM_INFO = Gauge(
    "ikea_dirigera_room_info",
    "1 for each room that holds a device. The icon and the color are those picked in the "
    "IKEA Home smart app. The name of a room is free text; the icon is from a fixed set, "
    "so a dashboard can tell the kind of a room by it.",
    ("room", "icon", "color"),
)
ROOM_ORDER = Gauge(
    "ikea_dirigera_room_order",
    "A number for each room that holds a device: 1 for the room with the oldest device, "
    "2 for the next. No 2 rooms share a number, so a dashboard can give each room a line "
    "style of its own, whatever its name or icon. A new room gets the highest number.",
    ("room",),
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
    LIGHT_ON,
    LIGHT_LEVEL,
    LIGHT_COLOR_TEMPERATURE,
    MOTION_DETECTED,
    MOTION_LAST,
    ILLUMINANCE,
    DEVICE_BATTERY,
    DEVICE_REACHABLE,
    DEVICE_LAST_SEEN,
    ROOM_INFO,
    ROOM_ORDER,
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


EVENTS = Counter(
    "ikea_dirigera_events",
    "Events received from the hub's event stream, by event type.",
    ("type",),
)
EVENT_STREAM_CONNECTED = Gauge(
    "ikea_dirigera_event_stream_connected",
    "1 if the exporter is connected to the hub's event stream, 0 otherwise.",
)


class Config:
    """Runtime configuration, sourced entirely from environment variables."""

    def __init__(self) -> None:
        self.ip = os.environ.get("DIRIGERA_IP", "").strip()
        self.token = os.environ.get("DIRIGERA_TOKEN", "").strip()
        self.poll_interval = int(os.environ.get("POLL_INTERVAL_SECONDS", "30"))
        self.temp_offset = float(os.environ.get("TEMP_OFFSET_CELSIUS", "0.0"))
        self.listen_port = int(os.environ.get("LISTEN_PORT", "9119"))
        event_stream = os.environ.get("EVENT_STREAM", "true").strip().lower()
        self.event_stream = event_stream not in ("0", "false", "no", "off")

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


def relation_id(raw: dict) -> str | None:
    """The ``relationId`` that the sub-devices of one device share, if the hub sent one."""
    relation = raw.get("relationId")
    return relation if isinstance(relation, str) and relation else None


def borrow_sibling_labels(raw: dict, placed: dict[str, dict]) -> dict:
    """Give a roomless Matter sub-device the name and room of its sibling.

    The hub splits one Matter device (e.g. TIMMERFLOTTE) into ``<uuid>_1`` and
    ``<uuid>_2`` with a shared ``relationId``. The app renames and places only
    one of them; the other keeps the model name and no room. When no sibling is
    placed in a room, there is nothing to borrow and ``raw`` is returned as is.
    """
    sibling = placed.get(relation_id(raw))
    if raw.get("room") or sibling is None or sibling is raw:
        return raw
    name = sibling["attributes"].get("customName")
    return {**raw, "room": sibling["room"], "attributes": {**raw["attributes"], "customName": name}}


# One poll's readings: gauge -> label values -> value.
Samples = dict[Gauge, dict[tuple[str, ...], float]]


class _State:
    """What the exporter has published, and when the hub last answered."""

    def __init__(self) -> None:
        self.published: dict[Gauge, set[tuple[str, ...]]] = {g: set() for g in DEVICE_GAUGES}
        # time.monotonic() of the last successful poll, so a wall-clock step
        # (NTP, a VM resume) cannot make the values look fresh or stale.
        self.last_success: float | None = None
        # The duplicate names of the last poll, so each one is logged when it
        # appears and not again at every poll.
        self.duplicates: set[tuple] = set()
        # The /devices list of the last poll, with the events since then merged
        # in. The published values are always built from this list.
        self.devices: list = []
        # Device id -> Unix time its motion sensor last started to report motion.
        self.last_motion: dict[str, float] = {}


STATE = _State()

# The poll loop and the event listener run in 2 threads. Both change STATE and
# publish, so each does it under this lock.
_LOCK = threading.RLock()


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
# health is then the worse of the two: unreachable, the older last-seen time and
# the emptier battery. A shared light is on, and a shared motion sensor sees
# motion, if either device does.
_COMBINE = {
    DEVICE_REACHABLE: min,
    DEVICE_LAST_SEEN: min,
    DEVICE_BATTERY: min,
    LIGHT_ON: max,
    MOTION_DETECTED: max,
}


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


# What the other kinds of device publish: the library's parser, and attribute
# of its model -> gauge. An attribute the hub did not send publishes nothing.
# A motion sensor has an "is_on" of its own; it is not a light and is not read.
_SIMPLE_KINDS: dict[str, tuple] = {
    "light": (
        dict_to_light,
        {
            "is_on": LIGHT_ON,
            "light_level": LIGHT_LEVEL,
            "color_temperature": LIGHT_COLOR_TEMPERATURE,
        },
    ),
    "motion sensor": (
        dict_to_motion_sensor,
        {"is_detected": MOTION_DETECTED, "battery_percentage": DEVICE_BATTERY},
    ),
    "light sensor": (dict_to_light_sensor, {"illuminance": ILLUMINANCE}),
    "remote": (dict_to_controller, {"battery_percentage": DEVICE_BATTERY}),
}


def zigbee_illuminance_to_lux(measured: float) -> float:
    """Convert the hub's illuminance to lux.

    The hub passes on the Zigbee "MeasuredValue", which is 10000 * log10(lux) + 1,
    and 0 for "too dark to measure". This is inferred, not documented by IKEA: a
    live VALLHORN in an entrance hall read 7781 at midday in November. That is
    6 lux as a Zigbee value. 7781 lux would be direct sunlight.
    """
    measured = float(measured)
    return 0.0 if measured <= 0 else 10 ** ((measured - 1) / 10000)


def simple_samples(device, kind: str, samples: Samples) -> None:
    """Add the state of one light, motion sensor, light sensor or remote."""
    if not _health_samples(device, samples):
        return
    labels = (device_name(device), room_name(device))
    for attribute, gauge in _SIMPLE_KINDS[kind][1].items():
        val = getattr(device.attributes, attribute, None)
        if val is not None:
            if gauge is ILLUMINANCE:
                val = zigbee_illuminance_to_lux(val)
            _add(samples, gauge, labels, val)


def device_kind(raw: dict) -> str | None:
    """The kind of device the exporter reads ``raw`` as, or ``None`` to ignore it."""
    device_type = raw.get("deviceType")
    if device_type == "environmentSensor":
        return "environment sensor"
    if raw.get("type") == "airPurifier":
        return "air purifier"
    if device_type == "light":
        return "light"
    if device_type == "motionSensor":
        return "motion sensor"
    if device_type == "lightSensor":
        return "light sensor"
    if raw.get("type") == "controller":
        return "remote"
    return None


def raw_labels(raw: dict, placed: dict[str, dict]) -> tuple[str, str]:
    """The series labels of a raw hub entry: the same as ``device_name`` and ``room_name``."""
    raw = borrow_sibling_labels(raw, placed)
    room = raw.get("room")
    room_label = (room.get("name") if isinstance(room, dict) else None) or "unknown"
    return (raw["attributes"].get("customName") or str(raw["id"]), room_label)


def _placed(devices: list) -> dict[str, dict]:
    # relation_id() takes strings only: any other value from the hub must not
    # raise here, outside the per-device try.
    return {
        relation_id(raw): raw
        for raw in devices
        if isinstance(raw, dict) and relation_id(raw) and raw.get("room")
    }


def _detects_motion(raw: dict | None) -> bool:
    if not isinstance(raw, dict) or raw.get("deviceType") != "motionSensor":
        return False
    attrs = raw.get("attributes")
    return (
        isinstance(attrs, dict)
        and attrs.get("isDetected") is True
        and raw.get("isReachable", True) is not False
    )


def _track_motion(old: dict | None, new: dict, placed: dict[str, dict]) -> None:
    """Count a motion sensor that starts to report motion.

    ``old`` is the entry before the change, or ``None`` for a device seen for
    the first time. A sensor that already reports motion at the first poll is
    not counted: the exporter cannot know when that motion started.
    """
    if not _detects_motion(new) or _detects_motion(old):
        return
    STATE.last_motion[str(new["id"])] = time.time()
    if old is not None:
        MOTION_DETECTIONS.labels(*raw_labels(new, placed)).inc()


def _room_samples(devices: list, samples: Samples) -> None:
    """Add the info and the order number of each room that holds a device.

    Every device counts, also a kind the exporter does not read. Rooms are
    ordered by their oldest device, so a room keeps its number when a newer room
    is added. 2 rooms with one name are 1 room here, as in every other series.
    """
    oldest: dict[str, tuple[str, str]] = {}
    for raw in devices:
        room = raw.get("room") if isinstance(raw, dict) else None
        if not (isinstance(room, dict) and isinstance(room.get("name"), str) and room["name"]):
            continue
        name, icon, color = room["name"], room.get("icon"), room.get("color")
        created = raw.get("createdAt")
        key = (created if isinstance(created, str) else "9999", str(room.get("id", "")))
        if name not in oldest:
            labels = (
                name,
                icon if isinstance(icon, str) else "",
                color if isinstance(color, str) else "",
            )
            samples.setdefault(ROOM_INFO, {})[labels] = 1.0
        if name not in oldest or key < oldest[name]:
            oldest[name] = key
    for number, name in enumerate(sorted(oldest, key=lambda room: (oldest[room], room)), start=1):
        samples.setdefault(ROOM_ORDER, {})[(name,)] = float(number)


def _publish_devices(hub: Hub, temp_offset: float, log: bool = True) -> tuple[dict[str, int], int]:
    """Publish the values of ``STATE.devices``.

    Returns the number of devices of each kind, and the number skipped. With
    ``log=False`` it does not log a skipped device or a duplicate name: the
    rebuild after an event would repeat the warnings of the last poll.
    """
    devices = STATE.devices
    placed = _placed(devices)
    samples: Samples = {}
    # Label values -> (what owns the series, id of the last device added to it).
    owners: dict[tuple[str, str], tuple[str, str]] = {}
    duplicates: set[tuple] = set()
    counts: dict[str, int] = {"environment sensor": 0, "air purifier": 0}
    skipped = 0
    for raw in devices:
        if not isinstance(raw, dict):
            skipped += 1
            if log:
                LOG.warning(
                    "skipped a /devices entry that is a %s, not an object", type(raw).__name__
                )
            continue
        kind = device_kind(raw)
        if kind is None:
            continue
        own: Samples = {}
        try:
            raw = borrow_sibling_labels(raw, placed)
            if kind == "environment sensor":
                device = dict_to_environment_sensor(raw, hub)
                sensor_samples(device, temp_offset, own)
            elif kind == "air purifier":
                device = dict_to_air_purifier(raw, hub)
                purifier_samples(device, own)
            else:
                device = _SIMPLE_KINDS[kind][0](raw, hub)
                simple_samples(device, kind, own)
            labels = (device_name(device), room_name(device))
            # A counter that first appears at 1 loses that step in increase().
            # So each counter starts at 0 as soon as the device is known.
            if kind == "motion sensor":
                MOTION_DETECTIONS.labels(*labels)
                last_motion = STATE.last_motion.get(device.id)
                if last_motion is not None:
                    _add(own, MOTION_LAST, labels, last_motion)
            elif kind == "remote":
                REMOTE_PRESSES.labels(*labels, "singlePress")
        except Exception as exc:  # noqa: BLE001 - one bad device must not stop the poll
            skipped += 1
            if log:
                LOG.warning("skipped device %s: %s", raw.get("id", "?"), exc)
            continue
        # Siblings of one Matter device share their series on purpose.
        owner = relation_id(raw) or device.id
        if labels in owners and owners[labels][0] != owner:
            duplicate = (frozenset((owners[labels][1], device.id)), labels)
            duplicates.add(duplicate)
            if log and duplicate not in STATE.duplicates:
                LOG.warning(
                    "devices %s and %s are both named %r in room %r, so they share their "
                    "series; give each device a unique name in its room",
                    owners[labels][1],
                    device.id,
                    *labels,
                )
        owners[labels] = (owner, device.id)
        _merge(samples, own)
        counts[kind] = counts.get(kind, 0) + 1

    _room_samples(devices, samples)

    publish(samples)
    if log:
        STATE.duplicates = duplicates
    return counts, skipped


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
        with _LOCK:
            if STATE.last_success is None or time.monotonic() - STATE.last_success > stale_after:
                # An event must not publish the old list again.
                STATE.devices = []
                publish({})
        return None

    with _LOCK:
        try:
            # A motion that started between 2 polls while the event stream was
            # down still counts once, if the sensor still reports it.
            before = {raw["id"]: raw for raw in STATE.devices if _has_id(raw)}
            placed = _placed(devices)
            for raw in devices:
                if _has_id(raw):
                    _track_motion(before.get(raw["id"]), raw, placed)
        except Exception as exc:  # noqa: BLE001 - counting must not stop the poll
            LOG.warning("could not count motion in this poll: %s", exc)
        # A copy: an event replaces an entry, and the list is the caller's.
        STATE.devices = list(devices)
        counts, skipped = _publish_devices(hub, temp_offset)
        STATE.last_success = time.monotonic()
        SCRAPE_SUCCESS.set(1)
        LAST_SUCCESS.set(time.time())
    LOG.info(
        "polled %s%s",
        ", ".join(f"{count} {kind}(s)" for kind, count in counts.items()),
        f", skipped {skipped}" if skipped else "",
    )
    return sum(counts.values())


def _has_id(raw) -> bool:
    return isinstance(raw, dict) and isinstance(raw.get("id"), str)


def handle_event(hub: Hub, message, temp_offset: float) -> None:
    """Apply one message of the hub's event stream.

    ``deviceStateChanged`` carries the attributes that changed. They are merged
    into the device of the last poll, and the values are published again.
    ``remotePressEvent`` is counted. Never raises: a message the exporter does
    not understand is logged and dropped, and the next poll corrects the state.
    """
    try:
        event = json.loads(message)
        event_type = event.get("type") if isinstance(event, dict) else None
        if not isinstance(event_type, str):
            raise TypeError("no event type")
        EVENTS.labels(event_type).inc()
        data = event.get("data")
        if not _has_id(data):
            return
        with _LOCK:
            index = next(
                (
                    i
                    for i, raw in enumerate(STATE.devices)
                    if _has_id(raw) and raw["id"] == data["id"]
                ),
                None,
            )
            if index is None:
                return  # A device the last poll did not list: the next poll adds it.
            old = STATE.devices[index]
            placed = _placed(STATE.devices)
            if event_type == "remotePressEvent":
                pattern = data.get("clickPattern")
                pattern = pattern if isinstance(pattern, str) else "unknown"
                REMOTE_PRESSES.labels(*raw_labels(old, placed), pattern).inc()
            elif event_type == "deviceStateChanged":
                new = {**old, **{k: v for k, v in data.items() if k != "attributes"}}
                changed = data.get("attributes")
                if isinstance(changed, dict):
                    new["attributes"] = {**old.get("attributes", {}), **changed}
                _track_motion(old, new, placed)
                STATE.devices[index] = new
                _publish_devices(hub, temp_offset, log=False)
    except Exception as exc:  # noqa: BLE001 - one bad message must not stop the listener
        LOG.warning("dropped an event from the hub: %s", exc)


def listen(config: Config) -> None:  # pragma: no cover - infinite loop
    """Listen to the hub's event stream for ever, and reconnect when it closes."""
    # A Hub of its own: create_event_listener() keeps its connection on the Hub.
    hub = Hub(token=config.token, ip_address=config.ip)
    connected = [False]

    def on_open(_ws) -> None:
        EVENT_STREAM_CONNECTED.set(1)
        if not connected[0]:
            LOG.info("connected to the event stream of the hub")
        connected[0] = True

    def on_message(_ws, message) -> None:
        handle_event(hub, message, config.temp_offset)

    def on_error(_ws, error) -> None:
        if connected[0]:
            LOG.warning("event stream error: %s", error)

    while True:
        try:
            # Returns when the connection closes. The hub closes an idle
            # connection on its own, so a close is normal and is not logged.
            hub.create_event_listener(on_open=on_open, on_message=on_message, on_error=on_error)
        except Exception as exc:  # noqa: BLE001 - the listener must survive any hub error
            if connected[0]:
                LOG.warning("event stream stopped: %s", exc)
            connected[0] = False
        EVENT_STREAM_CONNECTED.set(0)
        time.sleep(5)


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

    EVENT_STREAM_CONNECTED.set(0)
    if config.event_stream:
        threading.Thread(target=listen, args=(config,), name="events", daemon=True).start()

    hub = Hub(token=config.token, ip_address=config.ip)
    run(hub, config)


if __name__ == "__main__":
    main()
