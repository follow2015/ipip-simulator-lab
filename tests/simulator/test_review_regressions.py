import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock
from tempfile import TemporaryDirectory
from pathlib import Path
import pytest
import asyncssh
from aiohttp.test_utils import TestClient,TestServer
from idc_simulator.control import Controller
from idc_simulator.errors import SimulationError,Unavailable
from test_protocols import services  # noqa: F401  (pytest fixture shared with test_protocols)
from idc_simulator.model import normalize_lab
from idc_simulator.tools import generate_lab
from idc_simulator.idc_lab import generate_gpu_idc
from idc_simulator.runtime import Runtime
from idc_simulator.notifications import NotificationSender
from idc_simulator.metrics import Metrics
from idc_simulator.ipc import RpcServer
from pysnmp.hlapi.v3arch.asyncio import (UsmUserData,ContextData,ObjectType,ObjectIdentity,get_cmd,
    usmHMACSHAAuthProtocol,usmAesCfb128Protocol)


def test_same_vlan_requires_subnet_or_gateway(tmp_path,clock):
    lab=generate_lab(2,4)
    lab["hosts"][1]["address"]="198.51.100.10/24"
    core=Controller(tmp_path/"subnet.db",clock)
    try:
        core.create(lab)
        assert not core.diagnose_ping(lab["name"],"host-1","198.51.100.10")["reachable"]
        assert not core.snapshot("sw-001")["tables"]["flows"][0]["reachable"]
    finally:
        core.store.close()


@pytest.mark.parametrize("value",[
    {"id":"duplicate","vlan":100,"address":"192.0.2.1/24"},
    {"id":"collision","vlan":100,"address":"192.0.3.1/24","ifindex":1},
    {"id":"invalid","vlan":100,"interface":"p1","address":"192.0.3.1/24"},
    {"id":"physical","interface":"p2","address":"192.0.3.1/24","ifindex":99},
])
def test_invalid_l3_cannot_poison_committed_state(tmp_path,clock,value):
    core=Controller(tmp_path/"l3.db",clock)
    try:
        core.create(generate_lab(2,4))
        with pytest.raises(SimulationError):
            core.transact("sw-001",[{"kind":"l3","value":value}],1)
        assert core.snapshot("sw-001")["revision"]==1
    finally:
        core.store.close()


def test_duplicate_ip_different_prefix_is_rejected():
    lab=generate_lab(2,4)
    lab["hosts"][1]["address"]="192.0.2.10/25"
    with pytest.raises(SimulationError,match="Duplicate host"):
        normalize_lab(lab)


def test_server_counters_follow_wire_direction(tmp_path,clock):
    core=Controller(tmp_path/"gpu.db",clock)
    try:
        core.create(generate_gpu_idc());clock.advance(1)
        src=core.snapshot("gpu-001")["runtime"]["counters"]["infer"]
        dst=core.snapshot("gpu-064")["runtime"]["counters"]["infer"]
        assert src["out_bytes"]==1_250_000_000 and src["in_bytes"]==0
        assert dst["in_bytes"]==1_250_000_000 and dst["out_bytes"]==0
    finally:
        core.store.close()


async def test_vrf_context_added_and_removed_without_worker_restart(services):
    _,client,agent,_,manager,target,_=services
    await client.transact([{"kind":"vlan","vlan":200},{"kind":"l3","value":{"id":"tenant","vlan":200,"address":"198.51.100.1/24","vrf":"tenant"}}])
    assert b"tenant" in agent.context.context_names
    assert agent.context.get_mib_instrum(b"tenant").context_name=="tenant"
    user=UsmUserData("sim-user","sim-auth-password","sim-priv-password",
        authProtocol=usmHMACSHAAuthProtocol,privProtocol=usmAesCfb128Protocol)
    result=await get_cmd(manager,user,target,ContextData(contextName="tenant"),
        ObjectType(ObjectIdentity("1.3.6.1.2.1.1.5.0")))
    assert result[0] is None and not result[1] and str(result[3][0][1])=="IDC-DEMO"
    await client.transact([{"kind":"l3","value":{"id":"tenant"},"delete":True}])
    assert b"tenant" not in agent.context.context_names


