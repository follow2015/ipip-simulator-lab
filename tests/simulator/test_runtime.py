import asyncio
import socket
from aiohttp.test_utils import TestClient, TestServer
from pysnmp.hlapi.v3arch.asyncio import SnmpEngine, CommunityData, ContextData, UdpTransportTarget, ObjectType, ObjectIdentity, get_cmd, set_cmd
from pysnmp.proto import rfc1902
from idc_simulator.runtime import Runtime
from idc_simulator.deploy import network_plan, names
from idc_simulator.tools import generate_lab
from idc_simulator.model import normalize_lab
from test_protocols import free_port
from test_protocols import services  # noqa: F401  (pytest fixture shared with test_protocols)


async def test_api_validation_revision_idempotency_and_scenario(tmp_path, raw_lab):
    runtime = Runtime(tmp_path)
    await runtime.start()
    client = TestClient(TestServer(runtime.app("test-token")))
    await client.start_server()
    auth = {"Authorization": "Bearer test-token"}
    try:
        assert (await client.get("/v1/labs")).status == 401
        result = await client.post("/v1/labs", json=raw_lab, headers=auth)
        assert result.status == 201
        assert (await client.post("/v1/labs", json=raw_lab, headers=auth)).status == 409
        response = await client.get("/v1/devices/sw-a/state", headers=auth)
        revision = response.headers["ETag"]
        headers = {**auth, "If-Match": revision, "Idempotency-Key": "patch1"}
        response = await client.patch("/v1/devices/sw-a/interfaces/p1", json={"admin": False}, headers=headers)
        assert response.status == 200
        result = await response.json()
        response = await client.patch("/v1/devices/sw-a/interfaces/p1", json={"admin": False}, headers=headers)
        assert await response.json() == result
        assert (await client.patch("/v1/devices/sw-a/interfaces/p2", json={"admin": False}, headers={**auth, "If-Match": revision})).status == 409
        response = await client.post("/v1/scenarios/recovery/run", json={"spec": {"schema_version": 1, "seed": 1, "steps": [{"at": 0, "device": "sw-a", "operations": [{"kind": "interface", "port": "p1", "fields": {"admin": True}}]}]}}, headers=auth)
        assert response.status == 202
        run_id = (await response.json())["operation_id"]
        await asyncio.sleep(.02)
        assert (await (await client.get("/v1/operations/"+run_id, headers=auth)).json())["status"] == "complete"
        assert runtime.controller.snapshot("sw-a")["device"]["interfaces"]["p1"]["admin"]
        assert "idc_api_response_seconds" in await (await client.get("/metrics", headers=auth)).text()
    finally:
        await client.close(); await runtime.close()


async def test_real_worker_start_restart_hostkey_and_stop(tmp_path, raw_lab):
    port, ssh_port = free_port(socket.SOCK_DGRAM), free_port()
    runtime = Runtime(tmp_path, snmp_port=port, ssh_port=ssh_port)
    runtime.controller.create(raw_lab)
    await runtime.start()
    manager = SnmpEngine()
    target = await UdpTransportTarget.create(("127.0.0.1", port), timeout=1, retries=0)
    try:
        await runtime.supervisor.start("local-demo")
        original = runtime.controller.snapshot("sw-a")["runtime"]
        hostkey = (tmp_path/"local-demo/sw-a/ssh_host_key").read_bytes()
        result = await get_cmd(manager, CommunityData("sim-read"), target, ContextData(), ObjectType(ObjectIdentity("1.3.6.1.2.1.1.5.0")))
        assert result[0] is None and str(result[3][0][1]) == "IDC-DEMO"
        process = runtime.supervisor.processes["local-demo"]["sw-a"]
        process.kill(); await process.wait()
        deadline = asyncio.get_running_loop().time()+10
        while asyncio.get_running_loop().time() < deadline:
            if runtime.supervisor.processes["local-demo"]["sw-a"].pid != process.pid and (tmp_path/"local-demo/sw-a/ready.json").exists():
                break
            await asyncio.sleep(.05)
        current = runtime.controller.snapshot("sw-a")["runtime"]
        assert current["engine_id"] == original["engine_id"] and current["engine_boots"] == original["engine_boots"]+1
        assert (tmp_path/"local-demo/sw-a/ssh_host_key").read_bytes() == hostkey
        await runtime.supervisor.stop("local-demo")
        assert not runtime.supervisor.processes
        assert not (tmp_path/"local-demo/sw-a/ready.json").exists()
    finally:
        manager.close_dispatcher(); await runtime.close()


async def test_control_outage_reads_survive_writes_rejected(services):
    runtime, client, _, _, manager, target, _ = services
    await runtime.rpc.close()
    result = await get_cmd(manager, CommunityData("sim-read"), target, ContextData(), ObjectType(ObjectIdentity("1.3.6.1.2.1.1.5.0")))
    assert result[0] is None and str(result[3][0][1]) == "IDC-DEMO"
    result = await set_cmd(manager, CommunityData("sim-write"), target, ContextData(), ObjectType(ObjectIdentity("1.3.6.1.2.1.1.5.0"), rfc1902.OctetString("will-fail")))
    assert result[0] is None and int(result[1]) == 13
    assert runtime.controller.snapshot("sw-a")["device"]["hostname"] == "IDC-DEMO"
    await runtime.rpc.start()


def test_namespace_plans_isolate_management_and_cleanup():
    lab = normalize_lab(generate_lab(3, 48))
    create, delete = network_plan(lab), network_plan(lab, True)
    assert len([c for c in create if c[:3] == ["ip", "netns", "add"]]) == 3
    assert all(len(names(lab["name"], d)["host_veth"]) <= 15 for d in lab["devices"])
    assert all(c[0] == "ip" for c in create+delete)
    assert len(delete) == 4
