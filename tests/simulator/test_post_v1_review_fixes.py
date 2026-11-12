"""Regression coverage for the 2026-11-10 post-v1 independent review."""
import asyncio
import json

from aiohttp.test_utils import TestClient, TestServer
import pytest

from idc_simulator import deploy
from idc_simulator.errors import Conflict, SimulationError, Unavailable
from idc_simulator.ipc import RpcClient
from idc_simulator.runtime import Runtime
from idc_simulator.synchronization import same_tables
from idc_simulator.tools import generate_lab


class _Writer:
    def __init__(self, drain_error=None):
        self.written = b""
        self.drain_error = drain_error
        self.closed = False

    def write(self, data):
        self.written += data

    async def drain(self):
        if self.drain_error:
            raise self.drain_error

    def close(self):
        self.closed = True

    async def wait_closed(self):
        return None


async def test_rpc_connect_failure_is_not_executed(tmp_path, monkeypatch):
    async def refuse(path, limit=None):
        raise FileNotFoundError(path)

    monkeypatch.setattr("idc_simulator.ipc.asyncio.open_unix_connection", refuse, raising=False)
    with pytest.raises(Unavailable) as caught:
        await RpcClient(tmp_path/"missing.sock").call("transact", operations=[], revision=1, request_id="op-1")
    assert caught.value.outcome == "not_executed"
    assert caught.value.operation_id == "op-1"


async def test_rpc_failure_after_request_was_written_is_unknown(tmp_path, monkeypatch):
    writer = _Writer(drain_error=ConnectionResetError("reply lost"))

    async def accept(path, limit=None):
        return asyncio.StreamReader(), writer

    monkeypatch.setattr("idc_simulator.ipc.asyncio.open_unix_connection", accept, raising=False)
    with pytest.raises(Unavailable) as caught:
        await RpcClient(tmp_path/"control.sock").call("transact", operations=[], revision=1, request_id="op-2")
    assert writer.written and b"op-2" in writer.written
    assert caught.value.outcome == "unknown"
    assert caught.value.operation_id == "op-2"


async def test_rpc_empty_reply_is_unknown_not_not_executed(tmp_path, monkeypatch):
    writer = _Writer()

    async def accept(path, limit=None):
        return asyncio.StreamReader(), writer

    monkeypatch.setattr("idc_simulator.ipc.asyncio.open_unix_connection", accept, raising=False)
    with pytest.raises(Unavailable) as caught:
        await RpcClient(tmp_path/"control.sock").call("save")
    assert caught.value.outcome == "unknown"


@pytest.mark.parametrize("origin", ["https://sim.example", "https://SIM.example", "https://sim.example:443"])
async def test_origin_comparison_normalizes_case_and_default_port(tmp_path, origin):
    runtime = Runtime(tmp_path, model_only=True)
    client = TestClient(TestServer(runtime.app(allowed_hosts=["sim.example"],
                                             allowed_origins=["https://SIM.example"])))
    await client.start_server()
    try:
        reply = await client.post("/v1/labs", json=generate_lab(1, 4),
                                  headers={"Host": "sim.example", "Origin": origin})
        assert reply.status == 201
    finally:
        await client.close()
        await runtime.close()


@pytest.mark.parametrize("origin", ["https://sim.example/evil", "https://user@sim.example", "null"])
async def test_origin_with_path_or_credentials_is_rejected(tmp_path, origin):
    runtime = Runtime(tmp_path, model_only=True)
    client = TestClient(TestServer(runtime.app(allowed_hosts=["sim.example"],
                                             allowed_origins=["https://sim.example"])))
    await client.start_server()
    try:
        reply = await client.post("/v1/labs", json=generate_lab(1, 4),
                                  headers={"Host": "sim.example", "Origin": origin})
        assert reply.status == 403
    finally:
        await client.close()
        await runtime.close()


async def test_lifecycle_failure_is_counted(tmp_path):
    runtime = Runtime(tmp_path, model_only=True)
    async def fails():
        raise RuntimeError("replace failed after the client left")
    try:
        with pytest.raises(RuntimeError, match="replace failed"):
            await runtime.lifecycle(fails())
        await asyncio.sleep(0)
        assert runtime.metrics.counts["lifecycle_task_errors_total"] == 1
    finally:
        await runtime.close()


