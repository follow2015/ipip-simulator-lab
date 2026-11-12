"""常见硬件/资源/端口告警的注入与通知内容。"""
import pytest

from idc_simulator.cli import CliSession
from idc_simulator.notifications import event_varbinds, RMON_RISING, RMON_FALLING, ENTITY_CHANGE
from idc_simulator.oid import OidProvider, oid_tuple
from conftest import LocalClient

NOW = 1_000_000_000
CPU_OID = "1.3.6.1.4.1.9.9.109.1.1.1.1.6.1"
MEMORY_OID = "1.3.6.1.4.1.9.9.48.1.1.1.5.1"


def transact(controller, operations, device="sw-a"):
    revision = controller.labs["local-demo"]["revision"]
    return controller.transact(device, operations, revision)


def events(controller, kind=None):
    stored = controller.store.events(after=0, limit=200)
    items = stored["events"] if isinstance(stored, dict) else stored
    return [e for e in items if kind is None or e["type"] == kind]


def varbinds(event, snapshot):
    return {f"{'.'.join(map(str, oid))}": value.prettyPrint() for oid, value in (event_varbinds(event, snapshot) or [])}



@pytest.mark.asyncio
async def test_fan_stop_reports_nonoperational_status(controller, clock):
    transact(controller, [{"kind": "sensor", "id": "fan1", "value": 0, "status": 3}])
    clock.advance(1)
    snapshot = controller.snapshot("sw-a")
    alarm = events(controller, "sensorAlarm")[-1]
    assert alarm["payload"] == {"sensor": "fan1", "value": 0, "status": 3, "direction": "falling"}
    vbs = varbinds(alarm, snapshot)
    assert vbs["1.3.6.1.2.1.99.1.1.1.4.101"] == "0"
    assert vbs["1.3.6.1.2.1.99.1.1.1.5.101"] == "3"
    values = OidProvider(snapshot, NOW).values
    assert str(values[oid_tuple("1.3.6.1.2.1.99.1.1.1.5.101")]) == "3"


@pytest.mark.asyncio
async def test_fan_low_threshold_without_status(controller, clock):
    fan = controller.labs["local-demo"]["lab"]["devices"]["sw-a"]["sensors"][1]
    fan.update({"low_threshold": 1000, "low_clear": 3000})
    transact(controller, [{"kind": "sensor", "id": "fan1", "value": 800}])
    assert events(controller, "sensorAlarm")
    transact(controller, [{"kind": "sensor", "id": "fan1", "value": 3500}])
    assert events(controller, "sensorClear")



@pytest.mark.asyncio
async def test_power_supply_loss_uses_truthvalue_sensor(controller, clock):
    transact(controller, [{"kind": "sensor", "id": "power1", "value": 0}])
    clock.advance(1)
    snapshot = controller.snapshot("sw-a")
    alarm = events(controller, "sensorAlarm")[-1]
    vbs = varbinds(alarm, snapshot)
    assert vbs["1.3.6.1.2.1.99.1.1.1.4.102"] == "0"
    assert vbs["1.3.6.1.2.1.99.1.1.1.1.102"] == "12"
    transact(controller, [{"kind": "sensor", "id": "power1", "value": 1}])
    assert events(controller, "sensorClear")



@pytest.mark.asyncio
async def test_temperature_alarm_and_clear_still_work(controller, clock):
    transact(controller, [{"kind": "sensor", "id": "temperature", "value": 95}])
    transact(controller, [{"kind": "sensor", "id": "temperature", "value": 60}])
    types = [e["type"] for e in events(controller) if e["type"].startswith("sensor")]
    assert types == ["sensorAlarm", "sensorClear"]



