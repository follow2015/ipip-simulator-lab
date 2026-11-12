import asyncio
from copy import deepcopy
import json

import pytest
from aiohttp.test_utils import TestClient, TestServer

from idc_simulator.control import Controller
from idc_simulator.errors import Conflict, SimulationError
from idc_simulator.ipc import RpcClient
from idc_simulator.lifecycle import export_lab
from idc_simulator.oid import OidProvider, IFX
from idc_simulator.runtime import Runtime
from idc_simulator.tools import generate_lab


def conditional(core, dev, snapshot=None):
    return core.snapshot(dev, snapshot.get('control_epoch') if snapshot else None,
                         snapshot.get('state_seq') if snapshot else None, True)


def test_conditional_snapshot_keeps_counter_baseline_and_changes_on_reset(controller, clock):
    first = conditional(controller, 'sw-a')['snapshot']
    clock.advance(2)
    reply = conditional(controller, 'sw-a', first)
    assert reply['mode'] == 'not_modified'
    value = OidProvider(first, clock()).get(IFX+(6, 10))
    assert int(value) == 2_000_000
    controller.transact('sw-a', [{'kind': 'counter_reset'}], 1)
    assert conditional(controller, 'sw-a', first)['mode'] == 'full'
    reset = conditional(controller, 'sw-a')['snapshot']
    assert reset['runtime']['counter_generation'] == 1
    controller.transact('sw-a', [{'kind': 'counter_reset'}], 2)
    assert conditional(controller, 'sw-a', reset)['mode'] == 'full'


@pytest.mark.parametrize('operation',[
    {'kind':'interface','port':'p1','fields':{'admin':False}},
    {'kind':'flow','id':'demo','rate_bps':4_000_000},
    {'kind':'worker_started'}, {'kind':'reboot'},
    {'kind':'sensor','id':'temperature','value':85},
])
def test_protocol_state_changes_force_full(controller,operation):
    first=conditional(controller,'sw-a')['snapshot']
    controller.transact('sw-a',[operation],1)
    assert conditional(controller,'sw-a',first)['mode']=='full'


def test_peer_changes_and_ttl_expiry_have_own_state_sequence(tmp_path,clock):
    lab=generate_lab(2,4);lab['defaults']={'mac_aging_seconds':2,'arp_aging_seconds':2,
        'lldp':{'failure_policy':'ttl','ttl_seconds':2,'tx_interval_seconds':1}}
    core=Controller(tmp_path/'s.db',clock);core.create(lab)
    try:
        initial=conditional(core,'sw-002')['snapshot'];clock.advance(1)
        found=conditional(core,'sw-002',initial)
        assert found['mode']=='full' and found['snapshot']['tables']['lldp']
        core.transact('sw-001',[{'kind':'link','id':'link-1','up':False}],1)
        stale=conditional(core,'sw-002')['snapshot']
        assert stale['tables']['lldp']
        clock.advance(3)
        expired=conditional(core,'sw-002',stale)
        assert expired['mode']=='full' and not expired['snapshot']['tables']['lldp']
        assert not any(row['mac']==lab['hosts'][0]['mac'] for row in expired['snapshot']['tables']['fdb'])
    finally:core.store.close()


def test_control_restart_forces_full_handshake(tmp_path):
    core=Controller(tmp_path/'s.db');core.create(generate_lab(1,4));first=conditional(core,'sw-001')['snapshot']
    core.checkpoint();core.store.close();again=Controller(tmp_path/'s.db')
    try:
        reply=conditional(again,'sw-001',first)
        assert reply['mode']=='full' and reply['control_epoch']!=first['control_epoch']
    finally:again.store.close()


async def test_client_heartbeat_preserves_runtime_and_limits_bytes(tmp_path,raw_lab):
    runtime=Runtime(tmp_path,model_only=True);runtime.controller.create(raw_lab);await runtime.start()
    client=RpcClient(runtime.socket,'sw-a')
    try:
        await client.refresh();runtime_object=client.current['runtime'];bytes_full=client.response_bytes
        await client.refresh()
        assert client.current['runtime'] is runtime_object and client.sync_full==1 and client.sync_not_modified==1
        assert client.response_bytes-bytes_full<700
        await client.transact([{'kind':'interface','port':'p1','fields':{'admin':False}}])
        assert client.sync_full==2 and not client.current['device']['interfaces']['p1']['oper']
    finally:await runtime.close()


