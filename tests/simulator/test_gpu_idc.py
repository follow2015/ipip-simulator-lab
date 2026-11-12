from pathlib import Path
import pytest
import asyncssh
import yaml
from aiohttp.test_utils import TestClient, TestServer
from idc_simulator.control import Controller
from idc_simulator.idc_lab import generate_gpu_idc
from idc_simulator.model import normalize_lab, mac_bytes
from idc_simulator.oid import OidProvider
from idc_simulator.notifications import event_varbinds
from idc_simulator.runtime import Runtime
from idc_simulator.errors import SimulationError
from test_protocols import services  # noqa: F401  (pytest fixture shared with test_protocols)


@pytest.fixture
def gpu_core(tmp_path, clock):
    lab = generate_gpu_idc()
    lab["defaults"].update(mac_aging_seconds=3, arp_aging_seconds=3)
    core = Controller(tmp_path/"gpu.db",clock)
    core.create(lab)
    yield core
    core.store.close()


def modify(core, device, operations):
    return core.transact(device,operations,core.snapshot(device)["revision"])


def test_exact_idc_inventory_and_fixture():
    raw = generate_gpu_idc()
    assert yaml.safe_load((Path(__file__).resolve().parents[2]/"examples/idc-64gpu.yaml").read_text()) == raw
    lab = normalize_lab(raw)
    assert len(lab["devices"]) == 82 and len(lab["links"]) == 280 and len(lab["hosts"]) == 268
    assert sum(d.get("hardware",{}).get("gpu_count",0) for d in lab["devices"].values()) == 512
    for dev,d in lab["devices"].items():
        physical = [p for p in d["interfaces"].values() if not p.get("members")]
        if dev.startswith(("infer-sw", "storage-sw")):
            assert len(physical) == 64 and all(p["speed_bps"] == 100_000_000_000 for p in physical)
        elif dev.startswith("inband-sw"):
            assert sum(p["speed_bps"] == 25_000_000_000 for p in physical) == 48
            assert sum(p["speed_bps"] == 100_000_000_000 for p in physical) == 8
        elif dev.startswith("oob-sw"):
            assert sum(p["speed_bps"] == 1_000_000_000 and p["medium"] == "rj45" for p in physical) == 48
            assert sum(p["speed_bps"] == 10_000_000_000 for p in physical) == 4
        else:
            assert not d["forwarding"]


@pytest.mark.parametrize("address,device,port",[
    ("10.10.0.74","infer-sw-4","p16"), ("10.20.0.74","storage-sw-4","p16"),
    ("10.30.0.74","inband-sw-4","p16"), ("10.40.0.74","oob-sw-4","p16"),
    ("10.20.0.101","storage-sw-1","p17"), ("10.20.0.104","storage-sw-4","p17")])
def test_ip_mac_port_lookup_and_roundtrip_ping(gpu_core,address,device,port):
    core = gpu_core
    result = core.diagnose_ping("idc-64gpu","gpu-001",address)
    assert result["mode"] == "simulated" and result["reachable"] and result["received"] == 5
    location = core.locate_address("idc-64gpu",ip=address)
    attachment = [p for p in location["ports"] if p["role"] == "attachment"]
    assert [(p["device"],p["interface"]) for p in attachment] == [(device,port)]
    assert attachment[0]["ips"] == [address] and attachment[0]["fresh"]
    reverse = core.locate_address("idc-64gpu",mac=attachment[0]["mac"].upper())
    assert address in {b["ip"] for b in reverse["bindings"]}
    reverse_port = core.locate_address("idc-64gpu",device=device,port=port)
    assert address in {b["ip"] for b in reverse_port["bindings"]}
    snapshot = core.snapshot(device)
    provider = OidProvider(snapshot,core.clock(),result["vrf"])
    suffix = ".".join(str(v) for v in mac_bytes(attachment[0]["mac"]))
    assert int(provider.get(f"1.3.6.1.2.1.17.7.1.2.2.1.2.{attachment[0]['fdb_id']}.{suffix}")) == attachment[0]["bridge_port"]
    assert int(provider.get(f"1.3.6.1.2.1.31.1.1.1.15.{attachment[0]['ifindex']}")) in {1000,25000,100000}


def test_link_flap_traps_stale_lookup_and_recovery(gpu_core,clock):
    core = gpu_core
    modify(core,"infer-sw-1",[{"kind":"link","id":"gpu-001-infer","up":False}])
    assert not core.diagnose_ping("idc-64gpu","gpu-001","10.10.0.74")["reachable"]
    assert core.diagnose_ping("idc-64gpu","gpu-001","10.20.0.74")["reachable"]
    stale = core.locate_address("idc-64gpu",ip="10.10.0.11")
    assert stale["found"] and all(not p["fresh"] for p in stale["ports"])
    events = [e for e in core.store.events() if e["type"] == "linkDown"]
    assert {e["device_id"] for e in events} == {"gpu-001","infer-sw-1"}
    for event in events:
        binds = event_varbinds(event,core.snapshot(event["device_id"]))
        assert str(binds[1][1]) == "1.3.6.1.6.3.1.1.5.3" and int(binds[-1][1]) == 2
    clock.advance(4)
    assert not core.locate_address("idc-64gpu",ip="10.10.0.11")["found"]
    modify(core,"infer-sw-1",[{"kind":"link","id":"gpu-001-infer","up":True}])
    assert core.diagnose_ping("idc-64gpu","gpu-001","10.10.0.74")["reachable"]
    assert {e["device_id"] for e in core.store.events() if e["type"] == "linkUp"} == {"gpu-001","infer-sw-1"}


