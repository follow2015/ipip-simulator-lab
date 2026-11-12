"""H1/H2, M1..M4 and L1..L8 regression coverage."""
import asyncio
from copy import deepcopy
import json
import os
import statistics
import time
from unittest.mock import AsyncMock

from aiohttp.test_utils import TestClient, TestServer
import pytest

from idc_simulator.control import Controller
from idc_simulator.deploy import names, network_plan, read_ready
from idc_simulator.errors import Conflict, SimulationError
from idc_simulator.ipc import RpcClient
from idc_simulator.runtime import Runtime
from idc_simulator.tools import generate_lab
from idc_simulator.topology import derive


@pytest.mark.parametrize('size',[25,50,100,200])
def test_linear_host_traversals_and_time_refresh(tmp_path,monkeypatch,size):
    import idc_simulator.topology as topology
    calls=[]
    original=topology.host_tree
    def tree(*args):
        calls.append(1);return original(*args)
    monkeypatch.setattr(topology,'host_tree',tree)
    core=Controller(tmp_path/'state.db');core.create(generate_lab(size,48))
    try:
        assert len(calls)==size
        state=core.labs['idc-scale'];samples=[]
        for _ in range(3):
            start=time.perf_counter();derive(state,time.monotonic_ns());samples.append(time.perf_counter()-start)
        base=Controller(tmp_path/'base.db');base.create(generate_lab(25,48))
        try:
            bstate=base.labs['idc-scale'];base_samples=[]
            for _ in range(3):
                start=time.perf_counter();derive(bstate,time.monotonic_ns());base_samples.append(time.perf_counter()-start)
        finally:
            base.store.close()
        median=statistics.median(samples)
        base_median=statistics.median(base_samples)
        assert median < base_median*(size/25)*4
        count=len(calls);derive(state,time.monotonic_ns()+1_000_000_000,time_only=True)
        assert len(calls)==count
        assert len(state['tables']['sw-001']['arp'])==size
        assert len(state['tables'][f'sw-{size:03d}']['fdb'])==size
    finally:core.store.close()


async def test_worker_environment_and_kernel_bound_channels(tmp_path,raw_lab,monkeypatch):
    from test_protocols import free_port
    import socket
    monkeypatch.setenv('IDC_API_TOKEN','control-secret')
    monkeypatch.setenv('IDC_CREDENTIAL_SETS','{"other":{"ssh_password":"unit-test-password"}}')
    monkeypatch.setenv('IDC_SSH_PASSWORD','sim-password')
    runtime=Runtime(tmp_path,snmp_port=free_port(socket.SOCK_DGRAM),ssh_port=free_port())
    runtime.controller.create(raw_lab)
    runtime.controller.create(generate_lab(1,4))
    await runtime.start()
    captured=[];spawn=asyncio.create_subprocess_exec
    async def intercept(*args,**kwargs):
        captured.append(kwargs.get('env',{}));return await spawn(*args,**kwargs)
    monkeypatch.setattr(asyncio,'create_subprocess_exec',intercept)
    try:
        await runtime.supervisor.start('local-demo')
        env=captured[-1]
        assert set(env)<= {'PATH','LANG','LC_ALL','LC_CTYPE','PYTHONPATH','VIRTUAL_ENV','TMPDIR','IDC_SIM_CREDENTIALS'}
        assert not any(k in env for k in ('IDC_API_TOKEN','IDC_CREDENTIAL_SETS','IDC_SSH_PASSWORD'))
        channel=runtime.channels[('sw-a','query')]
        with pytest.raises(SimulationError,match='assigned worker'):
            await RpcClient(channel.path,'sw-a').refresh()
        channel.peer_pid=os.getpid()
        with pytest.raises(SimulationError,match='does not match'):
            await RpcClient(channel.path,'sw-001').refresh()
        notification=await runtime.worker_channel('sw-a','notification');notification.peer_pid=os.getpid()
        bad=RpcClient(notification.path,'sw-a','notification')
        with pytest.raises(SimulationError,match='cannot modify'):
            await bad.call('transact',operations=[],revision=1)
        assert read_ready(tmp_path/'local-demo'/'sw-a'/'ready.json')['pid']>0
    finally:await runtime.close()
    assert not list(tmp_path.glob('*/*/metrics-*.prom'))


