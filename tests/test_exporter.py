"""Unit tests for the DIRIGERA exporter.

All tests run against a fake hub that serves raw ``/devices`` JSON, which goes
through the dirigera library's real device parsers. No live hub is required.
"""

from __future__ import annotations

import datetime
import json

import pytest
from dirigera.hub.abstract_smart_home_hub import AbstractSmartHomeHub
from prometheus_client import REGISTRY

import dirigera_exporter as ex

LAST_SEEN = "2026-10-24T12:00:00.000Z"
LAST_SEEN_TS = datetime.datetime(2026, 10, 24, 12, 0, tzinfo=datetime.UTC).timestamp()


def _room(room):
    return {"id": f"room-{room}", "name": room, "color": "ikea_green_no_65", "icon": "rooms_sofa"}


def _device(device_id, dtype, device_type, attrs, room, reachable, last_seen):
    raw = {
        "id": device_id,
        "type": dtype,
        "deviceType": device_type,
        "createdAt": "2026-06-01T00:00:00.000Z",
        "isReachable": reachable,
        "lastSeen": last_seen,
        "attributes": {
            "model": "test model",
            "manufacturer": "IKEA of Sweden",
            "firmwareVersion": "1.0.0",
            "hardwareVersion": "1",
            **{k: v for k, v in attrs.items() if v is not None},
        },
        "capabilities": {"canSend": [], "canReceive": ["customName"]},
        "deviceSet": [],
        "remoteLinks": [],
    }
    if room is not None:
        raw["room"] = _room(room)
    return raw


def make_sensor(
    name="ALPSTUGA",
    room="Living room",
    temp=21.0,
    rh=45,
    co2=700,
    pm25=8,
    sensor_id="sensor-1",
    reachable=True,
    last_seen=LAST_SEEN,
):
    """Raw /devices entry of an environment sensor, as the hub sends it."""
    attrs = {
        "customName": name,
        "currentTemperature": temp,
        "currentRH": rh,
        "currentCO2": co2,
        "currentPM25": pm25,
    }
    return _device(sensor_id, "sensor", "environmentSensor", attrs, room, reachable, last_seen)


def make_purifier(
    name="STARKVIND",
    room="Living room",
    motor=20,
    pm25=12,
    filter_alarm=False,
    mode="auto",
    purifier_id="purifier-1",
    reachable=True,
    last_seen=LAST_SEEN,
):
    """Raw /devices entry of a STARKVIND air purifier, as the hub sends it."""
    attrs = {
        "customName": name,
        "fanMode": mode,
        "fanModeSequence": "lowMediumHighAuto",
        "motorState": motor,
        "childLock": False,
        "statusLight": True,
        "motorRuntime": 100,
        "filterAlarmStatus": filter_alarm,
        "filterElapsedTime": 1000,
        "filterLifetime": 259200,
        "currentPM25": pm25,
    }
    return _device(purifier_id, "airPurifier", "airPurifier", attrs, room, reachable, last_seen)


class FakeHub(AbstractSmartHomeHub):
    """A hub that serves raw /devices JSON. The device parsers require the base class."""

    def __init__(self, sensors=None, purifiers=None, error=None, others=None):
        self.devices = [*(sensors or []), *(purifiers or []), *(others or [])]
        self.error = error
        self.requests = []

    def get(self, route):
        self.requests.append(route)
        if self.error is not None:
            raise self.error
        return self.devices

    # The exporter only reads. Any write to the hub fails the test.
    def patch(self, route, data):
        raise AssertionError(f"exporter wrote to the hub: PATCH {route}")

    def post(self, route, data=None):
        raise AssertionError(f"exporter wrote to the hub: POST {route}")

    def delete(self, route, data=None):
        raise AssertionError(f"exporter wrote to the hub: DELETE {route}")


@pytest.fixture(autouse=True)
def _reset_metrics(monkeypatch):
    for metric in (*ex.DEVICE_GAUGES, ex.MOTION_DETECTIONS, ex.REMOTE_PRESSES, ex.EVENTS):
        metric.clear()
    monkeypatch.setattr(ex, "STATE", ex._State())
    ex.SCRAPE_SUCCESS.set(0)
    ex.LAST_SUCCESS.set(0)
    yield


def value(metric, **labels):
    return REGISTRY.get_sample_value(metric, labels)


LIVING = {"sensor": "ALPSTUGA", "room": "Living room"}
PURIFIER = {"sensor": "STARKVIND", "room": "Living room"}


def test_metric_mapping_and_success():
    hub = FakeHub([make_sensor()])
    count = ex.poll_once(hub, temp_offset=0.0)

    assert count == 1
    assert value("ikea_air_temperature_celsius", **LIVING) == 21.0
    assert value("ikea_air_humidity_percent", **LIVING) == 45
    assert value("ikea_air_co2_ppm", **LIVING) == 700
    assert value("ikea_air_pm25_micrograms_per_cubic_meter", **LIVING) == 8
    assert value("ikea_dirigera_device_reachable", **LIVING) == 1
    assert value("ikea_dirigera_device_last_seen_timestamp_seconds", **LIVING) == LAST_SEEN_TS
    assert value("ikea_dirigera_scrape_success") == 1
    assert value("ikea_dirigera_last_success_timestamp_seconds") > 0


def test_one_request_per_poll():
    hub = FakeHub([make_sensor()], [make_purifier()])
    ex.poll_once(hub, temp_offset=0.0)

    assert hub.requests == ["/devices"]


