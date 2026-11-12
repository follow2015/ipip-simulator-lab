"""Post-v1 normal operation, timeout recovery and semantic consistency."""
import asyncio
from copy import deepcopy
import json
import random
import threading

from aiohttp.test_utils import TestClient, TestServer
import pytest

from idc_simulator.control import Controller
from idc_simulator.deploy import names, naming_version, network_plan
from idc_simulator.errors import Unavailable
from idc_simulator.lifecycle import export_lab
from idc_simulator.runtime import Runtime
from idc_simulator.synchronization import publish_state
from idc_simulator.tools import generate_lab


async def test_configured_https_origin_can_create_lab(tmp_path):
    runtime = Runtime(tmp_path, model_only=True)
    client = TestClient(TestServer(runtime.app(allowed_hosts=['sim.example'], allowed_origins=['https://sim.example'])))
    await client.start_server()
    try:
        reply = await client.post('/v1/labs', json=generate_lab(1,4),
                                  headers={'Host':'sim.example', 'Origin':'https://sim.example'})
        assert reply.status == 201
    finally:
        await client.close()
        await runtime.close()


async def test_queued_timeout_is_not_executed(tmp_path):
    runtime = Runtime(tmp_path, model_only=True)
    entered, release = threading.Event(), threading.Event()
    writes = []
    def occupy():
        entered.set()
        release.wait(5)
    blocker = asyncio.create_task(runtime.state_call(occupy, _state_wait=True))
    try:
        await asyncio.to_thread(entered.wait, 2)
        runtime.state_timeout = .03
        with pytest.raises(Unavailable) as caught:
            await runtime.state_call(writes.append, 'late')
        assert caught.value.outcome == 'not_executed'
        release.set()
        await blocker
        assert not writes
    finally:
        release.set()
        await blocker
        runtime.state_timeout = 30
        await runtime.close()


async def test_function_timeout_error_is_not_wait_timeout(tmp_path):
    runtime = Runtime(tmp_path, model_only=True)
    def fails():
        raise TimeoutError('downstream timeout')
    try:
        with pytest.raises(TimeoutError, match='downstream timeout'):
            await runtime.state_call(fails)
        assert runtime.state_timeout_function is None
        assert not runtime.metrics.counts['state_call_timeouts_total']
        assert await runtime.state_call(lambda: 'ok') == 'ok'
    finally:
        await runtime.close()


async def test_running_timeout_keeps_admission_slot_until_completion(tmp_path):
    runtime = Runtime(tmp_path, model_only=True)
    runtime.state_slots = asyncio.Semaphore(1)
    runtime.state_timeout = .03
    release = threading.Event()
    try:
        with pytest.raises(Unavailable) as caught:
            await runtime.state_call(release.wait, 5)
        assert caught.value.outcome == 'unknown' and runtime.state_slots.locked()
        release.set()
        await asyncio.wrap_future(runtime.executor.submit(lambda: None))
        await asyncio.sleep(0)
        assert not runtime.state_slots.locked()
        runtime.state_timeout = 30
        await runtime.state_call(lambda: None, _state_wait=True)
    finally:
        release.set()
        runtime.state_timeout = 30
        await runtime.close()


async def test_lifecycle_function_timeout_error_does_not_leave_task(tmp_path):
    runtime = Runtime(tmp_path, model_only=True)
    async def fails():
        raise TimeoutError('lifecycle failure')
    try:
        with pytest.raises(TimeoutError, match='lifecycle failure'):
            await runtime.lifecycle(fails())
        assert not runtime.lifecycle_tasks
        assert not runtime.metrics.counts['lifecycle_timeouts_total']
    finally:
        await runtime.close()


async def test_expected_lifecycle_client_errors_are_not_server_failures(tmp_path, caplog):
    from idc_simulator.errors import NotFound
    runtime = Runtime(tmp_path, model_only=True)
    async def missing():
        raise NotFound('Unknown lab')
    try:
        with pytest.raises(NotFound):
            await runtime.lifecycle(missing())
        await asyncio.sleep(0)
        assert not runtime.metrics.counts['lifecycle_task_errors_total']
        assert 'Background lifecycle task rejected' in caplog.text
        assert 'ERROR' not in caplog.text
    finally:
        await runtime.close()


