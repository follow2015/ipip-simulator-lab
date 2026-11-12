"""Declarative lab comparison and stopped-lab replacement drafts."""
from copy import deepcopy
from idc_simulator.errors import Conflict, NotFound, SimulationError
from idc_simulator.model import normalize_lab, stable_hex
from idc_simulator.topology import derive, settle_counter


def export_lab(lab):
    result = deepcopy(lab)
    result['devices'] = list(result['devices'].values())
    for device in result['devices']:
        device['interfaces'] = list(device['interfaces'].values())
        for port in device['interfaces'] + device['l3_interfaces']:
            port.pop('oper', None)
    return result


def comparable(lab):
    return export_lab(lab)


def checked_lab(controller, lab_id, raw, revision):
    old = controller.labs.get(lab_id)
    if old is None:
        raise NotFound('Unknown lab')
    if old['revision'] != revision:
        raise Conflict('Lab revision changed; reload before replacing configuration')
    lab = normalize_lab(raw)
    if lab['name'] != lab_id:
        raise SimulationError('Replacement topology must keep the lab name')
    from idc_simulator.deploy import network_plan
    network_plan(lab)
    for name, state in controller.labs.items():
        if name == lab_id:
            continue
        if set(lab['devices']) & set(state['lab']['devices']):
            raise Conflict('Device IDs must be globally unique')
        addresses = {device['management_ip'] for device in state['lab']['devices'].values()}
        if addresses & {device['management_ip'] for device in lab['devices'].values()}:
            raise Conflict('Management IPs must be globally unique')
    return old, lab


def replacement(controller, lab_id, raw, revision, save_startup=False):
    if type(save_startup) is not bool:
        raise SimulationError('save_startup must be boolean')
    old, lab = checked_lab(controller, lab_id, raw, revision)
    before = {d['id']: d for d in comparable(old['lab'])['devices']}
    after = {d['id']: d for d in comparable(lab)['devices']}
    added = sorted(after.keys() - before.keys())
    removed = sorted(before.keys() - after.keys())
    modified = sorted(dev for dev in before.keys() & after.keys() if before[dev] != after[dev])
    now = controller.clock()
    state = deepcopy({key: value for key, value in old.items() if key not in {'tables', '_derived_plan'}})
    state['lab'] = lab
    state['runtime'] = {dev: runtime for dev, runtime in state['runtime'].items() if dev in after}
    state['startup'] = {dev: startup for dev, startup in state['startup'].items() if dev in after}
    for dev, device in lab['devices'].items():
        if dev not in state['runtime']:
            state['runtime'][dev] = {'engine_id': '80004fb805'+stable_hex(lab['seed'], lab_id+':'+dev, 22),
                'engine_boots': 0, 'boot_ns': now, 'online': True, 'counters': {}, 'last_change': {}}
        runtime = state['runtime'][dev]
        for counter in runtime.get('counters', {}).values():
            settle_counter(counter, now)
        runtime['counters'] = {port: counter for port, counter in runtime.get('counters', {}).items() if port in device['interfaces']}
        for key in ('lldp_cache', 'fdb_cache', 'arp_cache', 'lldp_since'):
            runtime[key] = {}
        startup_changed = save_startup and state['startup'].get(dev) != device
        if save_startup or dev not in state['startup']:
            state['startup'][dev] = deepcopy(device)
        if dev in added or dev in modified or startup_changed:
            runtime['config_revision'] = revision+1
    for key in ('_state_sequences', '_state_signatures'):
        state[key] = {dev: value for dev, value in state.get(key, {}).items() if dev in after}
    state['revision'] = revision+1
    state['topology_revision'] = revision+1
    derive(state, now)
    summary = {'lab': lab_id, 'revision': revision, 'next_revision': revision+1,
        'added_devices': added, 'removed_devices': removed, 'modified_devices': modified,
        'links_changed': old['lab']['links'] != lab['links'], 'hosts_changed': old['lab']['hosts'] != lab['hosts'],
        'identity_changes': [dev for dev in modified if any(before[dev].get(key) != after[dev].get(key)
                            for key in ('management_ip', 'profile', 'chassis_mac'))], 'save_startup': save_startup}
    return state, summary
