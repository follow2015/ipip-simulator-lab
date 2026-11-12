"""Regression coverage for device-info enrichment."""
import copy
import pytest
from idc_simulator.oid import OidProvider, oid_tuple
from idc_simulator.cli import CliSession, parametric_query
from idc_simulator.notifications import event_varbinds
from conftest import LocalClient

NOW = 1_000_000_000


def test_standard_and_vendor_private_oids_present(controller):
    snap = controller.snapshot("sw-a")
    values = OidProvider(snap, NOW).values
    assert oid_tuple("1.3.6.1.2.1.99.1.1.1.1.100") in values
    assert oid_tuple("1.3.6.1.2.1.99.1.1.1.4.100") in values
    assert oid_tuple("1.3.6.1.2.1.99.1.1.1.6.100") in values
    assert oid_tuple("1.3.6.1.2.1.25.1.1.0") in values
    assert oid_tuple("1.3.6.1.2.1.25.2.3.1.6.1") in values
    assert oid_tuple("1.3.6.1.2.1.25.3.3.1.2.1") in values
    assert oid_tuple("1.3.6.1.2.1.47.1.1.1.1.11.1") in values
    private = oid_tuple("1.3.6.1.4.1.9.9.13.1.3.1.3.1")
    assert private in values and str(values[private]) == "35"


@pytest.mark.asyncio
async def test_vendor_style_cli_output_is_not_json(controller):
    session = CliSession(LocalClient(controller))
    assert session.family == "ios"
    vlan = await session.execute("show vlan")
    assert "VLAN" in vlan and "{" not in vlan
    assert "CPU utilization" in await session.execute("show processes cpu")
    assert "Memory Using Percentage" in await session.execute("show memory")
    brief = await session.execute("show ip interface brief")
    assert "IP-Address" in brief and "Protocol" in brief
    assert "PID:" in await session.execute("show inventory")
    assert "VLAN" in await session.execute("show vlan id 100")
    assert "Processor board ID" in await session.execute("show version")
    assert "interface" in await session.execute("show running-config")


@pytest.mark.asyncio
async def test_route_table_uses_the_vendors_interface_spelling(controller):
    session = CliSession(LocalClient(controller))
    text = await session.execute("show ip route")
    assert "Vlanif" not in text
    assert "Vlan100" in text
    from idc_simulator.cli import render_routes
    device = {"interfaces": {}, "l3_interfaces": [{"vlan": 100, "address": "192.0.2.1/24", "vrf": "default"}],
              "routes": [{"prefix": "10.0.0.0/8", "next_hop": "192.0.2.254", "vrf": "default"}]}
    for family, expected in (("ios", "Vlan100"), ("vrp", "Vlanif100"), ("comware", "Vlan-interface100"),
                             ("junos", "irb.100")):
        routes = render_routes(device, family)
        direct, static = routes.strip().splitlines()[-2:]
        assert direct.endswith(expected) and static.endswith(expected), (family, routes)


def test_running_port_render():
    from idc_simulator.cli import render_running_port
    device = {"interfaces": {"p1": {"name": "GigabitEthernet1/0/1", "description": "uplink",
                                    "mtu": 1500, "admin": True, "mode": "switchport",
                                    "pvid": 100, "allowed_vlans": [100]}}}
    text = render_running_port(device, "ios", "GigabitEthernet0/0/1", lambda name: "p1")
    assert "interface GigabitEthernet1/0/1" in text
    assert "switchport access vlan 100" in text


def test_parametric_query_resolution():
    cmd = "display current-configuration interface GigabitEthernet0/0/1"
    assert parametric_query(cmd, cmd) == ("running_port", "GigabitEthernet0/0/1")
    assert parametric_query("show vlan id 100", "show vlan id 100") == ("vlans", "100")
    assert parametric_query("display vlan 100", "display vlan 100") == ("vlans", "100")
    assert parametric_query("show processes cpu", "show processes cpu") == (None, None)


def test_sensor_alarm_varbinds_use_vendor_trap_oid(controller):
    snap = controller.snapshot("sw-a")
    event = {"type": "sensorAlarm", "time_ns": NOW,
             "payload": {"sensor": "temperature", "value": 85}}
    binds = event_varbinds(event, snap)
    assert binds is not None
    bound = dict(binds)
    assert str(bound[oid_tuple("1.3.6.1.6.3.1.1.4.1.0")]) == "1.3.6.1.4.1.9.9.91.2.0.1"
    assert oid_tuple("1.3.6.1.2.1.99.1.1.1.4.100") in bound


def test_unmapped_event_is_dropped(controller):
    snap = controller.snapshot("sw-a")
    assert event_varbinds({"type": "cardInserted", "time_ns": NOW, "payload": {}}, snap) is None


def _with_profile(controller, profile):
    snap = copy.deepcopy(controller.snapshot("sw-a"))
    snap["device"]["profile"] = profile
    return snap


def test_vendor_private_gap_oids_have_real_values(controller):
    values = OidProvider(controller.snapshot("sw-a"), NOW).values
    assert str(values[oid_tuple("1.3.6.1.4.1.9.9.109.1.1.1.1.6.1")]) == "12"
    assert str(values[oid_tuple("1.3.6.1.4.1.9.9.48.1.1.1.5.1")]) == "30"
    for oid in ("1.3.6.1.4.1.9.9.13.1.3.1.2.1",
                "1.3.6.1.4.1.9.9.13.1.4.1.2.1",
                "1.3.6.1.4.1.9.9.13.1.5.1.2.1"):
        assert str(values[oid_tuple(oid)]) == "1"


def test_state_oid_tracks_sensor_status(controller):
    snap = copy.deepcopy(controller.snapshot("sw-a"))
    for sensor in snap["device"]["sensors"]:
        if sensor["id"] == "fan1":
            sensor["status"] = 2
    values = OidProvider(snap, NOW).values
    assert str(values[oid_tuple("1.3.6.1.4.1.9.9.13.1.4.1.2.1")]) == "2"
    assert str(values[oid_tuple("1.3.6.1.2.1.99.1.1.1.5.101")]) == "2"


def test_huawei_h3c_private_gap_oids(controller):
    hw = OidProvider(_with_profile(controller, "huawei/vrp/s5735"), NOW).values
    assert str(hw[oid_tuple("1.3.6.1.4.1.2011.5.25.31.1.1.10.1.7.1")]) == "1"
    h3c = OidProvider(_with_profile(controller, "h3c/comware/s5560x"), NOW).values
    assert str(h3c[oid_tuple("1.3.6.1.4.1.25506.2.6.1.1.1.1.6.1")]) == "12"
    assert str(h3c[oid_tuple("1.3.6.1.4.1.25506.2.6.1.1.1.1.8.1")]) == "30"
    assert str(h3c[oid_tuple("1.3.6.1.4.1.25506.2.6.1.1.1.1.19.1")]) == "1"


def test_dot3_duplex_status_present(controller):
    values = OidProvider(controller.snapshot("sw-a"), NOW).values
    assert str(values[oid_tuple("1.3.6.1.2.1.10.7.2.1.19.10")]) == "3"