async def test_running_transaction_returns_queryable_id_and_retry(tmp_path, raw_lab, monkeypatch):
    runtime = Runtime(tmp_path, model_only=True)
    runtime.controller.create(raw_lab)
    entered, release = threading.Event(), threading.Event()
    original = runtime.controller.transact
    def transact(*args, **kwargs):
        entered.set()
        release.wait(5)
        return original(*args, **kwargs)
    monkeypatch.setattr(runtime.controller, 'transact', transact)
    client = TestClient(TestServer(runtime.app()))
    await client.start_server()
    runtime.state_timeout = .05
    try:
        response = await client.patch('/v1/devices/sw-a/interfaces/p1', json={'description':'updated'}, headers={'If-Match':'1'})
        body = await response.json()
        assert entered.is_set() and response.status == 503
        assert body['outcome'] == 'unknown' and response.headers['Retry-After']
        operation_id = body['operation_id']
        release.set()
        runtime.state_timeout = 30
        await runtime.state_call(lambda: None, _state_wait=True)
        result = await (await client.get('/v1/operations/'+operation_id)).json()
        assert result['status'] == 'complete' and result['revision'] == 2
        retry = await client.patch('/v1/devices/sw-a/interfaces/p1', json={'description':'updated'},
                                   headers={'If-Match':'1', 'Idempotency-Key':operation_id})
        assert retry.status == 200 and await retry.json() == result
    finally:
        release.set()
        runtime.state_timeout = 30
        await client.close()
        await runtime.close()


@pytest.mark.parametrize('action', ['delete', 'replace', 'prepare_replace'])
async def test_lifecycle_timeout_finishes_and_releases_gate(tmp_path, raw_lab, monkeypatch, action):
    runtime = Runtime(tmp_path, model_only=True)
    runtime.controller.create(raw_lab)
    entered, release = threading.Event(), threading.Event()
    method = {'delete':'delete_lab', 'replace':'replace_lab', 'prepare_replace':'preview_lab'}[action]
    original = getattr(runtime.controller, method)
    def slow(*args, **kwargs):
        entered.set()
        release.wait(5)
        return original(*args, **kwargs)
    monkeypatch.setattr(runtime.controller, method, slow)
    client = TestClient(TestServer(runtime.app()))
    await client.start_server()
    runtime.lifecycle_timeout = .05
    try:
        if action == 'delete':
            response = await client.delete('/v1/labs/local-demo', headers={'If-Match':'1'})
        else:
            config = deepcopy(raw_lab)
            config['devices'][0]['hostname'] = 'REPLACED'
            response = await client.put('/v1/labs/local-demo/config', json={'topology':config}, headers={'If-Match':'1'})
        assert response.status == 503 and (await response.json())['outcome'] == 'unknown'
        assert entered.is_set() and runtime.lifecycle_tasks
        release.set()
        runtime.state_timeout = 30
        await asyncio.gather(*runtime.lifecycle_tasks)
        assert not runtime.controller.busy_labs
        if action == 'delete':
            assert not runtime.controller.labs
            await runtime.state_call(runtime.controller.create, raw_lab)
        else:
            assert runtime.controller.snapshot('sw-a')['device']['hostname'] == 'REPLACED'
        revision = runtime.controller.labs['local-demo']['revision']
        result = await runtime.state_call(runtime.controller.transact, 'sw-a', [{'kind':'save'}], revision)
        assert result['status'] == 'complete'
    finally:
        release.set()
        runtime.state_timeout = 30
        await client.close()
        await runtime.close()