def test_temp_offset_applies_to_temperature_only():
    hub = FakeHub([make_sensor(temp=24.0, rh=50)])
    ex.poll_once(hub, temp_offset=-2.0)

    assert value("ikea_air_temperature_celsius", **LIVING) == 22.0
    # Humidity must be untouched by the temperature offset.
    assert value("ikea_air_humidity_percent", **LIVING) == 50


def test_scrape_failure_sets_success_zero():
    hub = FakeHub(error=RuntimeError("hub unreachable"))
    count = ex.poll_once(hub, temp_offset=0.0)

    assert count is None
    assert value("ikea_dirigera_scrape_success") == 0


def test_missing_field_is_not_emitted():
    hub = FakeHub([make_sensor(co2=None)])
    ex.poll_once(hub, temp_offset=0.0)

    # CO2 is absent -> no series at all (not a misleading 0).
    assert value("ikea_air_co2_ppm", **LIVING) is None
    assert value("ikea_air_temperature_celsius", **LIVING) == 21.0


def test_field_that_stops_arriving_is_removed():
    hub = FakeHub([make_sensor(co2=700)])
    ex.poll_once(hub, temp_offset=0.0)
    hub.devices = [make_sensor(co2=None)]
    ex.poll_once(hub, temp_offset=0.0)

    # The old 700 ppm must not stay up as if it were current.
    assert value("ikea_air_co2_ppm", **LIVING) is None


def test_dropout_zeros_remove_the_readings_and_good_readings_return():
    hub = FakeHub([make_sensor()])
    ex.poll_once(hub, temp_offset=0.0)

    # A sensor that drops out: the hub reports 0 for every reading.
    hub.devices = [make_sensor(temp=0.0, rh=0, co2=0, pm25=0)]
    ex.poll_once(hub, temp_offset=0.0)
    for metric in (
        "ikea_air_temperature_celsius",
        "ikea_air_humidity_percent",
        "ikea_air_co2_ppm",
        "ikea_air_pm25_micrograms_per_cubic_meter",
    ):
        assert value(metric, **LIVING) is None, metric
    # The device itself is still known and reachable.
    assert value("ikea_dirigera_device_reachable", **LIVING) == 1

    hub.devices = [make_sensor(temp=20.5)]
    ex.poll_once(hub, temp_offset=0.0)
    assert value("ikea_air_temperature_celsius", **LIVING) == 20.5


def test_humidity_zero_drops_every_reading_of_the_sensor():
    # Only humidity reads 0, the other readings look plausible.
    hub = FakeHub([make_sensor(temp=23.0, rh=0, co2=650, pm25=5)])
    ex.poll_once(hub, temp_offset=-2.0)

    for metric in (
        "ikea_air_temperature_celsius",
        "ikea_air_humidity_percent",
        "ikea_air_co2_ppm",
        "ikea_air_pm25_micrograms_per_cubic_meter",
    ):
        assert value(metric, **LIVING) is None, metric
    assert value("ikea_dirigera_device_reachable", **LIVING) == 1


def test_dropout_with_offset_is_not_published_as_the_offset():
    hub = FakeHub([make_sensor(temp=0.0, rh=0, co2=0, pm25=0)])
    ex.poll_once(hub, temp_offset=-2.0)

    # Before the fix this was published as -2.0 °C.
    assert value("ikea_air_temperature_celsius", **LIVING) is None


def test_co2_zero_alone_drops_only_co2():
    hub = FakeHub([make_sensor(co2=0)])
    ex.poll_once(hub, temp_offset=0.0)

    assert value("ikea_air_co2_ppm", **LIVING) is None
    assert value("ikea_air_temperature_celsius", **LIVING) == 21.0
    assert value("ikea_air_humidity_percent", **LIVING) == 45


def test_pm25_zero_is_clean_air_and_kept():
    hub = FakeHub([make_sensor(pm25=0)])
    ex.poll_once(hub, temp_offset=0.0)

    assert value("ikea_air_pm25_micrograms_per_cubic_meter", **LIVING) == 0


def test_unreachable_sensor_publishes_health_only():
    hub = FakeHub([make_sensor()])
    ex.poll_once(hub, temp_offset=0.0)
    hub.devices = [make_sensor(reachable=False)]
    ex.poll_once(hub, temp_offset=0.0)

    assert value("ikea_dirigera_device_reachable", **LIVING) == 0
    assert value("ikea_dirigera_device_last_seen_timestamp_seconds", **LIVING) == LAST_SEEN_TS
    assert value("ikea_air_temperature_celsius", **LIVING) is None
    assert value("ikea_air_co2_ppm", **LIVING) is None


def test_unreachable_purifier_publishes_health_only():
    hub = FakeHub(purifiers=[make_purifier(reachable=False)])
    ex.poll_once(hub, temp_offset=0.0)

    assert value("ikea_dirigera_device_reachable", **PURIFIER) == 0
    assert value("ikea_air_purifier_motor_speed", **PURIFIER) is None
    assert value("ikea_air_purifier_fan_mode", **PURIFIER, mode="auto") is None


def test_renamed_or_moved_device_drops_its_old_series():
    hub = FakeHub([make_sensor(name="ALPSTUGA", room="Living room")])
    ex.poll_once(hub, temp_offset=0.0)
    hub.devices = [make_sensor(name="Kitchen sensor", room="Kitchen")]
    ex.poll_once(hub, temp_offset=0.0)

    assert value("ikea_air_temperature_celsius", **LIVING) is None
    assert value("ikea_dirigera_device_reachable", **LIVING) is None
    assert value("ikea_air_temperature_celsius", sensor="Kitchen sensor", room="Kitchen") == 21.0


