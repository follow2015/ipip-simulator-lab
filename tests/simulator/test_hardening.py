"""Regression tests for the 2026-11-09 independent review findings (F1..F16)."""
import asyncio
import json
import sqlite3
from pathlib import Path

import pytest
import yaml

from idc_simulator.cli import CliSession, parse_vlans
from idc_simulator.control import Controller
from idc_simulator.deploy import read_ready, write_ready
from idc_simulator.errors import SimulationError
from idc_simulator.model import load_lab, safe_load
from idc_simulator.notifications import NotificationSender
from idc_simulator.runtime import Runtime, is_loopback, prevent_unauthenticated_bind
from idc_simulator.ssh import AuthServer
from idc_simulator.tools import generate_lab

ROOT = Path(__file__).resolve().parents[2]

LEGACY_SCHEMA = """
  CREATE TABLE labs (id TEXT PRIMARY KEY, state TEXT NOT NULL);
  CREATE TABLE events (id INTEGER PRIMARY KEY AUTOINCREMENT, lab_id TEXT NOT NULL, device_id TEXT,
    type TEXT NOT NULL, revision INTEGER NOT NULL, time_ns INTEGER NOT NULL, payload TEXT NOT NULL);
  CREATE TABLE deliveries (id INTEGER PRIMARY KEY AUTOINCREMENT, event_id INTEGER NOT NULL,
    status TEXT NOT NULL, payload TEXT NOT NULL);
  CREATE TABLE operations (id TEXT PRIMARY KEY, result TEXT NOT NULL);
  CREATE TABLE cursors (id TEXT PRIMARY KEY, value INTEGER NOT NULL);
  CREATE TABLE runs (id TEXT PRIMARY KEY, result TEXT NOT NULL);
  PRAGMA user_version=1;
"""


def test_ready_file_is_published_atomically(tmp_path):
    ready = tmp_path/"ready.json"
    write_ready(ready, {"pid": 4321, "device": "sw-a"})
    assert json.loads(ready.read_text())["pid"] == 4321
    assert not (tmp_path/"ready.json.tmp").exists()
    write_ready(ready, {"pid": 4322, "device": "sw-a"})
    assert read_ready(ready)["pid"] == 4322


def test_ready_file_read_tolerates_partial_and_missing_files(tmp_path):
    ready = tmp_path/"ready.json"
    assert read_ready(ready) == {}
    ready.write_text("")
    assert read_ready(ready) == {}
    ready.write_text('{"pid": 43')
    assert read_ready(ready) == {}
    ready.unlink()
    assert read_ready(ready) == {}


def test_state_is_split_and_derived_caches_are_not_persisted(tmp_path):
    core = Controller(tmp_path/"state.db")
    core.create(yaml.safe_load((ROOT/"examples/local.yaml").read_text()))
    revision = core.labs["local-demo"]["revision"]
    core.transact("sw-a", [{"kind": "interface", "port": "p1", "fields": {"admin": False}}], revision)
    db = core.store.db
    meta = json.loads(db.execute("SELECT state FROM lab_meta").fetchone()[0])
    assert "devices" not in meta["lab"] and meta["device_ids"]
    runtime = json.loads(db.execute("SELECT state FROM lab_runtime").fetchone()[0])
    assert "lldp_cache" not in runtime and "counters" in runtime
    core.store.close()


def test_transaction_round_trip_survives_restart(tmp_path):
    database = tmp_path/"state.db"
    core = Controller(database)
    core.create(yaml.safe_load((ROOT/"examples/local.yaml").read_text()))
    core.transact("sw-a", [{"kind": "system", "fields": {"hostname": "RENAMED"}}], core.labs["local-demo"]["revision"])
    revision = core.labs["local-demo"]["revision"]
    core.store.close()
    again = Controller(database)
    assert again.labs["local-demo"]["revision"] == revision
    assert again.snapshot("sw-a")["device"]["hostname"] == "RENAMED"


