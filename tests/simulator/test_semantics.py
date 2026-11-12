import pytest
from pysnmp.proto import rfc1902, rfc1905
from idc_simulator.errors import SimulationError, Conflict
from idc_simulator.control import Controller
from idc_simulator.model import normalize_lab
from idc_simulator.oid import OidProvider, IFTABLE, IFX
from idc_simulator.cli import CliSession
from idc_simulator.tools import generate_lab
from conftest import LocalClient


def modify(core, operations, dev="sw-a"):
    return core.transact(dev, operations, core.locate(dev)[1]["revision"])


def test_transaction_atomic_conflicts_and_idempotency(controller):
    revision = controller.snapshot("sw-a")["revision"]
    with pytest.raises(SimulationError):
        controller.transact("sw-a", [{"kind": "system", "fields": {"hostname": "changed"}}, {"kind": "interface", "port": "p1", "fields": {"pvid": 999}}], revision)
    assert controller.snapshot("sw-a")["device"]["hostname"] == "IDC-DEMO"
    result = controller.transact("sw-a", [{"kind": "system", "fields": {"hostname": "changed"}}], revision, "once")
    assert controller.transact("sw-a", [{"kind":"system","fields":{"hostname":"changed"}}], revision, "once") == result
    with pytest.raises(Conflict):
        controller.transact("sw-a", [], revision, "once")
    with pytest.raises(Conflict):
        controller.transact("sw-a", [], revision)


def test_time_integral_rate_transition_down_and_reset(controller, clock):
    clock.advance(2)
    c = controller.snapshot("sw-a")["runtime"]["counters"]["p1"]
    assert c["in_bytes"] == 2_000_000
    assert c["in_packets"] == 2000
    modify(controller, [{"kind": "flow", "id": "demo", "rate_bps": 4_000_000}])
    clock.advance(3)
    assert controller.snapshot("sw-a")["runtime"]["counters"]["p1"]["in_bytes"] == 3_500_000
    modify(controller, [{"kind": "interface", "port": "p1", "fields": {"admin": False}}])
    clock.advance(3)
    assert controller.snapshot("sw-a")["runtime"]["counters"]["p1"]["in_bytes"] == 3_500_000
    modify(controller, [{"kind": "counter_reset"}])
    c = controller.snapshot("sw-a")["runtime"]["counters"]["p1"]
    assert c["in_bytes"] == 0 and c["discontinuity"] == 800


def test_aging_arp_scope_and_host_move(tmp_path, clock):
    lab = generate_lab(3, 4)
    lab["defaults"] = {"mac_aging_seconds": 3, "arp_aging_seconds": 3, "lldp": {"failure_policy": "ttl", "ttl_seconds": 2}}
    core = Controller(tmp_path/"other.db", clock)
    try:
        core.create(lab)
        assert len(core.snapshot("sw-001")["tables"]["arp"]) == 3
        assert not core.snapshot("sw-002")["tables"]["arp"]
        snap = core.snapshot("sw-001")
        assert snap["tables"]["lldp"][0]["remote_device"] == "sw-002"
        modify(core, [{"kind": "interface", "port": "p4", "fields": {"admin": False}}], "sw-001")
        assert core.snapshot("sw-002")["device"]["interfaces"]["p3"]["admin"] is True
        assert core.snapshot("sw-002")["device"]["interfaces"]["p3"]["oper"] is False
        assert core.snapshot("sw-001")["tables"]["lldp"]
        clock.advance(4)
        assert not core.snapshot("sw-001")["tables"]["lldp"]
        assert len(core.snapshot("sw-001")["tables"]["fdb"]) == 1
        modify(core, [{"kind": "host", "id": "host-1", "fields": {"interface": "p2"}}], "sw-001")
        assert core.snapshot("sw-001")["tables"]["fdb"][0]["bridge_port"] == 2
    finally:
        core.store.close()


def test_save_reboot_and_process_restore(controller, clock, tmp_path):
    identity = controller.snapshot("sw-a")["runtime"]["engine_id"]
    modify(controller, [{"kind": "system", "fields": {"hostname": "saved"}}, {"kind": "save"}])
    modify(controller, [{"kind": "system", "fields": {"hostname": "unsaved"}}])
    modify(controller, [{"kind": "reboot"}])
    snap = controller.snapshot("sw-a")
    assert snap["device"]["hostname"] == "saved"
    assert snap["runtime"]["engine_id"] == identity
    assert snap["runtime"]["engine_boots"] == 1
    restored = Controller(tmp_path/"state.db", clock)
    try:
        assert restored.snapshot("sw-a")["runtime"]["engine_id"] == identity
        assert restored.snapshot("sw-a")["device"]["hostname"] == "saved"
    finally:
        restored.store.close()