def test_removed_device_drops_its_series():
    hub = FakeHub([make_sensor()], [make_purifier()])
    ex.poll_once(hub, temp_offset=0.0)
    hub.devices = [make_sensor()]
    ex.poll_once(hub, temp_offset=0.0)

    assert value("ikea_air_purifier_motor_speed", **PURIFIER) is None
    assert value("ikea_air_purifier_fan_mode", **PURIFIER, mode="auto") is None
    assert value("ikea_air_temperature_celsius", **LIVING) == 21.0


def test_bad_device_does_not_stop_the_poll():
    broken = make_purifier()
    del broken["attributes"]["motorState"]  # a required field the hub left out
    hub = FakeHub([make_sensor()], [broken])
    count = ex.poll_once(hub, temp_offset=0.0)

    assert count == 1
    assert value("ikea_dirigera_scrape_success") == 1
    assert value("ikea_air_temperature_celsius", **LIVING) == 21.0
    assert value("ikea_air_purifier_motor_speed", **PURIFIER) is None


def test_device_that_fails_midway_publishes_nothing(monkeypatch):
    real = ex.purifier_samples

    def add_samples_then_fail(purifier, samples):
        real(purifier, samples)
        raise ValueError("bad value")

    monkeypatch.setattr(ex, "purifier_samples", add_samples_then_fail)
    hub = FakeHub([make_sensor()], [make_purifier()])
    count = ex.poll_once(hub, temp_offset=0.0)

    assert count == 1
    # The samples the purifier added before it failed must not leak out.
    assert value("ikea_dirigera_device_reachable", **PURIFIER) is None
    assert value("ikea_air_purifier_motor_speed", **PURIFIER) is None
    assert value("ikea_air_temperature_celsius", **LIVING) == 21.0


@pytest.mark.parametrize("payload", [None, {"error": "busy"}, "busy"])
def test_devices_reply_that_is_not_a_list_is_a_failed_poll(payload):
    hub = FakeHub([make_sensor()])
    ex.poll_once(hub, temp_offset=0.0)
    hub.devices = payload
    count = ex.poll_once(hub, temp_offset=0.0)

    assert count is None
    assert value("ikea_dirigera_scrape_success") == 0
    # Like any failed poll, the last values stay for now.
    assert value("ikea_air_temperature_celsius", **LIVING) == 21.0


@pytest.mark.parametrize("entry", ["x", None, 42])
def test_devices_entry_that_is_not_an_object_is_skipped(entry):
    hub = FakeHub([make_sensor()], others=[entry])
    count = ex.poll_once(hub, temp_offset=0.0)

    assert count == 1
    assert value("ikea_dirigera_scrape_success") == 1
    assert value("ikea_air_temperature_celsius", **LIVING) == 21.0


@pytest.mark.parametrize("bad_first", [False, True])
def test_two_sensors_with_one_name_combine_their_health(caplog, bad_first):
    good = make_sensor(sensor_id="good")
    bad = make_sensor(sensor_id="bad", reachable=False, last_seen="2026-10-24T11:00:00.000Z")
    hub = FakeHub([bad, good] if bad_first else [good, bad])
    ex.poll_once(hub, temp_offset=0.0)

    # The worse health of the two wins, whichever comes first.
    assert value("ikea_dirigera_device_reachable", **LIVING) == 0
    last_seen = value("ikea_dirigera_device_last_seen_timestamp_seconds", **LIVING)
    assert last_seen == LAST_SEEN_TS - 3600
    assert "are both named 'ALPSTUGA' in room 'Living room'" in caplog.text


def test_sensor_and_purifier_with_one_name_keep_their_readings():
    hub = FakeHub([make_sensor(name="Air")], [make_purifier(name="Air", reachable=False)])
    ex.poll_once(hub, temp_offset=0.0)

    air = {"sensor": "Air", "room": "Living room"}
    assert value("ikea_dirigera_device_reachable", **air) == 0
    assert value("ikea_air_temperature_celsius", **air) == 21.0


def test_other_device_types_are_ignored():
    blind = _device(
        "blind-1", "blinds", "blinds", {"customName": "Blind"}, "Bedroom", True, LAST_SEEN
    )
    hub = FakeHub([make_sensor()], others=[blind])
    count = ex.poll_once(hub, temp_offset=0.0)

    assert count == 1
    assert value("ikea_dirigera_device_reachable", sensor="Blind", room="Bedroom") is None


def test_failure_keeps_values_briefly_then_removes_them(monkeypatch):
    clock = [1000.0]
    monkeypatch.setattr(ex.time, "monotonic", lambda: clock[0])
    hub = FakeHub([make_sensor()])
    ex.poll_once(hub, temp_offset=0.0, stale_after=90)

    hub.error = RuntimeError("hub unreachable")
    clock[0] += 60
    ex.poll_once(hub, temp_offset=0.0, stale_after=90)
    # One missed poll: the reading stays, so one failure does not cut the line.
    assert value("ikea_air_temperature_celsius", **LIVING) == 21.0

    clock[0] += 60
    ex.poll_once(hub, temp_offset=0.0, stale_after=90)
    # 120 s since the last success: an old value must not look current.
    assert value("ikea_air_temperature_celsius", **LIVING) is None
    assert value("ikea_dirigera_device_reachable", **LIVING) is None
    assert value("ikea_dirigera_scrape_success") == 0

    hub.error = None
    ex.poll_once(hub, temp_offset=0.0, stale_after=90)
    assert value("ikea_air_temperature_celsius", **LIVING) == 21.0


