"""v4/v5 评审遗留项回归：子网预留、host id 校验、事务草稿隔离与 MAC 派生。"""
import json
from copy import deepcopy

import pytest
from aiohttp.test_utils import TestClient, TestServer

from idc_simulator.control import Controller
from idc_simulator.errors import SimulationError, NotFound
from idc_simulator.model import normalize_lab, mac_bytes
from idc_simulator.runtime import Runtime
from idc_simulator.tools import generate_lab



async def test_generated_subnets_are_reserved_before_create(tmp_path):
    runtime = Runtime(tmp_path, model_only=True)
    client = TestClient(TestServer(runtime.app()))
    await client.start_server()
    try:
        a = await (await client.get('/v1/templates/generated',
                                    params={'name': 'lab-36', 'devices': 1, 'ports': 4})).json()
        b = await (await client.get('/v1/templates/generated',
                                    params={'name': 'lab-87', 'devices': 1, 'ports': 4})).json()
        assert a['management']['subnet'] != b['management']['subnet']
        assert (await client.post('/v1/labs', json=a)).status == 201
        assert (await client.post('/v1/labs', json=b)).status == 201
    finally:
        await client.close(); await runtime.close()


async def test_generated_subnet_reservation_is_idempotent_and_expires(tmp_path):
    from idc_simulator.runtime import GENERATED_SUBNET_RESERVATION_NS
    runtime = Runtime(tmp_path, model_only=True)
    now = 1_000_000_000
    first = await runtime.state_call(runtime.available_generated_subnet, 'lab-36', False, now=now)
    again = await runtime.state_call(runtime.available_generated_subnet, 'lab-36', False, now=now)
    assert first == again
    other = await runtime.state_call(runtime.available_generated_subnet, 'lab-87', False, now=now)
    assert other != first
    later = now + GENERATED_SUBNET_RESERVATION_NS + 1
    after = await runtime.state_call(runtime.available_generated_subnet, 'lab-87', False, now=later)
    assert after == first
    assert after != other
    await runtime.close()


async def test_generated_subnet_reservation_does_not_leak_into_unrelated_names(tmp_path):
    runtime = Runtime(tmp_path, model_only=True)
    picks = []
    for i in range(30):
        picks.append(await runtime.state_call(runtime.available_generated_subnet, f'bulk-{i}', False))
    assert len(set(picks)) == len(picks)
    await runtime.close()



@pytest.mark.parametrize('bad', ['bad/id', 'bad id', 'x' * 49, 'a:b', 'slashed\\name'])
def test_invalid_host_id_is_rejected(bad):
    raw = generate_lab(2, 4)
    raw['hosts'][0]['id'] = bad
    raw['flows'] = []
    with pytest.raises(SimulationError, match='Invalid host ID'):
        normalize_lab(raw)


def test_generated_host_ids_pass_validation():
    raw = generate_lab(3, 4)
    lab = normalize_lab(raw)
    assert {h['id'] for h in lab['hosts']} == {'host-1', 'host-2', 'host-3'}



def test_generated_host_mac_is_valid_and_unique_beyond_64k():
    from idc_simulator.runtime import generated_host_mac
    identity = 'a' * 64
    macs = [generated_host_mac(identity, i) for i in (1, 2, 65535, 65536, 70000, 131072)]
    assert len(set(macs)) == len(macs)
    for mac in macs:
        mac_bytes(mac)


async def test_generated_topology_host_macs_are_unique(tmp_path):
    runtime = Runtime(tmp_path, model_only=True)
    client = TestClient(TestServer(runtime.app()))
    await client.start_server()
    try:
        config = await (await client.get('/v1/templates/generated',
                                         params={'name': 'mac-scale', 'devices': 12, 'ports': 4})).json()
        macs = [h['mac'] for h in config['hosts']]
        assert len(set(macs)) == len(macs)
        for mac in macs:
            mac_bytes(mac)
    finally:
        await client.close(); await runtime.close()



def _snapshot(state):
    return json.dumps(state, sort_keys=True, default=str)