@pytest.mark.asyncio
async def test_cpu_alarm_uses_rmon_rising_alarm(controller, clock):
    transact(controller, [{"kind": "resources", "cpu_percent": 93}])
    clock.advance(1)
    snapshot = controller.snapshot("sw-a")
    alarm = events(controller, "resourceAlarm")[-1]
    assert alarm["payload"] == {"resource": "cpu", "value": 93, "threshold": 80}
    vbs = varbinds(alarm, snapshot)
    assert vbs["1.3.6.1.6.3.1.1.4.1.0"] == RMON_RISING
    assert vbs["1.3.6.1.2.1.16.3.1.3"] == CPU_OID
    assert vbs["1.3.6.1.2.1.16.3.1.4"] == "1"
    assert vbs["1.3.6.1.2.1.16.3.1.5"] == "93"
    assert vbs["1.3.6.1.2.1.16.3.1.6"] == "80"
    assert str(OidProvider(snapshot, NOW).values[oid_tuple(CPU_OID)]) == "93"


@pytest.mark.asyncio
async def test_memory_alarm_and_falling_alarm(controller, clock):
    transact(controller, [{"kind": "resources", "memory_percent": 90}])
    transact(controller, [{"kind": "resources", "memory_percent": 40}])
    alarm, clear = events(controller, "resourceAlarm")[-1], events(controller, "resourceClear")[-1]
    assert clear["payload"] == {"resource": "memory", "value": 40, "threshold": 85}
    snapshot = controller.snapshot("sw-a")
    assert varbinds(clear, snapshot)["1.3.6.1.6.3.1.1.4.1.0"] == RMON_FALLING
    assert varbinds(alarm, snapshot)["1.3.6.1.2.1.16.3.1.3"] == MEMORY_OID
    assert snapshot["device"]["resources"]["memory_used"] == 1048576*40//100


@pytest.mark.asyncio
async def test_resource_thresholds_can_be_declared_per_device(controller, clock):
    controller.labs["local-demo"]["lab"]["devices"]["sw-a"]["thresholds"] = {"cpu": 50}
    transact(controller, [{"kind": "resources", "cpu_percent": 60}])
    assert events(controller, "resourceAlarm")[-1]["payload"]["threshold"] == 50



@pytest.mark.asyncio
async def test_interface_errors_flow_into_snmp_and_cli(controller, clock):
    transact(controller, [{"kind": "errors", "port": "p1", "in_errors": 120, "in_discards": 7}])
    clock.advance(10)
    snapshot = controller.snapshot("sw-a")
    values = OidProvider(snapshot, NOW).values
    assert str(values[oid_tuple("1.3.6.1.2.1.2.2.1.14.10")]) == "1200"
    assert str(values[oid_tuple("1.3.6.1.2.1.2.2.1.13.10")]) == "70"
    session = CliSession(LocalClient(controller))
    detail = await session.execute("show interfaces Gi1/0/1")
    assert "1200 input errors" in detail and "70 ignored" in detail
    table = await session.execute("show interfaces counters")
    assert "1200" in table


@pytest.mark.asyncio
async def test_counter_reset_clears_error_and_discard_totals_and_remainders(controller, clock):
    transact(controller, [{"kind":"errors", "port":"p1", "in_errors":3, "out_errors":2,
                          "in_discards":5, "out_discards":4}])
    clock.advance(.5)
    before = controller.snapshot("sw-a")["runtime"]["counters"]["p1"]
    assert (before["in_errors"], before["out_errors"], before["in_discards"], before["out_discards"]) == (1,1,2,2)
    transact(controller, [{"kind":"counter_reset"}])
    reset = controller.snapshot("sw-a")["runtime"]["counters"]["p1"]
    assert all(reset[key] == 0 for key in ("in_errors", "out_errors", "in_discards", "out_discards"))
    assert all(reset[key+"_remainder"] == 0 for key in ("in_errors", "out_errors", "in_discards", "out_discards"))
    clock.advance(1)
    after = controller.snapshot("sw-a")["runtime"]["counters"]["p1"]
    assert (after["in_errors"], after["out_errors"], after["in_discards"], after["out_discards"]) == (3,2,5,4)


