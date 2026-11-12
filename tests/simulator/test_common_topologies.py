import pytest
from idc_simulator.control import Controller
from idc_simulator.model import normalize_lab, mac_bytes
from idc_simulator.oid import OidProvider
from idc_simulator.tools import generate_lab
from idc_simulator.cli import CliSession
from conftest import LocalClient


def pair_lab():
    lab = generate_lab(2,48)
    lab["name"] = "p48-p48"
    lab["links"] = [{"id": "a48-b48", "endpoints": ["sw-001:p48", "sw-002:p48"], "mode": "trunk", "allowed_vlans": [100]}]
    return lab


def test_port48_pair_identity_lldp_and_vlan_learning(tmp_path,clock):
    core = Controller(tmp_path/"pair.db",clock)
    try:
        core.create(pair_lab())
        a,b = core.snapshot("sw-001"), core.snapshot("sw-002")
        pa,pb = a["device"]["interfaces"]["p48"],b["device"]["interfaces"]["p48"]
        assert pa["mac"] != pb["mac"] and pa["mac"] != a["device"]["chassis_mac"]
        assert OidProvider(a,clock()).get("1.3.6.1.2.1.2.2.1.6.48").asOctets() == mac_bytes(pa["mac"])
        assert a["tables"]["lldp"][0]["remote_ifindex"] == 48
        assert b["tables"]["lldp"][0]["remote_ifindex"] == 48
        assert a["tables"]["lldp"][0]["chassis_mac"] == b["device"]["chassis_mac"]
        assert a["tables"]["lldp"][0]["remote_port_mac"] == pb["mac"]
        remote_fdb = next(f for f in a["tables"]["fdb"] if f["mac"] == pair_lab()["hosts"][1]["mac"])
        assert remote_fdb["ifindex"] == 48
        assert core.topology("p48-p48")["links"][0]["lldp_discovered"] == [True,True]
        core.transact("sw-001",[{"kind":"interface","port":"p48","fields":{"allowed_vlans":[ ]}}],a["revision"])
        assert not core.snapshot("sw-001")["tables"]["flows"][0]["reachable"]
        assert core.snapshot("sw-001")["tables"]["lldp"]
    finally:
        core.store.close()


def lag_lab():
    lab = pair_lab()
    lab["name"] = "eth-trunk"
    for d in lab["devices"]:
        d["interfaces"].append({"id":"trunk1","name":"Eth-Trunk1","ifindex":100,"members":["p47","p48"],"speed_bps":2000000000,"type":161})
    lab["links"] = [{"id":"bundle","endpoints":["sw-001:trunk1","sw-002:trunk1"],"mode":"trunk","allowed_vlans":[100],"lldp_scope":"members"}]
    return lab


def test_eth_trunk_paired_members_and_partial_failure(tmp_path,clock):
    core = Controller(tmp_path/"lag.db",clock)
    try:
        core.create(lag_lab())
        a = core.snapshot("sw-001")
        assert {n["ifindex"] for n in a["tables"]["lldp"]} == {47,48}
        assert core.topology("eth-trunk")["links"][0]["lldp_discovered"] == [True,True]
        core.transact("sw-001",[{"kind":"interface","port":"p47","fields":{"admin":False}}],a["revision"])
        b = core.snapshot("sw-002")
        assert b["device"]["interfaces"]["p47"]["admin"] and not b["device"]["interfaces"]["p47"]["oper"]
        assert b["device"]["interfaces"]["trunk1"]["oper"]
        assert b["tables"]["flows"][0]["reachable"]
        core.transact("sw-002",[{"kind":"interface","port":"p48","fields":{"admin":False}}],b["revision"])
        assert not core.snapshot("sw-001")["device"]["interfaces"]["trunk1"]["oper"]
        assert not core.snapshot("sw-001")["tables"]["flows"][0]["reachable"]
    finally:
        core.store.close()


@pytest.mark.parametrize("layout",["tree","ring","mixed"])
def test_hundred_device_common_topologies(tmp_path,clock,layout):
    lab = generate_lab(100,48,layout=layout)
    core = Controller(tmp_path/(layout+".db"),clock)
    try:
        core.create(lab)
        topology = core.topology(lab["name"])
        assert len(topology["devices"]) == 100
        assert all(link["oper"] for link in topology["links"])
        assert all(link["lldp_discovered"] == [True,True] for link in topology["links"])
        assert core.snapshot(lab["hosts"][0]["device"])["tables"]["flows"][0]["reachable"]
        if layout == "mixed":
            assert sum(l["mode"] == "routed" for l in topology["links"]) == 20
            core_device = core.snapshot("sw-001")
            for arp in core_device["tables"]["arp"]:
                assert arp["origin"] == "routed-neighbor"
                assert arp["mac"] in {p["mac"] for d in topology["devices"] for p in d["interfaces"].values()}
    finally:
        core.store.close()