def test_state_comparison_covers_internal_signature_keys(tmp_path):
    core = Controller(tmp_path / 'state.db', clock=lambda: 1_000_000_000)
    core.create(generate_lab(2, 4))
    state = core.labs['idc-scale']
    before = json.loads(_snapshot(state))
    assert '_state_signatures' in before and '_state_sequences' in before
    state['_state_signatures']['sw-001'] = 'tampered'
    state['_state_sequences']['sw-001'] = 999
    assert _snapshot(state) != json.dumps(before, sort_keys=True, default=str)
    core.store.close()


def test_differential_detects_leaking_signature_containers(tmp_path):
    def leaking_draft(old, dev, operations):
        draft = deepcopy(old)
        draft['_state_signatures'] = old['_state_signatures']
        draft['_state_sequences'] = old['_state_sequences']
        return draft

    core = Controller(tmp_path / 'state.db', clock=lambda: 1_000_000_000)
    core._transaction_draft = staticmethod(leaking_draft)
    core.create(generate_lab(2, 4))
    state = core.labs['idc-scale']
    before = _snapshot(state)
    core.transact('sw-001', [{'kind': 'system', 'fields': {'hostname': 'LEAK'}}],
                  state['revision'], preview=True)
    assert _snapshot(core.labs['idc-scale']) != before, 'internal-key pollution went unnoticed'
    core.store.close()


def test_preview_never_pollutes_live_state(tmp_path):
    clock = lambda: 1_000_000_000
    core = Controller(tmp_path / 'state.db', clock=clock)
    core.create(generate_lab(3, 4))
    old = core.labs['idc-scale']
    before = _snapshot(old)
    result = core.transact('sw-001', [{'kind': 'system', 'fields': {'hostname': 'CHANGED'}}],
                           old['revision'], preview=True)
    assert result['hostname'] == 'CHANGED'
    assert _snapshot(core.labs['idc-scale']) == before
    assert core.labs['idc-scale']['lab']['devices']['sw-001']['hostname'] != 'CHANGED'
    core.store.close()


def test_failed_transaction_never_pollutes_live_state(tmp_path):
    core = Controller(tmp_path / 'state.db', clock=lambda: 1_000_000_000)
    core.create(generate_lab(3, 4))
    old = core.labs['idc-scale']
    before = _snapshot(old)
    with pytest.raises(NotFound):
        core.transact('sw-001', [
            {'kind': 'interface', 'port': 'p1', 'fields': {'description': 'changed'}},
            {'kind': 'interface', 'port': 'no-such-port', 'fields': {'description': 'x'}},
        ], old['revision'])
    assert _snapshot(core.labs['idc-scale']) == before
    core.store.close()


def test_transaction_draft_matches_full_deepcopy(tmp_path):
    clock = lambda: 1_000_000_000

    def full_copy_draft(old, dev, operations):
        return deepcopy(old)

    baseline = Controller(tmp_path / 'baseline.db', clock=clock)
    actual = Controller(tmp_path / 'actual.db', clock=clock)
    baseline._transaction_draft = staticmethod(full_copy_draft)
    raw = generate_lab(4, 6)
    baseline.create(deepcopy(raw))
    actual.create(deepcopy(raw))

    operations = [
        [{'kind': 'system', 'fields': {'hostname': 'renamed'}}],
        [{'kind': 'interface', 'port': 'p1', 'fields': {'description': 'uplink'}}],
        [{'kind': 'counter_reset', 'port': 'p1'}],
        [{'kind': 'online', 'value': False}],
        [{'kind': 'interface', 'port': 'p2', 'fields': {'admin': False}}],
        [{'kind': 'online', 'value': True}],
        [{'kind': 'interface', 'port': 'p1', 'fields': {'description': 'again'}}],
        [{'kind': 'host', 'id': 'host-1', 'fields': {'active': False}}],
        [{'kind': 'host', 'id': 'host-1', 'fields': {'active': True}}],
        [{'kind': 'counter_reset', 'port': 'p2'}],
        [{'kind': 'system', 'fields': {'location': 'rack-1'}}],
    ]
    for i, ops in enumerate(operations):
        rev_b = baseline.labs['idc-scale']['revision']
        rev_a = actual.labs['idc-scale']['revision']
        assert rev_b == rev_a, f'revision diverged at step {i}'
        baseline.transact('sw-001', deepcopy(ops), rev_b)
        actual.transact('sw-001', deepcopy(ops), rev_a)
        assert _snapshot(baseline.labs['idc-scale']) == _snapshot(actual.labs['idc-scale']), f'state diverged at step {i}'

    before_b, before_a = _snapshot(baseline.labs['idc-scale']), _snapshot(actual.labs['idc-scale'])
    baseline.transact('sw-001', [{'kind': 'system', 'fields': {'hostname': 'preview'}}],
                      baseline.labs['idc-scale']['revision'], preview=True)
    actual.transact('sw-001', [{'kind': 'system', 'fields': {'hostname': 'preview'}}],
                    actual.labs['idc-scale']['revision'], preview=True)
    assert _snapshot(baseline.labs['idc-scale']) == before_b
    assert _snapshot(actual.labs['idc-scale']) == before_a
    baseline.store.close(); actual.store.close()