@pytest.mark.asyncio
async def test_interface_errors_alarm_and_clear(controller, clock):
    transact(controller, [{"kind": "errors", "port": "p1", "in_errors": 5}])
    alarm = events(controller, "interfaceErrors")[-1]
    assert alarm["payload"]["ifindex"] == 10 and alarm["payload"]["in_errors"] == 5
    vbs = varbinds(alarm, controller.snapshot("sw-a"))
    assert vbs["1.3.6.1.6.3.1.1.4.1.0"] == RMON_RISING
    assert vbs["1.3.6.1.2.1.16.3.1.3"] == "1.3.6.1.2.1.2.2.1.14.10"
    assert vbs["1.3.6.1.2.1.16.3.1.4"] == "2"
    transact(controller, [{"kind": "errors", "port": "p1"}])
    assert varbinds(events(controller, "interfaceErrorsClear")[-1], controller.snapshot("sw-a"))["1.3.6.1.6.3.1.1.4.1.0"] == RMON_FALLING


@pytest.mark.asyncio
async def test_errors_reject_unknown_port_and_negative_rates(controller, clock):
    with pytest.raises(Exception):
        transact(controller, [{"kind": "errors", "port": "nope", "in_errors": 1}])
    with pytest.raises(Exception):
        transact(controller, [{"kind": "errors", "port": "p1", "in_errors": -1}])



@pytest.mark.asyncio
async def test_card_removal_carries_entity_varbinds(controller, clock):
    card = {"id": "psu1", "index": 300, "name": "Power Supply 1", "class": 6, "present": True, "serial": "SIM-PSU-01"}
    controller.labs["local-demo"]["lab"]["devices"]["sw-a"]["cards"].append(card)
    transact(controller, [{"kind": "card", "id": "psu1", "present": False}])
    clock.advance(1)
    snapshot = controller.snapshot("sw-a")
    removed = events(controller, "cardRemoved")[-1]
    vbs = varbinds(removed, snapshot)
    assert vbs["1.3.6.1.6.3.1.1.4.1.0"] == ENTITY_CHANGE
    assert vbs["1.3.6.1.2.1.47.1.1.1.1.300"] == "300"
    assert vbs["1.3.6.1.2.1.47.1.1.1.5.300"] == "6"
    assert vbs["1.3.6.1.2.1.47.1.1.1.7.300"] == "Power Supply 1"



def test_injection_api_accepts_alarm_types_and_validates_payload(controller):
    lab = controller.labs["local-demo"]
    result = controller.inject_notifications("local-demo", ["sw-a"], 2, "sensorAlarm", {"sensor": "fan1", "value": 0, "status": 3})
    assert result["events"] == 2
    for kind, payload in (("resourceAlarm", {"resource": "cpu", "value": 95, "threshold": 80}),
                          ("interfaceErrors", {"ifindex": 10}),
                          ("cardRemoved", {"index": 300})):
        assert controller.inject_notifications("local-demo", ["sw-a"], 1, kind, payload)["events"] == 1
    with pytest.raises(Exception):
        controller.inject_notifications("local-demo", ["sw-a"], 1, "resourceAlarm", {"resource": "cpu"})
    with pytest.raises(Exception):
        controller.inject_notifications("local-demo", ["sw-a"], 1, "sensorAlarm", {})
    with pytest.raises(Exception):
        controller.inject_notifications("local-demo", ["sw-a"], 1, "madeUpAlarm", {})
    assert lab["revision"] >= 1


def test_injected_interface_errors_by_ifindex_only_emits(controller):
    result = controller.inject_notifications("local-demo", ["sw-a"], 1, "interfaceErrors", {"ifindex": 10})
    assert result["unmapped_devices"] == []
    vbs = varbinds(events(controller, "interfaceErrors")[-1], controller.snapshot("sw-a"))
    assert vbs["1.3.6.1.6.3.1.1.4.1.0"] == RMON_RISING
    assert vbs["1.3.6.1.2.1.16.3.1.3"] == "1.3.6.1.2.1.2.2.1.14.10"
    assert vbs["1.3.6.1.2.1.16.3.1.4"] == "2"
    assert vbs["1.3.6.1.2.1.16.3.1.5"] == "1"