async def test_expected_lifecycle_http_errors_are_not_counted_or_logged_as_errors(tmp_path, caplog):
    runtime = Runtime(tmp_path, model_only=True)
    client = TestClient(TestServer(runtime.app()))
    await client.start_server()
    try:
        response = await client.post('/v1/labs/missing/stop', json={})
        assert response.status == 404
        await asyncio.sleep(0)
        assert not runtime.metrics.counts['lifecycle_task_errors_total']
        assert 'Background lifecycle task rejected' in caplog.text
        assert ' ERROR ' not in caplog.text
    finally:
        await client.close()
        await runtime.close()


async def test_shutdown_budget_cancels_stuck_lifecycle_task(tmp_path):
    runtime = Runtime(tmp_path, model_only=True)
    runtime.shutdown_timeout = 0.05
    stuck = asyncio.create_task(asyncio.sleep(60))
    runtime.lifecycle_tasks.add(stuck)
    await runtime.close()
    assert stuck.cancelled()
    assert runtime.metrics.counts["shutdown_timeout_total"] >= 1


async def test_start_runs_without_fcntl(tmp_path, monkeypatch):
    monkeypatch.setattr("idc_simulator.runtime.fcntl", None)
    runtime = Runtime(tmp_path, model_only=True)
    try:
        await runtime.start()
        assert runtime.started and runtime.owner_lock is None
    finally:
        await runtime.close()


async def test_corrupt_owner_record_is_a_conflict_not_a_bad_request(tmp_path, raw_lab):
    runtime = Runtime(tmp_path, model_only=True)
    runtime.controller.create(raw_lab)
    runtime.supervisor.mode = "namespace"
    owner = tmp_path/"local-demo"/"network-owner.json"
    owner.parent.mkdir(parents=True, exist_ok=True)
    owner.write_text("{not json", encoding="utf-8")
    try:
        with pytest.raises(Conflict, match="ownership record is unreadable"):
            await runtime.supervisor.stop("local-demo")
    finally:
        await runtime.close()


async def test_failed_start_keeps_original_error_when_cleanup_also_fails(tmp_path, raw_lab, monkeypatch):
    runtime = Runtime(tmp_path, model_only=True)
    runtime.controller.create(raw_lab)
    runtime.supervisor.mode = "namespace"
    monkeypatch.setattr(deploy.sys, "platform", "linux")
    monkeypatch.setattr(deploy.os, "geteuid", lambda: 0, raising=False)
    owner = tmp_path/"local-demo"/"network-owner.json"
    owner.parent.mkdir(parents=True, exist_ok=True)
    owner.write_text("{not json", encoding="utf-8")
    async def explode(lab_id):
        raise SimulationError("original start failure")
    monkeypatch.setattr(runtime.supervisor, "recover_orphans", explode)
    try:
        with pytest.raises(SimulationError, match="original start failure"):
            await runtime.supervisor.start("local-demo")
    finally:
        await runtime.close()


async def test_worker_metrics_invalid_lines_are_attributed(tmp_path):
    runtime = Runtime(tmp_path, model_only=True)
    directory = tmp_path/"local-demo"/"sw-a"
    directory.mkdir(parents=True)
    (directory/"metrics-query.prom").write_text("idc_good 1\nnot_a_sample\n", encoding="utf-8")
    client = TestClient(TestServer(runtime.app()))
    await client.start_server()
    try:
        text = await (await client.get("/metrics")).text()
        assert 'idc_worker_metrics_invalid_lines_total{device="sw-a",role="query"} 1' in text
    finally:
        await client.close()
        await runtime.close()


def test_same_tables_ignores_row_order_and_ttl_renewal():
    before = {"fdb": [{"mac": "aa", "vlan": 10, "expires_ns": 1},
                      {"mac": "bb", "vlan": 20, "expires_ns": 1}],
              "flows": []}
    reordered = {"fdb": [{"mac": "bb", "vlan": 20, "expires_ns": 99},
                         {"mac": "aa", "vlan": 10, "expires_ns": 99}],
                 "flows": []}
    assert same_tables(before, reordered)
    changed = json.loads(json.dumps(reordered))
    changed["fdb"][0]["vlan"] = 30
    assert not same_tables(before, changed)