def test_preview_zero_write_replace_identity_startup_and_restart(tmp_path,raw_lab):
    core=Controller(tmp_path/'s.db');core.create(raw_lab)
    old=core.snapshot('sw-a');config=export_lab(core.labs['local-demo']['lab']);config['devices'][0]['hostname']='NEW'
    try:
        before=json.dumps(core.store.load(),sort_keys=True)
        preview=core.preview_lab('local-demo',config,1)
        assert preview['modified_devices']==['sw-a'] and core.snapshot('sw-a')['revision']==1
        assert json.dumps(core.store.load(),sort_keys=True)==before
        result=core.replace_lab('local-demo',config,1)
        assert result['revision']==2 and core.snapshot('sw-a')['device']['hostname']=='NEW'
        assert core.snapshot('sw-a')['runtime']['engine_id']==old['runtime']['engine_id']
        assert core.labs['local-demo']['startup']['sw-a']['hostname']=='IDC-DEMO'
        core.replace_lab('local-demo',config,2,True)
        assert core.labs['local-demo']['startup']['sw-a']['hostname']=='NEW'
        core.store.close();core=Controller(tmp_path/'s.db')
        assert core.snapshot('sw-a')['device']['hostname']=='NEW'
        invalid=deepcopy(config);invalid['hosts'][0]['interface']='missing'
        with pytest.raises(SimulationError):core.replace_lab('local-demo',invalid,3)
        assert core.snapshot('sw-a')['revision']==3
        core.lab_status('local-demo','running')
        with pytest.raises(Conflict):core.replace_lab('local-demo',config,3)
    finally:core.store.close()


def test_removed_devices_do_not_return_after_reload(tmp_path):
    core=Controller(tmp_path/'s.db');core.create(generate_lab(2,4))
    smaller=generate_lab(1,4)
    try:
        result=core.replace_lab('idc-scale',smaller,1)
        assert result['removed_devices']==['sw-002']
        core.store.close();core=Controller(tmp_path/'s.db')
        assert set(core.labs['idc-scale']['lab']['devices'])=={'sw-001'}
    finally:core.store.close()


async def test_delete_cancels_scenario_cleans_records_and_recreate_skips_old_outbox(tmp_path,raw_lab):
    runtime=Runtime(tmp_path,model_only=True);runtime.controller.create(raw_lab);await runtime.start()
    runtime.controller.inject_notifications('local-demo',['sw-a'],2)
    runtime.controller.transact('sw-a',[{'kind':'save'}],1,'old-operation')
    scenario=await runtime.scenarios.start_async('future',{'schema_version':1,'steps':[
        {'device':'sw-a','at':60,'operations':[{'kind':'save'}]}]})
    await asyncio.sleep(.01)
    channel=await runtime.worker_channel('sw-a','query')
    client=TestClient(TestServer(runtime.app()));await client.start_server()
    try:
        assert (await client.delete('/v1/labs/local-demo',headers={'If-Match':'"1"'})).status==409
        reply=await client.delete('/v1/labs/local-demo',headers={'If-Match':'"2"'})
        assert reply.status==200 and not runtime.controller.labs
        assert scenario['status']=='cancelled' and not channel.path.exists()
        assert not runtime.controller.store.operation('sw-a:old-operation')
        assert not runtime.controller.store.load()
        assert any(event['type']=='labDeleted' for event in runtime.controller.store.events())
        runtime.controller.create(raw_lab)
        floor=runtime.controller.store.cursor('sw-a:query')
        assert not runtime.controller.store.pending_events('sw-a','query',floor)
    finally:await client.close();await runtime.close()


async def test_replace_and_preview_api_and_failed_stop_keeps_lab(tmp_path,raw_lab):
    from unittest.mock import AsyncMock
    runtime=Runtime(tmp_path,model_only=True);runtime.controller.create(raw_lab)
    client=TestClient(TestServer(runtime.app()));await client.start_server()
    try:
        config=deepcopy(raw_lab);config['devices'][0]['hostname']='API'
        headers={'If-Match':'"1"'};body={'topology':config}
        assert (await client.post('/v1/labs/local-demo/config/preview',json=body,headers=headers)).status==200
        assert (await client.put('/v1/labs/local-demo/config',json=body,headers=headers)).status==200
        runtime.supervisor.stop=AsyncMock(side_effect=SimulationError('network cleanup failed'))
        assert (await client.delete('/v1/labs/local-demo',headers={'If-Match':'"2"'})).status==400
        assert runtime.controller.labs['local-demo']['status']=='degraded' and not runtime.controller.busy_labs
    finally:await client.close();await runtime.close()


