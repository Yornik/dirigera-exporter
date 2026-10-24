"""Unit tests for the DIRIGERA exporter.

All tests run against a fake hub that serves raw ``/devices`` JSON, which goes
through the dirigera library's real device parsers. No live hub is required.
"""

from __future__ import annotations

import datetime

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
    for gauge in ex.DEVICE_GAUGES:
        gauge.clear()
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


def test_other_device_types_are_ignored():
    light = _device("light-1", "light", "light", {"customName": "Lamp"}, "Bedroom", True, LAST_SEEN)
    hub = FakeHub([make_sensor()], others=[light])
    count = ex.poll_once(hub, temp_offset=0.0)

    assert count == 1
    assert value("ikea_dirigera_device_reachable", sensor="Lamp", room="Bedroom") is None


def test_failure_keeps_values_briefly_then_removes_them(monkeypatch):
    clock = [1000.0]
    monkeypatch.setattr(ex.time, "time", lambda: clock[0])
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
