"""Protocol-visible state generations, independent of clocks and counters."""
from hashlib import sha256
import json
import os


def semantic_header(state, dev):
    runtime = state["runtime"][dev]
    return {
        "device": state["lab"]["devices"][dev],
        "identity": {key: runtime.get(key) for key in (
            "engine_id", "engine_boots", "engine_start_ns", "boot_ns", "online",
            "last_change", "alarms", "counter_generation")},
        "rates": {port: {key: value for key, value in counter.items()
                         if key.endswith("_rate") or key in {"discontinuity", "packet_denominator"}}
                  for port, counter in runtime.get("counters", {}).items()},
    }


def semantic_state(state, dev):
    return {**semantic_header(state, dev),
        "tables": {kind: [{key: value for key, value in row.items() if key != "expires_ns"}
                          for row in rows] for kind, rows in state["tables"][dev].items()}}


def _row_equal(left, right):
    if left == right:
        return True
    return left.keys() == right.keys() and all(
        left[key] == value for key, value in right.items() if key != "expires_ns")


def _row_identity(row):
    return json.dumps({key: value for key, value in row.items() if key != "expires_ns"},
                      sort_keys=True, separators=(",", ":"), default=str)


_KEY_CANDIDATES = (
    ("id",), ("fdb_id", "mac"), ("arp_id",), ("lldp_id",), ("local_port",),
    ("vrf", "ip"), ("vlan", "mac"), ("mac", "ifindex"), ("mac",), ("ip",),
)


def _keyed(rows, fields):
    table = {}
    for row in rows:
        if any(field not in row for field in fields):
            return None
        key = tuple(row[field] for field in fields)
        if key in table:
            return None
        table[key] = row
    return table


def _aligned_equal(old, rows):
    for fields in _KEY_CANDIDATES:
        before, after = _keyed(old, fields), _keyed(rows, fields)
        if before is not None and after is not None and before.keys() == after.keys():
            return all(_row_equal(before[key], after[key]) for key in before)
    return sorted(_row_identity(row) for row in old) == sorted(_row_identity(row) for row in rows)


def same_tables(before, after):
    if before.keys() != after.keys():
        return False
    for kind, rows in after.items():
        old = before[kind]
        if len(old) != len(rows):
            return False
        if all(_row_equal(left, right) for left, right in zip(old, rows)):
            continue
        if not _aligned_equal(old, rows):
            return False
    return True


def signature_mode():
    mode = os.getenv("IDC_SIGNATURE_MODE", "incremental")
    if mode not in {"incremental", "full", "verify"}:
        raise ValueError("IDC_SIGNATURE_MODE must be incremental, full or verify")
    return mode


def publish_state(state, devices=None):
    signatures = state.setdefault("_state_signatures", {})
    sequences = state.setdefault("_state_sequences", {})
    mode = signature_mode()
    all_devices = state["lab"]["devices"]
    dirty = set(all_devices if devices is None else devices)
    dirty.update(dev for dev in all_devices if dev not in signatures)
    def digest(dev):
        return sha256(json.dumps(semantic_state(state, dev), sort_keys=True,
                                separators=(",", ":"), allow_nan=False).encode()).digest()
    calculated = {dev: digest(dev) for dev in (all_devices if mode in {"full", "verify"} else dirty)}
    if mode == "verify":
        missed = [dev for dev in all_devices if dev not in dirty and signatures.get(dev) != calculated[dev]]
        if missed:
            state["_signature_verification_failures"] = state.get("_signature_verification_failures", 0)+1
            raise AssertionError(f"Untracked semantic state change: {', '.join(missed)}")
    for dev, signature in calculated.items():
        if signatures.get(dev) != signature:
            sequences[dev] = sequences.get(dev, 0) + 1
            signatures[dev] = signature