async def test_notification_kept_until_control_recovers():
    sender=NotificationSender.__new__(NotificationSender)
    sender.queue=asyncio.Queue();sender.targets=[{}];sender.rate=1000;sender.metrics=Metrics()
    cursor=[];attempts=[];fail=[True]
    async def call(method,**params):
        cursor.append(params["value"])
    sender.client=SimpleNamespace(call=call)
    sender.record=AsyncMock()
    async def send(event,n,target):
        attempts.append(event["id"])
        if fail[0]:raise Unavailable("temporary failure")
    sender.send=send
    await sender.queue.put({"id":1});await sender.queue.put({"id":2})
    task=asyncio.create_task(sender.consume())
    try:
        await asyncio.sleep(.03)
        assert cursor==[] and sender.queue.qsize()==1
        fail[0]=False
        await asyncio.wait_for(sender.queue.join(),1)
        assert cursor==[1,2] and attempts.count(1)>=2
    finally:
        task.cancel();await asyncio.gather(task,return_exceptions=True)


async def test_worker_recovery_retries_transient_failure(tmp_path):
    runtime=Runtime(tmp_path,model_only=True)
    runtime.controller.create(generate_lab(1,4))
    supervisor=runtime.supervisor
    supervisor.spawn=AsyncMock(side_effect=[RuntimeError("first failed"),None])
    try:
        await asyncio.wait_for(supervisor.monitor("idc-scale","sw-001",SimpleNamespace(wait=AsyncMock()),"query"),5)
        assert supervisor.spawn.await_count==2
        assert runtime.controller.labs["idc-scale"]["status"]=="running"
    finally:
        await runtime.close()


async def test_worker_recovery_does_not_hide_another_failed_worker(tmp_path):
    runtime=Runtime(tmp_path,model_only=True)
    runtime.controller.create(generate_lab(2,4))
    supervisor=runtime.supervisor
    supervisor.recovering.add(("idc-scale","sw-002"))
    supervisor.spawn=AsyncMock()
    try:
        await supervisor.monitor("idc-scale","sw-001",SimpleNamespace(wait=AsyncMock()),"query")
        assert runtime.controller.labs["idc-scale"]["status"]=="degraded"
        supervisor.processes["idc-scale"]={}
        assert (await supervisor.start("idc-scale"))["status"]=="degraded"
    finally:
        await runtime.close()


async def test_ssh_tracks_connection_until_all_channels_close(services):
    ssh=services[3]
    conn=await asyncssh.connect("127.0.0.1",port=services[-1],username="sim",
        password="sim-password",known_hosts=None,config=None)
    try:
        proc=await conn.create_process(term_type="vt100")
        await asyncio.wait_for(proc.stdout.readuntil("#"),2)
        await conn.run("show version",check=True)
        assert len(ssh.connections)==1
        proc.stdin.write("exit\r");await proc.wait_closed()
        assert len(ssh.connections)==1
    finally:
        conn.close();await conn.wait_closed()
    await asyncio.sleep(.02)
    assert not ssh.connections


async def test_rpc_peer_disconnect_during_response_is_clean(tmp_path):
    entered=asyncio.Event();release=asyncio.Event()
    async def handler(request):
        entered.set();await release.wait();return {"data":"x"*100000}
    directory=TemporaryDirectory(prefix="rpc-review-",dir="/tmp")
    server=RpcServer(Path(directory.name)/"rpc.sock",handler)
    loop=asyncio.get_running_loop();errors=[];original=loop.get_exception_handler()
    loop.set_exception_handler(lambda loop,context:errors.append(context))
    try:
        await server.start()
        _,writer=await asyncio.open_unix_connection(str(server.path))
        writer.write(b'{"method":"test"}\n');await writer.drain()
        await entered.wait();writer.close();await writer.wait_closed()
        release.set();await asyncio.sleep(.05)
        assert errors==[]
    finally:
        await server.close();loop.set_exception_handler(original);directory.cleanup()


@pytest.mark.parametrize("fields",[{"description":None},{"speed_bps":1.5},{"mtu":True},{"allowed_vlans":"100"}])
async def test_bad_interface_inputs_return_json_400(tmp_path,fields):
    runtime=Runtime(tmp_path,model_only=True);runtime.controller.create(generate_lab(1,4))
    client=TestClient(TestServer(runtime.app()));await client.start_server()
    try:
        response=await client.patch("/v1/devices/sw-001/interfaces/p1",headers={"If-Match":'"1"'},json=fields)
        assert response.status==400 and "error" in await response.json()
        assert runtime.controller.snapshot("sw-001")["revision"]==1
    finally:
        await client.close();await runtime.close()