async def test_namespace_stop_failure_preserves_owner_and_retry(tmp_path, raw_lab, monkeypatch):
    from idc_simulator.deploy import names
    runtime = Runtime(tmp_path, model_only=True)
    runtime.controller.create(raw_lab)
    runtime.supervisor.mode = 'namespace'
    owner = tmp_path/'local-demo'/'network-owner.json'
    owner.parent.mkdir(parents=True)
    owner.write_text(json.dumps({'lab_id':'local-demo','devices':['sw-a']}))
    remains = True

    async def command(argv, tolerate=False):
        if argv[:3] == ['ip','link','show']:
            return (0 if remains else 1), ''
        if argv == ['ip','netns','list']:
            return 0, names('local-demo','sw-a')['namespace'] if remains else ''
        return 0, ''

    monkeypatch.setattr('idc_simulator.deploy.run_command', command)
    try:
        with pytest.raises(SimulationError, match='remain'):
            await runtime.supervisor.stop('local-demo')
        assert owner.exists() and runtime.controller.labs['local-demo']['status'] == 'degraded'
        remains = False
        await runtime.supervisor.stop('local-demo')
        assert not owner.exists() and runtime.controller.labs['local-demo']['status'] == 'stopped'
    finally:
        await runtime.close()


def test_replace_database_failure_rolls_back_then_retry(tmp_path, raw_lab):
    import sqlite3
    core = Controller(tmp_path/'s.db'); core.create(raw_lab)
    config = export_lab(core.labs['local-demo']['lab']);config['devices'][0]['hostname']='NEW'
    config['defaults']['mac_aging_seconds'] = 90
    try:
        core.store.db.execute("CREATE TRIGGER fail_replace BEFORE INSERT ON events WHEN NEW.type='labReconfigured' BEGIN SELECT RAISE(ABORT,'injected failure'); END")
        with pytest.raises(sqlite3.IntegrityError):
            core.replace_lab('local-demo',config,1)
        loaded = core.store.load()['local-demo']
        assert loaded['revision'] == 1 and loaded['lab']['devices']['sw-a']['hostname'] == 'IDC-DEMO'
        core.store.db.execute('DROP TRIGGER fail_replace')
        core.replace_lab('local-demo',config,1)
        assert core.store.load()['local-demo']['lab']['defaults']['mac_aging_seconds'] == 90
    finally:
        core.store.close()


async def test_replace_cancels_retained_device_scenario_and_blocks_new_writes(tmp_path,raw_lab):
    runtime = Runtime(tmp_path,model_only=True);runtime.controller.create(raw_lab)
    spec = {'schema_version':1,'steps':[{'at':60,'device':'sw-a','operations':[{'kind':'system','fields':{'hostname':'LATE'}}]}]}
    runtime.scenarios.start('pending',spec,'pending-replace')
    config = export_lab(runtime.controller.labs['local-demo']['lab']);config['devices'][0]['hostname']='NEW'
    original = runtime.close_channels

    async def close_channels(devices):
        with pytest.raises(Conflict):
            runtime.scenarios.prepare('other',spec)
        with pytest.raises(Conflict):
            runtime.controller.transact('sw-a',[{'kind':'save'}],1)
        await original(devices)

    runtime.close_channels = close_channels
    client = TestClient(TestServer(runtime.app()));await client.start_server()
    try:
        reply = await client.put('/v1/labs/local-demo/config',headers={'If-Match':'1'},json={'topology':config})
        assert reply.status == 200
        assert runtime.scenarios.tasks['pending-replace'].done()
        assert runtime.scenarios.runs['pending-replace']['status'] == 'cancelled'
        assert runtime.controller.snapshot('sw-a')['device']['hostname'] == 'NEW'
    finally:
        await client.close();await runtime.close()


def test_readded_device_starts_after_historical_outbox(tmp_path):
    core=Controller(tmp_path/'s.db');core.create(generate_lab(2,4))
    try:
        core.transact('sw-002',[{'kind':'trap','type':'coldStart'}],1)
        floor=core.store.latest_event_id('sw-002')
        smaller=generate_lab(1,4);core.replace_lab('idc-scale',smaller,2)
        core.replace_lab('idc-scale',generate_lab(2,4),3)
        assert core.store.cursor('sw-002:query') == floor
        assert core.store.cursor('sw-002:notification') == floor
    finally:core.store.close()
