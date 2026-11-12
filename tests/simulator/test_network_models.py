from idc_simulator.control import Controller
from idc_simulator.oid import OidProvider
from idc_simulator.tools import generate_lab


def test_connected_static_routing_and_firewall(tmp_path, clock):
    lab = generate_lab(3, 4)
    for d in lab["devices"]:
        d["vlans"] = [100, 200, 300]
    lab["hosts"] = [
        {"id": "src", "device": "sw-001", "interface": "p1", "mac": "02:00:00:00:00:01", "address": "192.0.2.10/24", "gateway": "192.0.2.1", "vlan": 100},
        {"id": "dst", "device": "sw-003", "interface": "p1", "mac": "02:00:00:00:00:02", "address": "198.51.100.10/24", "gateway": "198.51.100.1", "vlan": 300}]
    lab["flows"] = [{"id": "routed", "source": "src", "destination": "dst", "rate_bps": 8000000}]
    lab["links"][0]["allowed_vlans"] = [100]
    lab["links"][1]["allowed_vlans"] = [200]
    lab["devices"][1]["l3_interfaces"] = [{"id": "source", "address": "192.0.2.1/24", "vlan": 100}, {"id": "transit", "address": "203.0.113.1/24", "vlan": 200}]
    lab["devices"][0]["l3_interfaces"] = []
    lab["devices"][2]["l3_interfaces"] = [{"id": "transit", "address": "203.0.113.2/24", "vlan": 200}, {"id": "destination", "address": "198.51.100.1/24", "vlan": 300}]
    core = Controller(tmp_path/"route.db", clock)
    try:
        core.create(lab)
        assert not core.snapshot("sw-001")["tables"]["flows"][0]["reachable"]
        arp = next(a for a in core.snapshot("sw-002")["tables"]["arp"] if a["ip"] == "203.0.113.2")
        assert arp["origin"] == "svi-neighbor" and arp["mac"] == core.snapshot("sw-003")["device"]["chassis_mac"]
        rev = core.snapshot("sw-002")["revision"]
        core.transact("sw-002", [{"kind": "route", "value": {"prefix": "198.51.100.0/24", "next_hop": "203.0.113.2", "vrf": "default"}}], rev)
        assert core.snapshot("sw-001")["tables"]["flows"][0]["reachable"]
        core.transact("sw-002", [{"kind": "firewall", "value": {"id": "deny", "source": "192.0.2.0/24", "destination": "198.51.100.0/24", "action": "deny"}}], rev+1)
        assert not core.snapshot("sw-001")["tables"]["flows"][0]["reachable"]
    finally:
        core.store.close()


def test_lag_mapping_card_fault_and_recovery(tmp_path, clock):
    lab = generate_lab(2, 5)
    for n, d in enumerate(lab["devices"]):
        d["interfaces"].append({"id": "lag1", "name": "Port-channel1", "ifindex": 100, "speed_bps": 2000000000, "type": 161, "members": ["p4", "p5"]})
        d["cards"] = [{"id": "linecard", "index": 2, "class": 9, "name": "Line card"}]
        d["interfaces"][-1]["card"] = "linecard"
    lab["links"][0]["endpoints"] = ["sw-001:lag1", "sw-002:lag1"]
    core = Controller(tmp_path/"lag.db", clock)
    try:
        core.create(lab)
        snap = core.snapshot("sw-001")
        assert snap["tables"]["lldp"][0]["ifindex"] == 100
        assert next(f for f in snap["tables"]["fdb"] if f["mac"] == lab["hosts"][1]["mac"])["ifindex"] == 100
        provider = OidProvider(snap, clock())
        assert int(provider.get("1.3.6.1.2.1.31.1.2.1.3.100.4")) == 1
        core.transact("sw-001", [{"kind": "card", "id": "linecard", "present": False}], snap["revision"])
        assert not core.snapshot("sw-002")["device"]["interfaces"]["lag1"]["oper"]
        assert core.snapshot("sw-002")["device"]["interfaces"]["lag1"]["admin"]
        core.transact("sw-001", [{"kind": "card", "id": "linecard", "present": True}], snap["revision"]+1)
        assert core.snapshot("sw-002")["device"]["interfaces"]["lag1"]["oper"]
    finally:
        core.store.close()