def test_wall_clock_step_does_not_remove_values(monkeypatch):
    monkeypatch.setattr(ex.time, "monotonic", lambda: 1000.0)
    hub = FakeHub([make_sensor()])
    ex.poll_once(hub, temp_offset=0.0, stale_after=90)

    # NTP moves the wall clock a day forward, then one poll fails.
    monkeypatch.setattr(ex.time, "time", lambda: 2e9 + 86400)
    hub.error = RuntimeError("hub unreachable")
    ex.poll_once(hub, temp_offset=0.0, stale_after=90)

    assert value("ikea_air_temperature_celsius", **LIVING) == 21.0


def test_stale_window_is_three_poll_intervals(monkeypatch):
    monkeypatch.setenv("POLL_INTERVAL_SECONDS", "10")

    assert ex.Config().stale_after == 30


def test_missing_room_defaults_to_unknown():
    hub = FakeHub([make_sensor(room=None)])
    ex.poll_once(hub, temp_offset=0.0)

    assert value("ikea_air_temperature_celsius", sensor="ALPSTUGA", room="unknown") == 21.0


def test_multiple_sensors_each_get_labels():
    hub = FakeHub(
        [
            make_sensor(name="ALPSTUGA", room="Living room", temp=21.0, sensor_id="a"),
            make_sensor(name="VINDSTYRKA", room="Bedroom", temp=19.0, sensor_id="b"),
        ]
    )
    count = ex.poll_once(hub, temp_offset=0.0)

    assert count == 2
    assert value("ikea_air_temperature_celsius", sensor="ALPSTUGA", room="Living room") == 21.0
    assert value("ikea_air_temperature_celsius", sensor="VINDSTYRKA", room="Bedroom") == 19.0


def test_air_purifier_metrics():
    hub = FakeHub(purifiers=[make_purifier(motor=20, pm25=12, filter_alarm=False, mode="auto")])
    count = ex.poll_once(hub, temp_offset=0.0)

    assert count == 1  # 0 sensors + 1 purifier
    assert value("ikea_air_purifier_running", **PURIFIER) == 1
    assert value("ikea_air_purifier_motor_speed", **PURIFIER) == 20
    assert value("ikea_air_purifier_filter_alarm", **PURIFIER) == 0
    assert value("ikea_dirigera_device_reachable", **PURIFIER) == 1
    # The purifier's PM2.5 goes on its own metric, never the env-sensor µg/m³ one.
    assert value("ikea_air_purifier_pm25", **PURIFIER) == 12
    assert value("ikea_air_pm25_micrograms_per_cubic_meter", **PURIFIER) is None
    # Active fan mode is 1, every other mode is 0.
    assert value("ikea_air_purifier_fan_mode", **PURIFIER, mode="auto") == 1
    assert value("ikea_air_purifier_fan_mode", **PURIFIER, mode="off") == 0


def test_air_purifier_off_when_motor_zero():
    hub = FakeHub(purifiers=[make_purifier(motor=0)])
    ex.poll_once(hub, temp_offset=0.0)

    assert value("ikea_air_purifier_running", **PURIFIER) == 0


def test_air_purifier_filter_alarm():
    hub = FakeHub(purifiers=[make_purifier(filter_alarm=True)])
    ex.poll_once(hub, temp_offset=0.0)

    assert value("ikea_air_purifier_filter_alarm", **PURIFIER) == 1


def test_sensors_and_purifier_counted_together():
    hub = FakeHub(sensors=[make_sensor()], purifiers=[make_purifier()])
    count = ex.poll_once(hub, temp_offset=0.0)

    assert count == 2
    assert value("ikea_dirigera_scrape_success") == 1


def test_matter_siblings_share_name_and_room(caplog):
    # TIMMERFLOTTE: _1 has temperature, name and room; _2 has humidity only.
    temp = make_sensor(
        name="TIMMERFLOTTE Living room", rh=None, co2=None, pm25=None, sensor_id="t_1"
    )
    hum = make_sensor(
        name="TIMMERFLOTTE temp/hmd sensor",
        room=None,
        temp=None,
        rh=63,
        co2=None,
        pm25=None,
        sensor_id="t_2",
    )
    temp["relationId"] = hum["relationId"] = "t"
    ex.poll_once(FakeHub([hum, temp]), temp_offset=0.0)

    labels = {"sensor": "TIMMERFLOTTE Living room", "room": "Living room"}
    assert value("ikea_air_temperature_celsius", **labels) == 21.0
    assert value("ikea_air_humidity_percent", **labels) == 63
    assert (
        value("ikea_air_humidity_percent", sensor="TIMMERFLOTTE temp/hmd sensor", room="unknown")
        is None
    )
    assert "are both named" not in caplog.text