def test_injected_interface_errors_unknown_ifindex_is_unmapped(controller):
    result = controller.inject_notifications("local-demo", ["sw-a"], 1, "interfaceErrors", {"ifindex": 999})
    assert result["unmapped_devices"] == ["sw-a"]
    assert event_varbinds(events(controller, "interfaceErrors")[-1], controller.snapshot("sw-a")) is None


def test_injection_reports_profiles_without_a_mapping(controller):
    device = controller.labs["local-demo"]["lab"]["devices"]["sw-a"]
    device["profile"] = "generic/ethernet/switch"
    assert controller.inject_notifications("local-demo", ["sw-a"], 1, "sensorAlarm", {"sensor": "fan1"})["unmapped_devices"] == []
    assert controller.inject_notifications("local-demo", ["sw-a"], 1, "sensorAlarm", {"sensor": "fan9"})["unmapped_devices"] == ["sw-a"]
    assert controller.inject_notifications("local-demo", ["sw-a"], 1, "coldStart", {})["unmapped_devices"] == []
    assert controller.inject_notifications("local-demo", ["sw-a"], 1, "resourceAlarm",
                                           {"resource": "cpu", "value": 90, "threshold": 80})["unmapped_devices"] == []



def test_generic_profile_falls_back_to_rmon_with_correct_direction(controller):
    controller.labs["local-demo"]["lab"]["devices"]["sw-a"]["profile"] = "generic/ethernet/switch"
    snapshot = controller.snapshot("sw-a")
    assert varbinds({"type": "sensorAlarm", "time_ns": NOW, "payload": {"sensor": "temperature", "value": 95, "direction": "rising"}},
                    snapshot)["1.3.6.1.6.3.1.1.4.1.0"] == "1.3.6.1.2.1.16.0.1"
    fan = varbinds({"type": "sensorAlarm", "time_ns": NOW, "payload": {"sensor": "fan1", "value": 0, "direction": "falling"}},
                   snapshot)
    assert fan["1.3.6.1.6.3.1.1.4.1.0"] == "1.3.6.1.2.1.16.0.2"
    assert fan["1.3.6.1.2.1.16.3.1.3"] == "1.3.6.1.2.1.99.1.1.1.4.101"
    assert fan["1.3.6.1.2.1.16.3.1.4"] == "1"


def test_vendor_profile_keeps_private_trap_oid(controller):
    snapshot = controller.snapshot("sw-a")
    vbs = varbinds({"type": "sensorAlarm", "time_ns": NOW, "payload": {"sensor": "temperature", "value": 95, "direction": "rising"}},
                   snapshot)
    assert vbs["1.3.6.1.6.3.1.1.4.1.0"] == "1.3.6.1.4.1.9.9.91.2.0.1"


def test_injected_alarm_still_maps_to_trap(controller):
    controller.inject_notifications("local-demo", ["sw-a"], 1, "sensorAlarm", {"sensor": "power1", "value": 0})
    snapshot = controller.snapshot("sw-a")
    injected = [e for e in events(controller, "sensorAlarm") if e.get("payload", {}).get("pressure")][-1]
    assert varbinds(injected, snapshot)["1.3.6.1.2.1.99.1.1.1.4.102"] == "0"



def test_invalid_thresholds_and_sensor_bounds_are_rejected(controller):
    from idc_simulator.model import validate_config
    device = controller.labs["local-demo"]["lab"]["devices"]["sw-a"]
    device["thresholds"] = {"disk": 90}
    with pytest.raises(Exception):
        validate_config(device)
    device["thresholds"] = {"cpu": 120}
    with pytest.raises(Exception):
        validate_config(device)
    device.pop("thresholds")
    device["sensors"][1].update({"low_threshold": 3000, "low_clear": 1000})
    with pytest.raises(Exception):
        validate_config(device)