def test_randomized_semantic_changes_match_full_signatures(tmp_path, clock, monkeypatch):
    monkeypatch.setenv('IDC_SIGNATURE_MODE', 'verify')
    raw = generate_lab(6, 8)
    raw['defaults'] = {'mac_aging_seconds':2, 'arp_aging_seconds':2,
                       'lldp':{'failure_policy':'ttl', 'ttl_seconds':2, 'tx_interval_seconds':1}}
    core = Controller(tmp_path/'s.db', clock)
    core.create(raw)
    rng = random.Random(20261110)
    try:
        for _ in range(120):
            dev = rng.choice(list(core.labs['idc-scale']['lab']['devices']))
            choice = rng.randrange(7)
            operation = [
                {'kind':'interface', 'port':'p1', 'fields':{'admin':rng.choice([True,False])}},
                {'kind':'link', 'id':rng.choice(raw['links'])['id'], 'up':rng.choice([True,False])},
                {'kind':'host', 'id':'host-1', 'fields':{'device':dev, 'interface':'p2'}},
                {'kind':'online', 'value':rng.choice([True,False])},
                {'kind':'system', 'fields':{'hostname':str(rng.randrange(1000))}},
                {'kind':'counter_reset'},
                {'kind':'flow', 'id':'baseline', 'rate_bps':rng.randrange(1000)*1000},
            ][choice]
            core.transact(dev, [operation], core.labs['idc-scale']['revision'])
            clock.advance(rng.choice([.1,1,3]))
            core.snapshot(dev)
        config = export_lab(core.labs['idc-scale']['lab'])
        core.replace_lab('idc-scale', config, core.labs['idc-scale']['revision'])
        assert not core.labs['idc-scale'].get('_signature_verification_failures')
    finally:
        core.store.close()


def test_device_transaction_draft_shares_untouched_collections_and_rolls_back(tmp_path, raw_lab):
    core = Controller(tmp_path/'cow.db')
    lab = generate_lab(2, 4)
    core.create(lab)
    state = core.labs[lab['name']]
    dev, peer = list(state['lab']['devices'])
    original_peer = state['lab']['devices'][peer]
    original_startup = state['startup'][dev]
    original_runtime = state['runtime'][dev]
    try:
        draft = core._transaction_draft(state, dev, [{'kind':'interface'}])
        assert draft['lab']['links'] is state['lab']['links']
        assert draft['lab']['devices'][peer]['interfaces']['p1'] is not original_peer['interfaces']['p1']
        assert draft['startup'][dev] is original_startup
        assert draft['runtime'][dev] is not original_runtime
        before = core.snapshot(dev)['device']['interfaces']['p1']['description']
        with pytest.raises(Exception):
            core.transact(dev, [{'kind':'interface','port':'p1','fields':{'description':'draft'}},
                                {'kind':'unsupported'}], state['revision'])
        assert core.snapshot(dev)['device']['interfaces']['p1']['description'] == before
    finally:
        core.store.close()


def test_lag_member_changes_verify(tmp_path, monkeypatch):
    from idc_simulator.model import load_lab
    monkeypatch.setenv('IDC_SIGNATURE_MODE', 'verify')
    core = Controller(tmp_path/'s.db')
    raw = load_lab('examples/eth-trunk.yaml')
    core.create(export_lab(raw))
    try:
        state = core.labs[raw['name']]
        for dev, device in state['lab']['devices'].items():
            for port, interface in device['interfaces'].items():
                if interface.get('members'):
                    core.transact(dev, [{'kind':'lag', 'port':port, 'fields':{'min_links':2}}], state['revision'])
                    return
        pytest.fail('fixture has no LAG')
    finally:
        core.store.close()


def test_verify_detects_omitted_dirty_device(controller, monkeypatch):
    monkeypatch.setenv('IDC_SIGNATURE_MODE', 'verify')
    state = controller.labs['local-demo']
    state['lab']['devices']['sw-a']['hostname'] = 'untracked'
    sequences = dict(state['_state_sequences'])
    with pytest.raises(AssertionError, match='Untracked'):
        publish_state(state, set())
    assert state['_state_sequences'] == sequences
    assert state['_signature_verification_failures'] == 1