def test_several_matter_devices_stay_separate(caplog):
    devices = []
    for rid, room, rh in (("a", "Living room", 60), ("b", "Bedroom", 50)):
        temp = make_sensor(
            name=f"T {room}", room=room, rh=None, co2=None, pm25=None, sensor_id=f"{rid}_1"
        )
        hum = make_sensor(
            name="TIMMERFLOTTE temp/hmd sensor",
            room=None,
            temp=None,
            rh=rh,
            co2=None,
            pm25=None,
            sensor_id=f"{rid}_2",
        )
        temp["relationId"] = hum["relationId"] = rid
        devices += [temp, hum]
    ex.poll_once(FakeHub(devices), temp_offset=0.0)

    assert value("ikea_air_humidity_percent", sensor="T Living room", room="Living room") == 60
    assert value("ikea_air_humidity_percent", sensor="T Bedroom", room="Bedroom") == 50
    assert "are both named" not in caplog.text


def _matter_pair(relation, room, name="T"):
    """The 2 halves of a TIMMERFLOTTE: temperature with the name, humidity without."""
    temp = make_sensor(
        name=name, room=room, rh=None, co2=None, pm25=None, sensor_id=f"{relation}_1"
    )
    hum = make_sensor(
        name="TIMMERFLOTTE temp/hmd sensor",
        room=None,
        temp=None,
        rh=55,
        co2=None,
        pm25=None,
        sensor_id=f"{relation}_2",
    )
    temp["relationId"] = hum["relationId"] = relation
    return temp, hum


@pytest.mark.parametrize("bad", [["a"], {"a": 1}, 7, True])
def test_relation_id_that_is_not_a_string_does_not_stop_the_poll(caplog, bad):
    temp, hum = _matter_pair("t", "Bedroom")
    temp["relationId"] = hum["relationId"] = bad

    # The 2 badly described devices are skipped; the other one is still published.
    assert ex.poll_once(FakeHub([temp, hum, make_sensor()]), temp_offset=0.0) == 1
    assert value("ikea_dirigera_scrape_success") == 1
    assert value("ikea_air_temperature_celsius", **LIVING) == 21.0
    assert "skipped device t_1" in caplog.text
    assert "skipped device t_2" in caplog.text


def test_empty_relation_id_is_no_relation():
    temp, hum = _matter_pair("t", "Bedroom")
    temp["relationId"] = hum["relationId"] = ""

    # No sibling is found, so each half keeps its own labels.
    assert ex.poll_once(FakeHub([temp, hum]), temp_offset=0.0) == 2
    assert value("ikea_air_temperature_celsius", sensor="T", room="Bedroom") == 21.0
    unplaced = {"sensor": "TIMMERFLOTTE temp/hmd sensor", "room": "unknown"}
    assert value("ikea_air_humidity_percent", **unplaced) == 55


def test_duplicate_warning_names_device_ids_not_relation_ids(caplog):
    # 2 unplaced TIMMERFLOTTEs: their humidity halves share one series.
    devices = [half for relation in ("p", "q") for half in _matter_pair(relation, None)]
    ex.poll_once(FakeHub(devices), temp_offset=0.0)

    assert "devices p_2 and q_2 are both named 'TIMMERFLOTTE temp/hmd sensor'" in caplog.text


# --- lights, motion sensors, light sensors and remotes ---
# The attribute sets below are those of a live hub, without the fields the
# exporter does not read.


def make_light(
    name="Lamp",
    room="Bedroom",
    on=True,
    level=40,
    kelvin=2702,
    light_id="light-1",
    reachable=True,
    last_seen=LAST_SEEN,
):
    attrs = {
        "customName": name,
        "isOn": on,
        "lightLevel": level,
        "colorTemperature": kelvin,
        "colorMode": "temperature",
        "deviceOnBehavior": {"behavior": "lastValue", "profileId": ""},
    }
    return _device(light_id, "light", "light", attrs, room, reachable, last_seen)


def make_remote(relation="wheel", name="Wheel", room="Bedroom", battery=86, reachable=True):
    """The 3 entries of 1 BILRESA scroll wheel: only the first has the name and the room."""
    entries = []
    for suffix, group in (("3", 1), ("6", 2), ("9", 3)):
        named = group == 1
        attrs = {
            "customName": name if named else "BILRESA scroll wheel",
            "batteryPercentage": battery,
            "switchGroup": group,
            "switchLabel": "button",
            "controlMode": "light",
        }
        entry = _device(
            f"{relation}_{suffix}",
            "controller",
            "genericSwitch",
            attrs,
            room if named else None,
            reachable,
            LAST_SEEN,
        )
        entry["relationId"] = relation
        entries.append(entry)
    return entries


def make_motion_sensor(
    relation="motion", name="Hall sensor", room="Entrance", detected=False, illuminance=7781
):
    """The 2 entries of 1 VALLHORN: the motion sensor, and a light sensor with no name."""
    motion_attrs = {
        "customName": name,
        "batteryPercentage": 97,
        "isOn": False,
        "isDetected": detected,
        "motionDetectedDelay": 20,
        "sensorConfig": {"onDuration": 180, "scheduleOn": False},
    }
    motion = _device(f"{relation}_1", "sensor", "motionSensor", motion_attrs, room, True, LAST_SEEN)
    light_attrs = {"customName": "", "illuminance": illuminance}
    light = _device(f"{relation}_3", "unknown", "lightSensor", light_attrs, None, True, LAST_SEEN)
    motion["relationId"] = light["relationId"] = relation
    return [motion, light]