def test_transaction_draft_copies_signatures_and_sequences(tmp_path):
    core = Controller(tmp_path / 'state.db', clock=lambda: 1_000_000_000)
    core.create(generate_lab(2, 4))
    old = core.labs['idc-scale']
    draft = core._transaction_draft(old, 'sw-001', [{'kind': 'system', 'fields': {'hostname': 'x'}}])
    for key in ('_state_signatures', '_state_sequences'):
        assert draft[key] is not old[key], f'{key} shared with live state'
        assert draft[key] == old[key]
    for dev in old['runtime']:
        assert draft['runtime'][dev] is not old['runtime'][dev]
    core.store.close()


def test_randomized_transactions_match_full_copy_across_layouts(tmp_path):
    import random

    def full_copy_draft(old, dev, operations):
        return deepcopy(old)

    kinds = [
        lambda i: [{'kind': 'system', 'fields': {'hostname': f'h{i % 30}'}}],
        lambda i: [{'kind': 'interface', 'port': 'p1', 'fields': {'description': f'd{i}'}}],
        lambda i: [{'kind': 'interface', 'port': 'p2', 'fields': {'admin': bool(i % 2)}}],
        lambda i: [{'kind': 'counter_reset', 'port': 'p1'}],
        lambda i: [{'kind': 'online', 'value': bool(i % 3)}],
        lambda i: [{'kind': 'system', 'fields': {'location': f'rack-{i % 7}'}}],
        lambda i: [{'kind': 'host', 'id': 'host-1', 'fields': {'active': bool(i % 2)}}],
    ]
    for layout, count, ports in [('chain', 8, 8), ('tree', 9, 8), ('ring', 8, 8)]:
        rng = random.Random(20261111)
        structured = Controller(tmp_path / f'{layout}-new.db', clock=lambda: 1_000_000_000)
        reference = Controller(tmp_path / f'{layout}-ref.db', clock=lambda: 1_000_000_000)
        reference._transaction_draft = staticmethod(full_copy_draft)
        raw = generate_lab(count, ports, layout=layout)
        structured.create(deepcopy(raw))
        reference.create(deepcopy(raw))
        for step in range(150):
            dev = f'sw-{rng.randint(1, count):03}'
            ops = rng.choice(kinds)(step)
            before_new = _snapshot(structured.labs['idc-scale'])
            before_ref = _snapshot(reference.labs['idc-scale'])
            outcomes = []
            for core in (structured, reference):
                try:
                    core.transact(dev, deepcopy(ops), core.labs['idc-scale']['revision'])
                    outcomes.append(True)
                except (SimulationError, NotFound, KeyError):
                    outcomes.append(False)
            assert outcomes[0] == outcomes[1], f'{layout} step {step}: outcome mismatch'
            if not outcomes[0]:
                assert _snapshot(structured.labs['idc-scale']) == before_new, f'{layout} step {step}: new draft polluted live state'
                assert _snapshot(reference.labs['idc-scale']) == before_ref, f'{layout} step {step}: reference polluted live state'
            assert _snapshot(structured.labs['idc-scale']) == _snapshot(reference.labs['idc-scale']), \
                f'{layout} step {step}: state divergence'
        structured.store.close(); reference.store.close()
