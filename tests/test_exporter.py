"""Unit tests for the DIRIGERA exporter.

All tests run against mocked hub/device objects — no live hub is required.
"""

from __future__ import annotations

import types

import pytest
from prometheus_client import REGISTRY

import dirigera_exporter as ex


def make_sensor(
    name="ALPSTUGA",
    room="Living room",
    temp=21.0,
    rh=45,
    co2=700,
    pm25=8,
    sensor_id="sensor-1",
):
    """Build a stand-in for dirigera's EnvironmentSensor."""
    attrs = types.SimpleNamespace(
        custom_name=name,
        current_temperature=temp,
        current_r_h=rh,
        current_c_o2=co2,
        current_p_m25=pm25,
    )
    room_obj = types.SimpleNamespace(name=room) if room is not None else None
    return types.SimpleNamespace(id=sensor_id, attributes=attrs, room=room_obj)


def make_purifier(
    name="STARKVIND",
    room="Living room",
    motor=20,
    pm25=12,
    filter_alarm=False,
    mode="auto",
    purifier_id="purifier-1",
):
    """Build a stand-in for dirigera's AirPurifier."""
    attrs = types.SimpleNamespace(
        custom_name=name,
        motor_state=motor,
        current_p_m25=pm25,
        filter_alarm_status=filter_alarm,
        fan_mode=types.SimpleNamespace(value=mode),
    )
    room_obj = types.SimpleNamespace(name=room) if room is not None else None
    return types.SimpleNamespace(id=purifier_id, attributes=attrs, room=room_obj)


class FakeHub:
    def __init__(self, sensors=None, purifiers=None, error=None):
        self._sensors = sensors or []
        self._purifiers = purifiers or []
        self._error = error

    def get_environment_sensors(self):
        if self._error is not None:
            raise self._error
        return self._sensors

    def get_air_purifiers(self):
        if self._error is not None:
            raise self._error
        return self._purifiers


@pytest.fixture(autouse=True)
def _reset_metrics():
    for gauge in (
        ex.TEMPERATURE,
        ex.HUMIDITY,
        ex.CO2,
        ex.PM25,
        ex.PURIFIER_RUNNING,
        ex.PURIFIER_MOTOR_SPEED,
        ex.PURIFIER_FAN_MODE,
        ex.PURIFIER_FILTER_ALARM,
        ex.PURIFIER_PM25,
    ):
        gauge.clear()
    ex.SCRAPE_SUCCESS.set(0)
    ex.LAST_SUCCESS.set(0)
    yield


def value(metric, **labels):
    return REGISTRY.get_sample_value(metric, labels)


def test_metric_mapping_and_success():
    hub = FakeHub([make_sensor()])
    count = ex.poll_once(hub, temp_offset=0.0)

    assert count == 1
    labels = {"sensor": "ALPSTUGA", "room": "Living room"}
    assert value("ikea_air_temperature_celsius", **labels) == 21.0
    assert value("ikea_air_humidity_percent", **labels) == 45
    assert value("ikea_air_co2_ppm", **labels) == 700
    assert value("ikea_air_pm25_micrograms_per_cubic_meter", **labels) == 8
    assert value("ikea_dirigera_scrape_success") == 1
    assert value("ikea_dirigera_last_success_timestamp_seconds") > 0


def test_temp_offset_applies_to_temperature_only():
    hub = FakeHub([make_sensor(temp=24.0, rh=50)])
    ex.poll_once(hub, temp_offset=-2.0)

    labels = {"sensor": "ALPSTUGA", "room": "Living room"}
    assert value("ikea_air_temperature_celsius", **labels) == 22.0
    # Humidity must be untouched by the temperature offset.
    assert value("ikea_air_humidity_percent", **labels) == 50


def test_scrape_failure_sets_success_zero():
    hub = FakeHub(error=RuntimeError("hub unreachable"))
    count = ex.poll_once(hub, temp_offset=0.0)

    assert count is None
    assert value("ikea_dirigera_scrape_success") == 0


def test_missing_field_is_not_emitted():
    hub = FakeHub([make_sensor(co2=None)])
    ex.poll_once(hub, temp_offset=0.0)

    labels = {"sensor": "ALPSTUGA", "room": "Living room"}
    # CO2 is absent -> no series at all (not a misleading 0).
    assert value("ikea_air_co2_ppm", **labels) is None
    assert value("ikea_air_temperature_celsius", **labels) == 21.0


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
    labels = {"sensor": "STARKVIND", "room": "Living room"}
    assert value("ikea_air_purifier_running", **labels) == 1
    assert value("ikea_air_purifier_motor_speed", **labels) == 20
    assert value("ikea_air_purifier_filter_alarm", **labels) == 0
    # The purifier's PM2.5 goes on its own metric, never the env-sensor µg/m³ one.
    assert value("ikea_air_purifier_pm25", **labels) == 12
    assert value("ikea_air_pm25_micrograms_per_cubic_meter", **labels) is None
    # Active fan mode is 1, every other mode is 0.
    assert (
        value("ikea_air_purifier_fan_mode", sensor="STARKVIND", room="Living room", mode="auto")
        == 1
    )
    assert (
        value("ikea_air_purifier_fan_mode", sensor="STARKVIND", room="Living room", mode="off") == 0
    )


def test_air_purifier_off_when_motor_zero():
    hub = FakeHub(purifiers=[make_purifier(motor=0)])
    ex.poll_once(hub, temp_offset=0.0)

    assert value("ikea_air_purifier_running", sensor="STARKVIND", room="Living room") == 0


def test_air_purifier_filter_alarm():
    hub = FakeHub(purifiers=[make_purifier(filter_alarm=True)])
    ex.poll_once(hub, temp_offset=0.0)

    assert value("ikea_air_purifier_filter_alarm", sensor="STARKVIND", room="Living room") == 1


def test_sensors_and_purifier_counted_together():
    hub = FakeHub(sensors=[make_sensor()], purifiers=[make_purifier()])
    count = ex.poll_once(hub, temp_offset=0.0)

    assert count == 2
    assert value("ikea_dirigera_scrape_success") == 1