def test_namespace_collision_uses_extended_version():
    from hashlib import sha256
    seen = {}
    for number in range(10000):
        dev = f'collision-{number}'
        suffix = sha256(dev.encode()).hexdigest()[:5]
        if suffix in seen:
            pair = [seen[suffix], dev]
            break
        seen[suffix] = dev
    else:
        pytest.fail('no deterministic collision found')
    raw = generate_lab(2,4)
    lab = {**raw, 'devices':dict(zip(pair,raw['devices']))}
    assert naming_version(lab) == 2
    plan = network_plan(lab)
    namespaces = [command[3] for command in plan if command[:3] == ['ip','netns','add']]
    assert len(set(namespaces)) == 2
    assert all(len(namespace.rsplit('-',1)[1]) == 8 for namespace in namespaces)
    cleanup = network_plan(lab, remove=True, version=2)
    assert [command[3] for command in cleanup[:-1]] == namespaces
    single = {**lab, 'devices':{pair[0]:raw['devices'][0]}}
    assert naming_version(single) == 1
    assert network_plan(single, remove=True, version=1)[0][3] == names(lab['name'],pair[0])['namespace']


async def test_normal_worker_metrics_and_private_channel_lifecycle(tmp_path):
    runtime = Runtime(tmp_path, model_only=True)
    channel = await runtime.worker_channel('sw-a','query')
    assert channel.path.parent == runtime.socket_directory
    assert channel.path.stat().st_mode & 0o777 == 0o600
    directory = tmp_path/'local-demo'/'sw-a'
    directory.mkdir(parents=True)
    (directory/'metrics-query.prom').write_text('idc_state_sync_full_total 3\nidc_state_sync_seconds{quantile="0.5"} 0.02\n')
    client = TestClient(TestServer(runtime.app()))
    await client.start_server()
    try:
        response = await client.get('/metrics')
        text = await response.text()
        assert response.status == 200
        assert 'idc_state_sync_full_total{device="sw-a",role="query"} 3' in text
        assert 'role="query",quantile="0.5"} 0.02' in text
    finally:
        await client.close()
        await runtime.close()
    assert not channel.path.exists()


async def test_long_state_path_uses_short_socket_directory_and_cleans_it(tmp_path):
    runtime = Runtime(tmp_path/('long-state-path-'*12), model_only=True)
    await runtime.start()
    channel = await runtime.worker_channel('sw-a','query')
    temporary = runtime.temporary_socket_directory
    try:
        assert temporary is not None and channel.path.parent == temporary
        assert len(bytes(channel.path)) <= 100
        assert runtime.owner_lock is not None
    finally:
        await runtime.close()
    assert not temporary.exists()


async def test_shutdown_waits_for_checkpoint_beyond_response_timeout(tmp_path):
    runtime = Runtime(tmp_path, model_only=True)
    runtime.state_timeout = .01
    original = runtime.controller.checkpoint
    def checkpoint():
        import time
        time.sleep(.04)
        return original()
    runtime.controller.checkpoint = checkpoint
    await runtime.close()
    assert not runtime.metrics.counts['state_call_timeouts_total']


async def test_state_directory_lock_survives_repeated_start_and_releases(tmp_path):
    from idc_simulator.errors import SimulationError
    runtime = Runtime(tmp_path, model_only=True)
    other = Runtime(tmp_path, model_only=True)
    await runtime.start()
    lock = runtime.owner_lock
    try:
        await runtime.start()
        assert runtime.owner_lock == lock
        with pytest.raises(SimulationError, match='already in use'):
            await other.start()
        assert other.owner_lock is None
    finally:
        await runtime.close()
    try:
        await other.start()
        assert other.owner_lock is not None
    finally:
        await other.close()


async def test_recorded_namespace_version_and_devices_used_for_cleanup(tmp_path, monkeypatch):
    runtime = Runtime(tmp_path, model_only=True)
    runtime.controller.create(generate_lab(1,4))
    runtime.supervisor.mode = 'namespace'
    owner = tmp_path/'idc-scale'/'network-owner.json'
    owner.parent.mkdir()
    owner.write_text(json.dumps({'lab_id':'idc-scale','devices':['old-device'], 'naming_version':2}))
    commands = []
    async def command(argv, tolerate=False):
        commands.append(argv)
        return (1 if argv[:3] == ['ip','link','show'] else 0), ''
    monkeypatch.setattr('idc_simulator.deploy.run_command', command)
    try:
        await runtime.supervisor.stop('idc-scale')
        assert ['ip','netns','delete',names('idc-scale','old-device',2)['namespace']] in commands
        assert not owner.exists()
    finally:
        await runtime.close()