async def test_svi_and_routed_port_cli(controller):
    session = CliSession(LocalClient(controller))
    for command in ("configure terminal","interface Vlan100","ip address 192.0.2.254/24","interface GigabitEthernet1/0/2","no switchport","ip address 203.0.113.1/30"):
        assert "Error:" not in await session.execute(command)
    d = controller.snapshot("sw-a")["device"]
    assert d["interfaces"]["p2"]["mode"] == "routed"
    assert next(l for l in d["l3_interfaces"] if l["id"] == "p2")["interface"] == "p2"
    assert next(l for l in d["l3_interfaces"] if l["id"] == "vlan100")["address"] == "192.0.2.254/24"
    for command in ("interface Vlan100","shutdown","exit"):
        assert "Error:" not in await session.execute(command)
    assert session.mode == "config" and session.l3_vlan is None
    d = controller.snapshot("sw-a")["device"]
    assert not next(l for l in d["l3_interfaces"] if l["id"] == "vlan100")["oper"]
    assert int(OidProvider(controller.snapshot("sw-a"),controller.clock()).get("1.3.6.1.2.1.2.2.1.8.10100")) == 2


def test_access_vlan_mismatch_does_not_break_lldp(tmp_path,clock):
    lab = pair_lab()
    lab["links"][0].update(mode="access",vlan=100)
    lab["devices"][1]["vlans"].append(200)
    lab["devices"][1]["interfaces"][47]["pvid"] = 200
    core = Controller(tmp_path/"access.db",clock)
    try:
        core.create(lab)
        assert not core.snapshot("sw-001")["tables"]["flows"][0]["reachable"]
        assert core.topology("p48-p48")["links"][0]["lldp_discovered"] == [True,True]
    finally:
        core.store.close()


def test_lag_member_cannot_also_have_a_physical_link():
    from idc_simulator.errors import SimulationError
    lab = lag_lab()
    lab["links"].append({"id":"duplicate","endpoints":["sw-001:p47","sw-002:p47"]})
    with pytest.raises(SimulationError,match="multiple links"):
        normalize_lab(lab)


def test_mixed_l3_failure_reroutes_and_updates_standard_mibs(tmp_path,clock):
    lab = generate_lab(100,48,layout="mixed")
    core = Controller(tmp_path/"routed.db",clock)
    try:
        core.create(lab)
        before = core.snapshot("sw-005")
        flow = before["tables"]["flows"][0]
        assert flow["reachable"]
        edge = next(e for e in flow["path"] if e[0].startswith("sw-005:"))
        link = next(l for l in lab["links"] if set(l["endpoints"]) == set(edge))
        local = next(l for l in before["device"]["l3_interfaces"] if l.get("interface") == edge[0].split(":")[1])
        from ipaddress import ip_interface
        ip = str(ip_interface(local["address"]).ip)
        provider = OidProvider(before,clock())
        assert int(provider.get("1.3.6.1.2.1.4.20.1.2."+ip)) == local["ifindex"]
        assert int(provider.get("1.3.6.1.2.1.4.21.1.2.10.10.0.0")) == 10100
        core.transact("sw-005",[{"kind":"link","id":link["id"],"up":False}],before["revision"])
        after = core.snapshot("sw-005")
        assert after["tables"]["flows"][0]["reachable"]
        assert edge not in after["tables"]["flows"][0]["path"]
        assert not next(l for l in after["device"]["l3_interfaces"] if l["id"] == local["id"])["oper"]
        network = str(ip_interface(local["address"]).network.network_address)
        from pysnmp.proto import rfc1905
        assert isinstance(OidProvider(after,clock()).get("1.3.6.1.2.1.4.21.1.2."+network),rfc1905.NoSuchInstance)
    finally:
        core.store.close()


def test_old_snapshot_upgrade_preserves_port_identity_and_startup(tmp_path,clock):
    database = tmp_path/"upgrade.db"
    core = Controller(database,clock)
    core.create(pair_lab())
    state = core.labs["p48-p48"]
    expected = state["lab"]["devices"]["sw-001"]["interfaces"]["p48"]["mac"]
    for configs in (state["lab"]["devices"],state["startup"]):
        for d in configs.values():
            for p in d["interfaces"].values():
                p.pop("mac"); p.pop("mode")
            for l in d["l3_interfaces"]:
                l.pop("admin"); l.pop("mac")
    core.store.commit("p48-p48",state)
    core.store.close()
    restored = Controller(database,clock)
    try:
        assert restored.snapshot("sw-001")["device"]["interfaces"]["p48"]["mac"] == expected
        restored.transact("sw-001",[{"kind":"reboot"}],restored.snapshot("sw-001")["revision"])
        assert restored.snapshot("sw-001")["device"]["interfaces"]["p48"]["mac"] == expected
    finally:
        restored.store.close()