def test_numeric_oid_walk_types_and_indexes(controller, clock):
    provider = OidProvider(controller.snapshot("sw-a"), clock())
    assert provider.index == tuple(sorted(set(provider.index)))
    assert isinstance(provider.get(IFX+(6, 10)), rfc1902.Counter64)
    assert isinstance(provider.get(IFTABLE+(10, 10)), rfc1902.Counter32)
    assert int(provider.get((1,3,6,1,2,1,17,1,4,1,2,1))) == 10
    assert isinstance(provider.get((9,9,9)), rfc1905.NoSuchObject)
    assert isinstance(provider.get(IFTABLE+(7, 999)), rfc1905.NoSuchInstance)
    assert isinstance(provider.get((1,0,8802,1,1,2,1,4,1,1,9,0,1,1)), rfc1905.NoSuchInstance)
    assert isinstance(provider.get_next(provider.index[-1])[1], rfc1905.EndOfMibView)
    current = ()
    count = 0
    while True:
        nxt, value = provider.get_next(current)
        if isinstance(value, rfc1905.EndOfMibView):
            break
        assert nxt > current
        current = nxt; count += 1
    assert count == len(provider.index)


@pytest.mark.parametrize("problem", ["duplicate-ip", "duplicate-index", "vlan", "loop", "speed"])
def test_validation_rejects_ambiguous_topology(problem):
    lab = generate_lab(3, 4)
    if problem == "duplicate-ip":
        lab["devices"][1]["management_ip"] = lab["devices"][0]["management_ip"]
    elif problem == "duplicate-index":
        lab["devices"][0]["interfaces"][1]["ifindex"] = 1
    elif problem == "vlan":
        lab["devices"][0]["interfaces"][0]["pvid"] = 200
    elif problem == "loop":
        lab["links"].append({"id": "cycle", "endpoints": ["sw-001:p2", "sw-003:p4"], "allowed_vlans": [100]})
    else:
        lab["devices"][0]["interfaces"][0]["speed_bps"] = 0
    with pytest.raises(SimulationError):
        normalize_lab(lab)


@pytest.mark.parametrize("profile,enter,enable,disable,save", [
    ("cisco/ios/c9300", "configure terminal", "no shutdown", "shutdown", "write memory"),
    ("arista/eos/demo", "configure terminal", "no shutdown", "shutdown", "write memory"),
    ("cisco/nxos/demo", "configure terminal", "no shutdown", "shutdown", "write memory"),
    ("huawei/vrp/s5735", "system-view", "undo shutdown", "shutdown", "save force"),
    ("h3c/comware/s5560x", "system-view", "undo shutdown", "shutdown", "save force")])
async def test_cli_immediate_configuration(controller, profile, enter, enable, disable, save):
    controller.labs["local-demo"]["lab"]["devices"]["sw-a"]["profile"] = profile
    session = CliSession(LocalClient(controller))
    assert "Error:" not in await session.execute(enter)
    assert "Error:" not in await session.execute("interface GigabitEthernet1/0/1")
    assert "Error:" not in await session.execute(disable)
    assert not controller.snapshot("sw-a")["device"]["interfaces"]["p1"]["admin"]
    assert "Error:" not in await session.execute(enable)
    assert "Error:" not in await session.execute("description test uplink")
    assert "Error:" not in await session.execute(save)
    assert "test uplink" in await session.execute("display saved-configuration" if profile.startswith(("h3c", "huawei")) else "show startup-config")
    assert "Error:" in await session.execute("unsupported shell; rm -rf /tmp/anything")


async def test_junos_candidate_commit_and_conflict(controller):
    controller.labs["local-demo"]["lab"]["devices"]["sw-a"]["profile"] = "juniper/junos/demo"
    session = CliSession(LocalClient(controller))
    await session.execute("configure")
    await session.execute("set interfaces GigabitEthernet1/0/1 disable")
    assert controller.snapshot("sw-a")["device"]["interfaces"]["p1"]["admin"]
    assert "disable" in await session.execute("show configuration")
    assert await session.execute("commit") == "commit complete"
    assert not controller.snapshot("sw-a")["device"]["interfaces"]["p1"]["admin"]
    await session.execute("set system host-name candidate")
    modify(controller, [{"kind": "system", "fields": {"hostname": "concurrent"}}])
    assert "Revision conflict" in await session.execute("commit")
    assert controller.snapshot("sw-a")["device"]["hostname"] == "concurrent"


async def test_fortios_transaction(controller):
    controller.labs["local-demo"]["lab"]["devices"]["sw-a"]["profile"] = "fortinet/fortios/demo"
    session = CliSession(LocalClient(controller))
    for command in ("config system interface", 'edit "GigabitEthernet1/0/1"', "set status down", 'set alias "WAN test"'):
        assert "Error:" not in await session.execute(command)
    assert controller.snapshot("sw-a")["device"]["interfaces"]["p1"]["admin"]
    assert "Error:" not in await session.execute("next")
    assert not controller.snapshot("sw-a")["device"]["interfaces"]["p1"]["admin"]


def test_threshold_hysteresis(controller):
    for value in (81, 82, 75, 69, 68):
        modify(controller, [{"kind": "sensor", "id": "temperature", "value": value}])
    events = [e["type"] for e in controller.store.events() if e["type"].startswith("sensor")]
    assert events == ["sensorAlarm", "sensorClear"]


def test_hundred_devices_complete_tables(tmp_path, clock):
    core = Controller(tmp_path/"hundred.db", clock)
    try:
        core.create(generate_lab())
        for dev in core.labs["idc-scale"]["lab"]["devices"]:
            s = core.snapshot(dev)
            assert len(s["device"]["interfaces"]) == 48
            assert len(s["tables"]["fdb"]) == 100
        assert len(core.snapshot("sw-001")["tables"]["arp"]) == 100
    finally:
        core.store.close()