def test_storage_nics_do_not_bridge_or_bypass_failed_isl(gpu_core):
    core = gpu_core
    modify(core,"storage-sw-1",[{"kind":"link","id":"storage-isl-1","up":False}])
    assert core.diagnose_ping("idc-64gpu","gpu-001","10.20.0.101")["reachable"]
    result = core.diagnose_ping("idc-64gpu","gpu-001","10.20.0.104")
    assert not result["reachable"] and result["reason"] == "no-forwarding-path"
    assert not core.snapshot("store-01")["tables"]["fdb"]
    assert core.snapshot("store-01")["device"]["interfaces"]["data4"]["oper"]


def test_lag_min_links_membership_rollback_and_vlan_filter(gpu_core):
    core = gpu_core
    modify(core,"infer-sw-1",[{"kind":"interface","port":"p63","fields":{"admin":False}}])
    assert core.diagnose_ping("idc-64gpu","gpu-001","10.10.0.74")["reachable"]
    modify(core,"infer-sw-1",[{"kind":"lag","port":"lag-right","fields":{"min_links":2}}])
    assert not core.diagnose_ping("idc-64gpu","gpu-001","10.10.0.74")["reachable"]
    modify(core,"infer-sw-1",[{"kind":"lag","port":"lag-right","fields":{"min_links":1}}])
    revision = core.snapshot("infer-sw-1")["revision"]
    with pytest.raises(SimulationError,match="multiple links"):
        core.transact("infer-sw-1",[{"kind":"lag","port":"lag-right","fields":{"members":["p1","p64"]}}],revision)
    assert core.snapshot("infer-sw-1")["revision"] == revision
    modify(core,"infer-sw-1",[{"kind":"interface","port":"p1","fields":{"allowed_vlans":[]}}])
    assert not core.diagnose_ping("idc-64gpu","gpu-001","10.10.0.74")["reachable"]
    assert any(n["remote_device"] == "gpu-001" for n in core.snapshot("infer-sw-1")["tables"]["lldp"])


def test_ping_requires_return_route_and_checks_firewall(tmp_path,clock):
    raw = yaml.safe_load((Path(__file__).resolve().parents[2]/"examples/l3-port48.yaml").read_text())
    raw["devices"][1]["routes"] = []
    core = Controller(tmp_path/"route.db",clock)
    try:
        core.create(raw)
        result = core.diagnose_ping(raw["name"],"host-1","192.0.3.10")
        assert result["forward"]["reachable"] and not result["reachable"]
        assert result["reason"] == "return-no-forwarding-path"
        modify(core,"sw-002",[{"kind":"route","value":{"prefix":"192.0.2.0/24","next_hop":"203.0.113.1"}}])
        assert core.diagnose_ping(raw["name"],"host-1","192.0.3.10")["reachable"]
        modify(core,"sw-002",[{"kind":"firewall","value":{"id":"deny-reply","source":"192.0.3.0/24","destination":"192.0.2.0/24","action":"deny"}}])
        assert core.diagnose_ping(raw["name"],"host-1","192.0.3.10")["reason"] == "return-firewall-deny"
    finally:
        core.store.close()


async def test_diagnostic_rest_auth_validation_and_port_reverse(tmp_path,raw_lab):
    runtime = Runtime(tmp_path)
    runtime.controller.create(raw_lab)
    client = TestClient(TestServer(runtime.app("secret")))
    await client.start_server()
    auth = {"Authorization":"Bearer secret"}
    base = "/v1/labs/local-demo"
    try:
        assert (await client.get(base+"/locate?ip=192.0.2.11")).status == 401
        response = await client.get(base+"/locate?device=sw-a&port=p2",headers=auth)
        assert response.status == 200 and (await response.json())["ports"][0]["ips"] == ["192.0.2.11"]
        response = await client.post(base+"/ping",headers=auth,json={"source":"host-a","destination":"192.0.2.11"})
        assert response.status == 200 and (await response.json())["reachable"]
        assert (await client.get(base+"/locate?ip=bad",headers=auth)).status == 400
        assert (await client.get(base+"/locate?ip=192.0.2.11&mac=02:00:00:00:01:02",headers=auth)).status == 400
        assert (await client.get(base+"/locate?device=sw-a&port=missing",headers=auth)).status == 404
        response = await client.post(base+"/ping",headers=auth,json={"source":"host-a","destination":"192.0.2.200"})
        assert (await response.json())["reason"] == "destination-not-modeled"
        assert runtime.controller.snapshot("sw-a")["revision"] == 1
    finally:
        await client.close(); await runtime.close()


async def test_real_ssh_ping_uses_control_state(services):
    runtime,client,_,_,_,_,port = services
    async with asyncssh.connect("127.0.0.1",port=port,username="sim",password="sim-password",known_hosts=None,config=[]) as conn:
        result = await conn.run("ping -c 3 192.0.2.11",check=True)
        assert "Simulated ping" in result.stdout and "3 received, 0% packet loss" in result.stdout
        await client.transact([{"kind":"interface","port":"p2","fields":{"admin":False}}])
        result = await conn.run("ping 192.0.2.11 source 192.0.2.10",check=True)
        assert "0 received, 100% packet loss" in result.stdout and "endpoint-down" in result.stdout