def test_light_state_is_exported():
    count = ex.poll_once(FakeHub(others=[make_light()]), temp_offset=0.0)

    lamp = {"sensor": "Lamp", "room": "Bedroom"}
    assert count == 1
    assert value("ikea_light_on", **lamp) == 1
    assert value("ikea_light_level_percent", **lamp) == 40
    assert value("ikea_light_color_temperature_kelvin", **lamp) == 2702
    assert value("ikea_dirigera_device_reachable", **lamp) == 1
    assert value("ikea_dirigera_device_last_seen_timestamp_seconds", **lamp) == LAST_SEEN_TS


def test_light_that_is_off_keeps_its_level():
    ex.poll_once(FakeHub(others=[make_light(on=False, level=14)]), temp_offset=0.0)

    lamp = {"sensor": "Lamp", "room": "Bedroom"}
    assert value("ikea_light_on", **lamp) == 0
    assert value("ikea_light_level_percent", **lamp) == 14


def test_light_without_colour_temperature_publishes_none():
    light = make_light(kelvin=None)
    ex.poll_once(FakeHub(others=[light]), temp_offset=0.0)

    lamp = {"sensor": "Lamp", "room": "Bedroom"}
    assert value("ikea_light_on", **lamp) == 1
    assert value("ikea_light_color_temperature_kelvin", **lamp) is None


def test_unreachable_light_publishes_only_its_health():
    ex.poll_once(FakeHub(others=[make_light(reachable=False)]), temp_offset=0.0)

    lamp = {"sensor": "Lamp", "room": "Bedroom"}
    assert value("ikea_dirigera_device_reachable", **lamp) == 0
    assert value("ikea_light_on", **lamp) is None
    assert value("ikea_light_level_percent", **lamp) is None


def test_two_lights_with_one_name_are_on_if_either_is(caplog):
    lights = [make_light(light_id="a", on=False), make_light(light_id="b", on=True)]
    for order in (lights, lights[::-1]):
        ex.poll_once(FakeHub(others=order), temp_offset=0.0)
        assert value("ikea_light_on", sensor="Lamp", room="Bedroom") == 1
    assert "are both named 'Lamp' in room 'Bedroom'" in caplog.text


def test_remote_entries_share_one_battery_series(caplog):
    count = ex.poll_once(FakeHub(others=make_remote()), temp_offset=0.0)

    wheel = {"sensor": "Wheel", "room": "Bedroom"}
    assert count == 3
    assert value("ikea_dirigera_device_battery_percent", **wheel) == 86
    assert value("ikea_dirigera_device_reachable", **wheel) == 1
    # The entries without a name of their own do not get a series.
    unnamed = {"sensor": "BILRESA scroll wheel", "room": "unknown"}
    assert value("ikea_dirigera_device_battery_percent", **unnamed) is None
    assert "are both named" not in caplog.text


def test_unreachable_remote_publishes_no_battery():
    ex.poll_once(FakeHub(others=make_remote(reachable=False)), temp_offset=0.0)

    wheel = {"sensor": "Wheel", "room": "Bedroom"}
    assert value("ikea_dirigera_device_reachable", **wheel) == 0
    assert value("ikea_dirigera_device_battery_percent", **wheel) is None


def test_motion_sensor_and_its_light_sensor_share_name_and_room(caplog):
    count = ex.poll_once(FakeHub(others=make_motion_sensor(detected=True)), temp_offset=0.0)

    hall = {"sensor": "Hall sensor", "room": "Entrance"}
    assert count == 2
    assert value("ikea_motion_detected", **hall) == 1
    # 7781 is a Zigbee value: 10000 * log10(lux) + 1.
    assert value("ikea_illuminance_lux", **hall) == pytest.approx(6.0, abs=0.01)
    assert value("ikea_dirigera_device_battery_percent", **hall) == 97
    # The motion sensor has an "isOn" of its own. It is not a light.
    assert value("ikea_light_on", **hall) is None
    assert "are both named" not in caplog.text


def test_no_motion_is_published_as_zero():
    ex.poll_once(FakeHub(others=make_motion_sensor(detected=False)), temp_offset=0.0)

    assert value("ikea_motion_detected", sensor="Hall sensor", room="Entrance") == 0


def test_light_with_a_bad_value_is_skipped_and_the_rest_published(caplog):
    bad = make_light(name="Bad", level="bright")
    count = ex.poll_once(FakeHub([make_sensor()], others=[bad, make_light()]), temp_offset=0.0)

    assert count == 2
    assert value("ikea_light_on", sensor="Bad", room="Bedroom") is None
    assert value("ikea_dirigera_device_reachable", sensor="Bad", room="Bedroom") is None
    assert value("ikea_light_on", sensor="Lamp", room="Bedroom") == 1
    assert "skipped device light-1" in caplog.text


def test_light_that_goes_away_is_removed():
    ex.poll_once(FakeHub(others=[make_light()]), temp_offset=0.0)
    ex.poll_once(FakeHub(others=[]), temp_offset=0.0)

    assert value("ikea_light_on", sensor="Lamp", room="Bedroom") is None


def test_poll_log_counts_each_kind(caplog):
    caplog.set_level("INFO")
    others = [make_light(), *make_remote(), *make_motion_sensor()]
    ex.poll_once(FakeHub([make_sensor()], [make_purifier()], others=others), temp_offset=0.0)

    assert (
        "polled 1 environment sensor(s), 1 air purifier(s), 1 light(s), 3 remote(s), "
        "1 motion sensor(s), 1 light sensor(s)" in caplog.text
    )