@pytest.mark.parametrize('headers,expected',[
    ({'Host':'evil.example'},403),
    ({'Origin':'http://evil.example'},403),
    ({'Content-Type':'text/plain'},415),
    ({'Origin':'null'},403),
])
async def test_cross_site_writes_rejected(tmp_path,headers,expected):
    runtime=Runtime(tmp_path,model_only=True);client=TestClient(TestServer(runtime.app()))
    await client.start_server()
    try:
        response=await client.post('/v1/labs',data=json.dumps(generate_lab(1,4)),headers={'Content-Type':'application/json',**headers})
        assert response.status==expected and not runtime.controller.labs
        assert (await client.get('/v1/labs',headers={'Host':'evil.example'})).status==403
    finally:await client.close();await runtime.close()


async def test_same_origin_and_explicit_remote_host_allowed(tmp_path):
    runtime=Runtime(tmp_path,model_only=True);client=TestClient(TestServer(runtime.app(allowed_hosts=['sim.example'])))
    await client.start_server()
    try:
        response=await client.post('/v1/labs',json=generate_lab(1,4),headers={'Host':'sim.example','Origin':'http://sim.example'})
        assert response.status==201
    finally:await client.close();await runtime.close()


async def test_failed_resume_keeps_api_and_other_labs_available(tmp_path,raw_lab):
    runtime=Runtime(tmp_path);runtime.controller.create(raw_lab);runtime.controller.create(generate_lab(2,4))
    runtime.controller.lab_status('local-demo','running');runtime.controller.lab_status('idc-scale','running')
    async def start(lab):
        if lab=='idc-scale':raise SimulationError('resource conflict')
    runtime.supervisor.start=AsyncMock(side_effect=start)
    try:
        await runtime.start()
        assert runtime.controller.labs['idc-scale']['status']=='degraded'
        assert runtime.supervisor.start.await_count==2
        client=TestClient(TestServer(runtime.app()));await client.start_server()
        try:assert (await client.get('/v1/status')).status==200
        finally:await client.close()
    finally:await runtime.close()


def test_idempotency_payload_revision_and_restart(tmp_path):
    path=tmp_path/'s.db';core=Controller(path);core.create(generate_lab(1,4))
    operations=[{'kind':'system','fields':{'hostname':'FIRST'}}]
    result=core.transact('sw-001',operations,1,'same')
    assert core.transact('sw-001',deepcopy(operations),1,'same')==result
    for ops,revision in (([],1),(operations,2)):
        with pytest.raises(Conflict,match='different request'):core.transact('sw-001',ops,revision,'same')
    core.store.close();core=Controller(path)
    try:assert core.transact('sw-001',operations,1,'same')==result
    finally:core.store.close()


async def test_scenario_idempotency_payload(tmp_path,raw_lab):
    runtime=Runtime(tmp_path);runtime.controller.create(raw_lab)
    spec={'schema_version':1,'steps':[{'device':'sw-a','at':60,'operations':[{'kind':'save'}]}]}
    try:
        first=await runtime.scenarios.start_async('test',spec,'once')
        assert await runtime.scenarios.start_async('test',deepcopy(spec),'once')==first
        with pytest.raises(Conflict):await runtime.scenarios.start_async('different',spec,'once')
    finally:await runtime.close()


def test_database_and_sidecars_private_without_changing_umask(tmp_path):
    core=Controller(tmp_path/'s.db')
    try:
        core.create(generate_lab(1,4))
        assert all(p.stat().st_mode&0o777==0o600 for p in tmp_path.glob('s.db*'))
    finally:core.store.close()


def test_resource_collision_rejected_before_deployment(monkeypatch):
    import idc_simulator.deploy as deploy
    core_lab=generate_lab(2,4)
    from idc_simulator.model import normalize_lab
    lab=normalize_lab(core_lab)
    assert all(len(names(lab['name'],dev)['host_veth'])<=15 for dev in lab['devices'])
    monkeypatch.setattr(deploy,'names',lambda *args:{'namespace':'collision','host_veth':'collision','bridge':'b','peer':'mgmt0'})
    with pytest.raises(SimulationError,match='collision'):network_plan(lab)


def test_worker_restart_does_not_invalidate_configuration_revision(tmp_path):
    core=Controller(tmp_path/'s.db');core.create(generate_lab(2,4))
    try:
        core.transact('sw-001',[{'kind':'worker_started'}],1)
        assert core.snapshot('sw-001')['revision']==1
        core.transact('sw-002',[{'kind':'system','fields':{'hostname':'EDIT'}}],1)
        assert core.snapshot('sw-001')['revision']==2
    finally:core.store.close()