def test_version_one_database_migrates_in_place(tmp_path):
    raw = yaml.safe_load((ROOT/"examples/local.yaml").read_text())
    source = Controller(tmp_path/"source.db")
    source.create(raw)
    state = {k:v for k,v in source.labs[raw["name"]].items() if not k.startswith("_")}
    devices = len(state["lab"]["devices"])
    source.store.close()

    legacy = tmp_path/"legacy.db"
    connection = sqlite3.connect(legacy)
    connection.executescript(LEGACY_SCHEMA)
    connection.execute("INSERT INTO labs VALUES (?,?)", (raw["name"], json.dumps(state)))
    connection.execute("INSERT INTO operations VALUES (?,?)", ("op-1", json.dumps({"operation_id": "op-xyz", "revision": 1})))
    connection.execute("INSERT INTO deliveries(event_id,status,payload) VALUES (?,?,?)",
                       (1, "sent", json.dumps({"device": "sw-a", "role": "notification"})))
    connection.commit()
    connection.close()

    migrated = Controller(legacy)
    assert len(migrated.labs[raw["name"]]["lab"]["devices"]) == devices
    assert migrated.store.operation_by_id("op-xyz")["operation_id"] == "op-xyz"
    check = sqlite3.connect(legacy)
    assert check.execute("PRAGMA user_version").fetchone()[0] == 2
    tables = {row[0] for row in check.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert "labs" not in tables and {"lab_meta", "lab_device", "lab_runtime"} <= tables
    assert check.execute("SELECT device, role FROM deliveries").fetchone() == ("sw-a", "notification")
    migrated.store.close()


def test_pending_events_query_uses_the_composite_index(tmp_path):
    from idc_simulator.storage import Store
    store = Store(tmp_path/"plan.db")
    query = ("SELECT event_id FROM deliveries WHERE device=? AND role=? "
             "AND status IN ('processed','dropped') AND event_id>?")
    plan = " ".join(row[-1] for row in store.db.execute("EXPLAIN QUERY PLAN "+query, ("d", "query", 0)))
    assert "deliveries_lookup" in plan and "SCAN deliveries" not in plan
    store.close()


def test_retention_never_discards_events_a_consumer_still_owes(tmp_path):
    from idc_simulator.storage import Store
    store = Store(tmp_path/"prune.db")
    for n in range(10):
        store.append_events("lab", 1, [{"device_id": "sw-a", "type": "coldStart", "time_ns": n}])
    store.cursor("sw-a:notification", 4)
    removed = store.prune(event_rows=3, operation_hours=0)
    assert removed == 4
    assert store.events(after=0, limit=100)[0]["id"] == 5
    assert {row["event_id"] for row in store.deliveries()} <= set()
    assert store.cursor("sw-a:notification") == 4
    store.close()


def test_operation_retention_expires_only_old_rows(tmp_path):
    from idc_simulator.storage import Store
    store = Store(tmp_path/"ops.db")
    store.commit("lab", {"lab": {"devices": {}}, "revision": 1, "status": "stopped", "runtime": {}, "startup": {}},
                 operation=("op-key", {"operation_id": "fresh"}))
    store.db.execute("UPDATE operations SET created_ns=1 WHERE id='op-key'")
    store.prune(event_rows=0, operation_hours=1)
    assert store.operation("op-key") is None
    store.close()


async def test_junos_route_delete_removes_the_prefix(controller):
    from conftest import LocalClient
    controller.labs["local-demo"]["lab"]["devices"]["sw-a"]["profile"] = "juniper/junos/demo"
    session = CliSession(LocalClient(controller))
    await session.execute("configure")
    await session.execute("set routing-options static route 10.9.0.0/24 next-hop 192.0.2.10")
    await session.execute("set routing-options static route 10.9.0.0/24 next-hop 192.0.2.11")
    await session.execute("commit")
    assert len(controller.snapshot("sw-a")["device"]["routes"]) == 2

    await session.execute("delete routing-options static route 10.9.0.0/24 next-hop 192.0.2.10")
    await session.execute("commit")
    assert [r["next_hop"] for r in controller.snapshot("sw-a")["device"]["routes"]] == ["192.0.2.11"]

    await session.execute("delete routing-options static route 10.9.0.0/24")
    await session.execute("commit")
    assert controller.snapshot("sw-a")["device"]["routes"] == []


async def test_junos_route_requires_next_hop_when_adding(controller):
    from conftest import LocalClient
    controller.labs["local-demo"]["lab"]["devices"]["sw-a"]["profile"] = "juniper/junos/demo"
    session = CliSession(LocalClient(controller))
    await session.execute("configure")
    result = await session.execute("set routing-options static route 10.9.0.0/24")
    assert result.startswith("Error:") and "next-hop" in result


def test_vlan_lists_accept_ranges_and_reject_invalid_values():
    assert parse_vlans("100,200-203, 300 to 302") == [100, 200, 201, 202, 203, 300, 301, 302]
    for bad in ("", "0", "4095", "10-5", "abc"):
        with pytest.raises(SimulationError):
            parse_vlans(bad)


class FakeCliClient:

    def __init__(self, device):
        self.current = {"revision": 1, "device": device, "tables": {}}
        self.operations = []

    async def refresh(self):
        return self.current

    async def transact(self, operations, revision=None, request_id=None):
        self.operations.extend(operations)
        for operation in operations:
            if operation["kind"] == "interface":
                self.current["device"]["interfaces"][operation["port"]].update(operation["fields"])
        return {"revision": self.current["revision"]+1}

    async def call(self, method, **params):
        raise ValueError(method)


async def test_trunk_allowed_vlan_add_and_remove_are_incremental():
    device = {"id": "sw-a", "hostname": "sw-a", "profile": "cisco/ios/c9300", "vlans": {}, "l3_interfaces": [],
              "routes": [], "firewall": [], "cross_connects": [], "cards": [], "sensors": [], "serial": "x",
              "chassis_mac": "02:00:00:00:00:01", "management_ip": "127.0.0.1", "forwarding": True, "hardware": {},
              "interfaces": {"p1": {"id": "p1", "name": "Gi1/0/1", "admin": True, "oper": True, "pvid": 1,
                                    "allowed_vlans": [1], "mode": "switchport", "ifindex": 1, "mtu": 1500,
                                    "mac": "02:00:00:00:00:01", "description": "", "speed_bps": 10**9,
                                    "bridge_port": 1, "lldp": True, "trap_enabled": True}}}
    client = FakeCliClient(device)
    session = CliSession(client)
    await session.execute("configure terminal")
    await session.execute("interface Gi1/0/1")
    await session.execute("switchport trunk allowed vlan 100-102")
    await session.execute("switchport trunk allowed vlan add 200,300-301")
    await session.execute("switchport trunk allowed vlan remove 101")
    assert device["interfaces"]["p1"]["allowed_vlans"] == [100, 102, 200, 300, 301]
    assert (await session.execute("switchport trunk allowed vlan remove 100,102,200,300,301")).startswith("% Error:")


def test_non_ascii_credentials_fail_authentication_instead_of_raising():
    server = AuthServer({"ssh_user": "sim", "ssh_password": "sim-password"}, set())
    assert server.validate_password("sim", "sim-password") is True
    assert server.validate_password("用户", "sim-password") is False
    assert server.validate_password("sim", "口令") is False
    assert server.validate_password("sim", None) is False


async def test_non_ascii_authorization_header_returns_401(tmp_path):
    from aiohttp.test_utils import TestClient, TestServer
    runtime = Runtime(tmp_path, model_only=True)
    client = TestClient(TestServer(runtime.app("ascii-token")))
    await client.start_server()
    try:
        response = await client.get("/v1/labs", headers={"Authorization": "Bearer tökén"})
        assert response.status == 401
        assert response.headers["WWW-Authenticate"] == "Bearer"
    finally:
        await client.close()
        await runtime.close()


def test_unauthenticated_non_loopback_bind_is_refused():
    assert is_loopback("127.0.0.1") and is_loopback("::1")
    assert not is_loopback("0.0.0.0") and not is_loopback("10.0.0.5")
    prevent_unauthenticated_bind("127.0.0.1", None, False)
    prevent_unauthenticated_bind("0.0.0.0", "secret", False)
    prevent_unauthenticated_bind("0.0.0.0", None, True)
    with pytest.raises(SystemExit) as excinfo:
        prevent_unauthenticated_bind("0.0.0.0", None, False)
    assert "IDC_API_TOKEN" in str(excinfo.value)


async def test_local_mode_rejects_multi_device_labs_before_spawning(tmp_path):
    runtime = Runtime(tmp_path, mode="local")
    lab = generate_lab(2, 4, local=True)
    runtime.controller.create(lab)
    try:
        with pytest.raises(SimulationError) as excinfo:
            await runtime.supervisor.start(lab["name"])
        assert "one device per lab" in str(excinfo.value)
    finally:
        await runtime.close()


def test_yaml_import_rejects_anchor_expansion_bombs():
    lines = ["a0: &a0 [x, x, x, x, x, x, x, x, x]"]
    for n in range(1, 12):
        lines.append(f"a{n}: &a{n} [" + ", ".join([f"*a{n-1}"]*9) + "]")
    with pytest.raises(SimulationError):
        safe_load("\n".join(lines))
    assert safe_load((ROOT/"examples/local.yaml").read_text())["name"] == "local-demo"
    with pytest.raises(SimulationError):
        safe_load(12345)


def test_all_example_topologies_still_validate():
    for path in sorted((ROOT/"examples").glob("*.yaml")):
        lab = load_lab(path)
        assert lab["devices"]


def test_read_paths_share_the_derive_throttle(controller, clock):
    state = controller.labs["local-demo"]
    controller.topology("local-demo")
    stamp = state["derived_ns"]
    clock.advance(0.1)
    controller.topology("local-demo")
    host = controller.labs["local-demo"]["lab"]["hosts"][0]["address"].split("/")[0]
    controller.diagnose_ping("local-demo", "sw-a", host)
    assert state["derived_ns"] == stamp
    clock.advance(0.3)
    controller.topology("local-demo")
    assert state["derived_ns"] > stamp


class _StubClient:
    def __init__(self, failures):
        self.failures = failures
        self.current = {"revision": 1, "device": {"hostname": "sw-a"}, "runtime": {"boot_ns": 0}}
        self.calls = 0

    async def call(self, method, **params):
        self.calls += 1
        if self.calls <= self.failures:
            raise RuntimeError("poison event")
        return []


class _StubAgent:
    credentials = {"community": "c", "v3_user": "u"}


async def test_poll_loop_survives_non_control_errors():
    from idc_simulator.metrics import Metrics
    metrics = Metrics()
    client = _StubClient(failures=2)
    agent = _StubAgent()
    agent.client = client
    sender = NotificationSender(agent, [], metrics)
    sender.cursor = 0
    task = asyncio.create_task(sender.poll())
    for _ in range(60):
        await asyncio.sleep(.05)
        if client.calls >= 4:
            break
    assert not task.done()
    assert metrics.counts["notification_poll_errors_total"] >= 1
    task.cancel()
    await asyncio.gather(task, return_exceptions=True)


def test_stop_handlers_fall_back_when_loop_cannot_register_signals(monkeypatch):
    import signal as signal_module
    from idc_simulator.runtime import install_stop_handlers
    recorded = {}

    def fake_signal(sig, handler):
        recorded[sig] = handler

    async def scenario():
        def unavailable(*_args, **_kwargs):
            raise NotImplementedError

        monkeypatch.setattr(asyncio.get_running_loop(), "add_signal_handler", unavailable)
        real_signal = signal_module.signal
        signal_module.signal = fake_signal
        try:
            stop = asyncio.Event()
            assert install_stop_handlers(stop) == []
        finally:
            signal_module.signal = real_signal
        assert set(recorded) == {signal_module.SIGTERM, signal_module.SIGINT}
        recorded[signal_module.SIGTERM](signal_module.SIGTERM, None)
        await asyncio.sleep(0)
        return stop.is_set()

    assert asyncio.run(scenario()) is True


def test_stop_handlers_survive_platforms_that_reject_every_registration(monkeypatch):
    import signal as signal_module
    from idc_simulator.runtime import install_stop_handlers

    def refuse(*_args, **_kwargs):
        raise OSError("signal registration unavailable")

    async def scenario():
        def unavailable(*_args, **_kwargs):
            raise NotImplementedError

        monkeypatch.setattr(asyncio.get_running_loop(), "add_signal_handler", unavailable)
        real_signal = signal_module.signal
        signal_module.signal = refuse
        try:
            return install_stop_handlers(asyncio.Event())
        finally:
            signal_module.signal = real_signal

    assert asyncio.run(scenario()) == []