def test_duplicate_name_is_logged_once_until_it_changes(caplog):
    lights = [make_light(light_id="a"), make_light(light_id="b")]
    for _ in range(3):
        ex.poll_once(FakeHub(others=lights), temp_offset=0.0)
    assert caplog.text.count("are both named 'Lamp'") == 1

    # The duplicate goes away and comes back: that is news again.
    ex.poll_once(FakeHub(others=lights[:1]), temp_offset=0.0)
    ex.poll_once(FakeHub(others=lights), temp_offset=0.0)
    assert caplog.text.count("are both named 'Lamp'") == 2


@pytest.mark.parametrize(("measured", "lux"), [(0, 0.0), (1, 1.0), (10001, 10.0), (30001, 1000.0)])
def test_illuminance_is_converted_from_the_zigbee_value(measured, lux):
    ex.poll_once(FakeHub(others=make_motion_sensor(illuminance=measured)), temp_offset=0.0)

    hall = {"sensor": "Hall sensor", "room": "Entrance"}
    assert value("ikea_illuminance_lux", **hall) == pytest.approx(lux)


# --- the event stream ---
# The event shapes below are those of a live hub.


def state_changed(device_id, attributes=None, **data):
    """A deviceStateChanged message, as the hub sends it."""
    payload = {"id": device_id, **data}
    if attributes is not None:
        payload["attributes"] = attributes
    return json.dumps(
        {
            "id": "event-1",
            "source": "urn:lpgera:dirigera",
            "specversion": "1.1.0",
            "time": "2026-11-17T13:09:16.000Z",
            "type": "deviceStateChanged",
            "data": payload,
        }
    )


def remote_press(device_id, pattern="singlePress"):
    data = {"id": device_id, "clickPattern": pattern}
    return json.dumps({"id": "event-2", "type": "remotePressEvent", "data": data})


HALL = {"sensor": "Hall sensor", "room": "Entrance"}
LAMP = {"sensor": "Lamp", "room": "Bedroom"}
WHEEL = {"sensor": "Wheel", "room": "Bedroom"}


def test_event_updates_a_light_between_polls():
    hub = FakeHub(others=[make_light(on=False, level=40)])
    ex.poll_once(hub, temp_offset=0.0)

    ex.handle_event(hub, state_changed("light-1", {"isOn": True}), 0.0)
    assert value("ikea_light_on", **LAMP) == 1
    # An event carries only what changed. The rest stays.
    assert value("ikea_light_level_percent", **LAMP) == 40

    ex.handle_event(hub, state_changed("light-1", {"lightLevel": 7}), 0.0)
    assert value("ikea_light_level_percent", **LAMP) == 7
    assert value("ikea_dirigera_events_total", type="deviceStateChanged") == 2
    # The exporter did not ask the hub again.
    assert hub.requests == ["/devices"]


def test_event_without_attributes_changes_reachability():
    hub = FakeHub(others=[make_light()])
    ex.poll_once(hub, temp_offset=0.0)

    ex.handle_event(hub, state_changed("light-1", isReachable=False), 0.0)
    assert value("ikea_dirigera_device_reachable", **LAMP) == 0
    assert value("ikea_light_on", **LAMP) is None

    ex.handle_event(hub, state_changed("light-1", isReachable=True), 0.0)
    assert value("ikea_dirigera_device_reachable", **LAMP) == 1
    assert value("ikea_light_on", **LAMP) == 1


def test_motion_event_is_counted_and_timed(monkeypatch):
    monkeypatch.setattr(ex.time, "time", lambda: 1_800_000_000.0)
    hub = FakeHub(others=make_motion_sensor(detected=False))
    ex.poll_once(hub, temp_offset=0.0)
    # The counter is there at 0 before the first motion, so increase() sees the first step.
    assert value("ikea_motion_detections_total", **HALL) == 0
    assert value("ikea_motion_last_detected_timestamp_seconds", **HALL) is None

    ex.handle_event(hub, state_changed("motion_1", {"isDetected": True}), 0.0)
    assert value("ikea_motion_detected", **HALL) == 1
    assert value("ikea_motion_detections_total", **HALL) == 1
    assert value("ikea_motion_last_detected_timestamp_seconds", **HALL) == 1_800_000_000.0

    # The hub repeats the state: that is not a new motion.
    ex.handle_event(hub, state_changed("motion_1", {"isDetected": True}), 0.0)
    assert value("ikea_motion_detections_total", **HALL) == 1

    ex.handle_event(hub, state_changed("motion_1", {"isDetected": False}), 0.0)
    ex.handle_event(hub, state_changed("motion_1", {"isDetected": True}), 0.0)
    assert value("ikea_motion_detections_total", **HALL) == 2


def test_motion_seen_by_a_poll_and_an_event_counts_once():
    quiet, moving = make_motion_sensor(detected=False), make_motion_sensor(detected=True)
    hub = FakeHub(others=quiet)
    ex.poll_once(hub, temp_offset=0.0)

    ex.handle_event(hub, state_changed("motion_1", {"isDetected": True}), 0.0)
    hub.devices = moving
    ex.poll_once(hub, temp_offset=0.0)
    assert value("ikea_motion_detections_total", **HALL) == 1


def test_motion_between_polls_counts_without_the_event_stream():
    hub = FakeHub(others=make_motion_sensor(detected=False))
    ex.poll_once(hub, temp_offset=0.0)
    hub.devices = make_motion_sensor(detected=True)
    ex.poll_once(hub, temp_offset=0.0)

    assert value("ikea_motion_detections_total", **HALL) == 1
    assert value("ikea_motion_last_detected_timestamp_seconds", **HALL) is not None