def test_device_scoped_cas_allows_independent_edits_but_rejects_topology_changes(tmp_path):
    core=Controller(tmp_path/'s.db');core.create(generate_lab(2,4))
    try:
        core.transact('sw-001',[{'kind':'system','fields':{'hostname':'ONE'}}],1)
        core.transact('sw-002',[{'kind':'system','fields':{'hostname':'TWO'}}],1,scope='device')
        with pytest.raises(Conflict):core.transact('sw-002',[{'kind':'save'}],1,scope='device')
        core.transact('sw-001',[{'kind':'link','id':core.labs['idc-scale']['lab']['links'][0]['id'],'up':False}],3)
        with pytest.raises(Conflict):core.transact('sw-002',[{'kind':'save'}],3,scope='device')
    finally:core.store.close()


def test_boot_id_survives_wall_clock_step_and_resets_on_actual_boot(tmp_path,monkeypatch):
    import idc_simulator.control as control
    monkeypatch.setattr(control,'host_boot_id',lambda:'boot-one')
    core=Controller(tmp_path/'s.db');core.create(generate_lab(1,4));core.checkpoint()
    initial=core.snapshot('sw-001')['runtime']['boot_ns'];core.store.close()
    monkeypatch.setattr(control.time,'time',lambda:1)
    core=Controller(tmp_path/'s.db')
    assert core.snapshot('sw-001')['runtime']['boot_ns']==initial
    core.checkpoint();core.store.close()
    monkeypatch.setattr(control,'host_boot_id',lambda:'boot-two')
    core=Controller(tmp_path/'s.db')
    try:assert core.snapshot('sw-001')['runtime']['boot_ns']>initial
    finally:core.store.close()


async def test_slow_state_transaction_does_not_block_status(tmp_path):
    runtime=Runtime(tmp_path,model_only=True);client=TestClient(TestServer(runtime.app()))
    await client.start_server();entered=asyncio.Event()
    loop=asyncio.get_running_loop()
    def slow():
        loop.call_soon_threadsafe(entered.set);time.sleep(.3)
    task=asyncio.create_task(runtime.state_call(slow))
    try:
        await entered.wait();start=time.perf_counter()
        assert (await client.get('/v1/status')).status==200
        assert time.perf_counter()-start<.15
        await task
    finally:await client.close();await runtime.close()


def test_transaction_writes_only_the_targeted_runtime_row(tmp_path,monkeypatch):
    core=Controller(tmp_path/'s.db');core.create(generate_lab(8,4))
    seen=[];original=core.store._write_state
    def spy(lab_id,state,devices,runtime):
        seen.append((devices,runtime));return original(lab_id,state,devices,runtime)
    monkeypatch.setattr(core.store,'_write_state',spy)
    try:
        core.transact('sw-001',[{'kind':'system','fields':{'hostname':'edge-1'}}],1)
        assert seen[-1]==({'sw-001'},{'sw-001'})
        assert all(devices is not None and runtime is not None for devices,runtime in seen)
    finally:core.store.close()


async def test_allowed_hosts_strips_whitespace_and_folds_case(tmp_path):
    from idc_simulator.runtime import configured_hosts
    assert configured_hosts('0.0.0.0','a, b ,  ')==['a','b']
    runtime=Runtime(tmp_path,model_only=True);client=TestClient(TestServer(runtime.app(allowed_hosts=configured_hosts('0.0.0.0','a, b'))))
    await client.start_server()
    try:
        assert (await client.get('/v1/status',headers={'Host':'b'})).status==200
        assert (await client.get('/v1/status',headers={'Host':'B'})).status==200
        assert (await client.get('/v1/status',headers={'Host':'c'})).status==403
    finally:await client.close();await runtime.close()


async def test_state_call_execution_timeout_surfaces_and_gates_probes(tmp_path):
    from idc_simulator.errors import Unavailable
    runtime=Runtime(tmp_path,model_only=True);runtime.state_timeout=.05
    try:
        with pytest.raises(Unavailable):
            await runtime.state_call(time.sleep,.2)
        assert runtime.metrics.counts['state_call_timeouts_total']==1
        assert runtime.state_timeout_function=='sleep'
        start=time.perf_counter()
        with pytest.raises(Unavailable):
            await runtime.state_call(lambda:None)
        assert time.perf_counter()-start<.02
        await asyncio.sleep(.25)
        runtime.state_timeout_at-=runtime.state_timeout
        assert await runtime.state_call(lambda:'ok')=='ok'
        assert runtime.state_timeout_function is None
    finally:
        runtime.state_timeout=30;await runtime.close()