def test_motion_at_the_first_poll_is_timed_but_not_counted():
    ex.poll_once(FakeHub(others=make_motion_sensor(detected=True)), temp_offset=0.0)

    assert value("ikea_motion_detections_total", **HALL) == 0
    assert value("ikea_motion_last_detected_timestamp_seconds", **HALL) is not None


def test_last_motion_time_stays_after_the_motion_ends():
    hub = FakeHub(others=make_motion_sensor(detected=False))
    ex.poll_once(hub, temp_offset=0.0)
    ex.handle_event(hub, state_changed("motion_1", {"isDetected": True}), 0.0)
    seen = value("ikea_motion_last_detected_timestamp_seconds", **HALL)

    ex.handle_event(hub, state_changed("motion_1", {"isDetected": False}), 0.0)
    ex.poll_once(hub, temp_offset=0.0)
    assert value("ikea_motion_detected", **HALL) == 0
    assert value("ikea_motion_last_detected_timestamp_seconds", **HALL) == seen


def test_illuminance_event_is_converted_to_lux():
    hub = FakeHub(others=make_motion_sensor())
    ex.poll_once(hub, temp_offset=0.0)

    # The light sensor has no name of its own: the event still lands on its sibling.
    ex.handle_event(hub, state_changed("motion_3", {"illuminance": 10001}), 0.0)
    assert value("ikea_illuminance_lux", **HALL) == pytest.approx(10.0)


def test_remote_press_is_counted_by_pattern():
    hub = FakeHub(others=make_remote())
    ex.poll_once(hub, temp_offset=0.0)
    assert value("ikea_remote_presses_total", pattern="singlePress", **WHEEL) == 0

    # A press comes from any of the 3 entries of the wheel.
    ex.handle_event(hub, remote_press("wheel_3"), 0.0)
    ex.handle_event(hub, remote_press("wheel_6"), 0.0)
    ex.handle_event(hub, remote_press("wheel_3", "longPress"), 0.0)

    assert value("ikea_remote_presses_total", pattern="singlePress", **WHEEL) == 2
    assert value("ikea_remote_presses_total", pattern="longPress", **WHEEL) == 1
    assert value("ikea_dirigera_events_total", type="remotePressEvent") == 3


def test_event_for_an_unknown_device_is_ignored():
    hub = FakeHub(others=[make_light()])
    ex.poll_once(hub, temp_offset=0.0)

    ex.handle_event(hub, state_changed("light-99", {"isOn": False}), 0.0)
    ex.handle_event(hub, remote_press("wheel-99"), 0.0)
    assert value("ikea_light_on", **LAMP) == 1
    assert value("ikea_dirigera_events_total", type="deviceStateChanged") == 1


def test_event_before_the_first_poll_is_ignored():
    ex.handle_event(FakeHub(), state_changed("light-1", {"isOn": True}), 0.0)
    assert value("ikea_light_on", **LAMP) is None


@pytest.mark.parametrize(
    "message", ["not json", "[]", "{}", "42", '{"type": 7}', '{"type": "x", "data": []}', b"\xff"]
)
def test_bad_event_is_dropped_and_the_values_stay(message):
    hub = FakeHub(others=[make_light()])
    ex.poll_once(hub, temp_offset=0.0)

    ex.handle_event(hub, message, 0.0)
    assert value("ikea_light_on", **LAMP) == 1


def test_event_with_a_bad_value_does_not_remove_the_other_devices(caplog):
    hub = FakeHub([make_sensor()], others=[make_light()])
    ex.poll_once(hub, temp_offset=0.0)
    caplog.clear()

    ex.handle_event(hub, state_changed("light-1", {"lightLevel": "bright"}), 0.0)
    assert value("ikea_air_temperature_celsius", **LIVING) == 21.0
    # The light drops out until the next poll corrects it. The rebuild is quiet.
    assert value("ikea_light_on", **LAMP) is None
    assert "skipped device" not in caplog.text

    ex.poll_once(hub, temp_offset=0.0)
    assert value("ikea_light_on", **LAMP) == 1


def test_event_after_the_values_went_stale_does_not_publish_old_values(monkeypatch):
    clock = [1000.0]
    monkeypatch.setattr(ex.time, "monotonic", lambda: clock[0])
    hub = FakeHub(others=[make_light()])
    ex.poll_once(hub, temp_offset=0.0)

    hub.error = RuntimeError("hub down")
    clock[0] += 91
    ex.poll_once(hub, temp_offset=0.0, stale_after=90.0)
    assert value("ikea_light_on", **LAMP) is None

    ex.handle_event(hub, state_changed("light-1", {"isOn": False}), 0.0)
    assert value("ikea_light_on", **LAMP) is None


def test_event_does_not_repeat_the_duplicate_name_warning(caplog):
    hub = FakeHub(others=[make_light(light_id="a"), make_light(light_id="b")])
    ex.poll_once(hub, temp_offset=0.0)
    ex.handle_event(hub, state_changed("a", {"isOn": False}), 0.0)
    ex.poll_once(hub, temp_offset=0.0)

    assert caplog.text.count("are both named 'Lamp'") == 1


def test_event_stream_can_be_switched_off(monkeypatch):
    monkeypatch.setenv("EVENT_STREAM", "false")
    assert ex.Config().event_stream is False
    monkeypatch.delenv("EVENT_STREAM")
    assert ex.Config().event_stream is True
