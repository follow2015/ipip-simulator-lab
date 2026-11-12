# scan-secrets: fixtures-allowed  # 本文件必须写出假口令才能测鉴权失败路径；只在 tests/ 内生效
import asyncio
import contextlib
import socket
import pytest
import asyncssh
from pysnmp.hlapi.v3arch.asyncio import (
    SnmpEngine, CommunityData, UsmUserData, UdpTransportTarget, ContextData,
    ObjectType, ObjectIdentity, get_cmd, next_cmd, bulk_cmd, set_cmd,
    usmHMACSHAAuthProtocol, usmAesCfb128Protocol)
from pysnmp.proto import rfc1902, rfc1905
from pysnmp.entity import engine, config
from pysnmp.carrier.asyncio.dgram import udp
from pysnmp.entity.rfc3413 import ntfrcv
from idc_simulator.runtime import Runtime, credentials_from_env
from idc_simulator.ipc import RpcClient
from idc_simulator.snmp import SnmpAgent
from idc_simulator.ssh import SshServer
from idc_simulator.metrics import Metrics
from idc_simulator.notifications import NotificationSender


def free_port(kind=socket.SOCK_STREAM):
    with socket.socket(socket.AF_INET, kind) as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


async def start_agent(client, credentials, metrics, attempts=5):
    for attempt in range(attempts):
        port = free_port(socket.SOCK_DGRAM)
        agent = SnmpAgent(client, "127.0.0.1", port, credentials, metrics)
        try:
            await agent.ready()
            return agent, port
        except OSError:
            with contextlib.suppress(Exception):
                agent.transport.close()
            if attempt == attempts-1:
                raise
    raise RuntimeError("unreachable")


@pytest.fixture
async def services(tmp_path, raw_lab):
    runtime = Runtime(tmp_path)
    runtime.controller.create(raw_lab)
    runtime.controller.transact("sw-a", [{"kind": "worker_started"}], 1)
    await runtime.start()
    client = RpcClient(runtime.socket, "sw-a")
    await client.refresh()
    metrics = Metrics()
    credentials = credentials_from_env()
    agent, port = await start_agent(client, credentials, metrics)
    ssh_port = free_port()
    ssh = SshServer(client, "127.0.0.1", ssh_port, tmp_path/"host_key", credentials, metrics)
    await ssh.start()
    manager = SnmpEngine()
    target = await UdpTransportTarget.create(("127.0.0.1", port), timeout=1, retries=0)
    yield runtime, client, agent, ssh, manager, target, ssh_port
    manager.close_dispatcher()
    await ssh.close(); await agent.close(); await runtime.close()


async def query(services, oid, auth=None):
    _, _, _, _, manager, target, _ = services
    return await get_cmd(manager, auth or CommunityData("sim-read"), target, ContextData(), ObjectType(ObjectIdentity(oid)))


@pytest.mark.parametrize("version", [0, 1])
async def test_wire_get_v1_v2_and_unknown_objects(services, version):
    result = await query(services, "1.3.6.1.2.1.1.5.0", CommunityData("sim-read", mpModel=version))
    assert result[0] is None and int(result[1]) == 0
    assert str(result[3][0][1]) == "IDC-DEMO"
    result = await query(services, "1.3.6.1.2.1.999.0", CommunityData("sim-read", mpModel=version))
    if version == 0:
        assert int(result[1]) == 2
    else:
        assert result[1] == 0 and isinstance(result[3][0][1], rfc1905.NoSuchObject)


async def test_bulk_order_end_and_counter_sample(services):
    _, _, _, _, manager, target, _ = services
    result = await bulk_cmd(manager, CommunityData("sim-read"), target, ContextData(), 0, 100,
        ObjectType(ObjectIdentity("1.3.6.1.2.1.2.2.1")))
    assert result[0] is None and not result[1]
    oids = [tuple(pair[0]) for pair in result[3]]
    assert oids == sorted(set(oids))
    end = await next_cmd(manager, CommunityData("sim-read"), target, ContextData(), ObjectType(ObjectIdentity("2.99")))
    assert isinstance(end[3][0][1], rfc1905.EndOfMibView)
    result = await get_cmd(manager, CommunityData("sim-read"), target, ContextData(),
        ObjectType(ObjectIdentity("1.3.6.1.2.1.2.2.1.10.10")), ObjectType(ObjectIdentity("1.3.6.1.2.1.31.1.1.1.6.10")))
    assert int(result[3][0][1]) == int(result[3][1][1]) % 2**32


async def test_wire_set_atomic_permissions_and_visibility(services):
    runtime, client, _, _, manager, target, _ = services
    result = await set_cmd(manager, CommunityData("sim-write"), target, ContextData(),
        ObjectType(ObjectIdentity("1.3.6.1.2.1.1.5.0"), rfc1902.OctetString("SET-NAME")),
        ObjectType(ObjectIdentity("1.3.6.1.2.1.2.2.1.7.10"), rfc1902.Integer32(3)))
    assert int(result[1]) == 10 and int(result[2]) == 2
    assert runtime.controller.snapshot("sw-a")["device"]["hostname"] == "IDC-DEMO"
    result = await set_cmd(manager, CommunityData("sim-read"), target, ContextData(),
        ObjectType(ObjectIdentity("1.3.6.1.2.1.1.5.0"), rfc1902.OctetString("READ-ONLY")))
    assert int(result[1]) != 0
    result = await set_cmd(manager, CommunityData("sim-write"), target, ContextData(),
        ObjectType(ObjectIdentity("1.3.6.1.2.1.1.5.0"), rfc1902.OctetString("SET-NAME")),
        ObjectType(ObjectIdentity("1.3.6.1.2.1.2.2.1.7.10"), rfc1902.Integer32(2)))
    assert result[0] is None and not result[1]
    assert str((await query(services, "1.3.6.1.2.1.1.5.0"))[3][0][1]) == "SET-NAME"
    assert not client.current["device"]["interfaces"]["p1"]["admin"]


async def test_v3_authpriv_reboot_identity(services):
    runtime, client, agent, _, manager, target, _ = services
    user = UsmUserData("sim-user", "sim-auth-password", "sim-priv-password", authProtocol=usmHMACSHAAuthProtocol, privProtocol=usmAesCfb128Protocol)
    result = await query(services, "1.3.6.1.2.1.1.5.0", user)
    assert result[0] is None and not result[1]
    original_id = client.current["runtime"]["engine_id"]
    original_boots = client.current["runtime"]["engine_boots"]
    await client.transact([{"kind": "reboot"}])
    agent.synchronize_identity()
    result = await query(services, "1.3.6.1.2.1.1.5.0", user)
    assert result[0] is None and not result[1]
    assert client.current["runtime"]["engine_id"] == original_id
    assert client.current["runtime"]["engine_boots"] == original_boots+1


async def test_ssh_exec_shell_editing_and_state(services):
    runtime, client, agent, ssh, _, _, port = services
    async with asyncssh.connect("127.0.0.1", port=port, username="sim", password="sim-password", known_hosts=None, config=None) as conn:
        result = await conn.run("show version", check=True)
        assert "C9300" in result.stdout
        result = await conn.run("configure terminal\ninterface GigabitEthernet1/0/1\nshutdown\nwrite memory", check=True)
        assert "Configuration saved" in result.stdout
        assert not runtime.controller.snapshot("sw-a")["device"]["interfaces"]["p1"]["admin"]
        proc = await conn.create_process(term_type="vt100")
        await asyncio.wait_for(proc.stdout.readuntil("#"), 2)
        proc.stdin.write("show versiox\x7fn\r")
        output = await asyncio.wait_for(proc.stdout.readuntil("#"), 2)
        assert "Serial Number" in output
        proc.stdin.write("exit\r")
        await proc.wait_closed()


async def test_bad_ssh_password_rejected(services):
    with pytest.raises(asyncssh.PermissionDenied):
        await asyncssh.connect("127.0.0.1", port=services[-1], username="sim", password="wrong", known_hosts=None, config=None)


@pytest.mark.parametrize("notification_type", ["trap", "inform"])
@pytest.mark.parametrize("notification_version", ["v2c", "v3"])
async def test_notification_varbinds_source_and_inform_ack(services, notification_type, notification_version):
    runtime, client, agent, _, _, _, _ = services
    receiver = engine.SnmpEngine()
    transport = udp.UdpTransport().open_server_mode(("127.0.0.1", 0))
    config.add_transport(receiver, udp.DOMAIN_NAME, transport)
    await transport._lport
    port = transport.transport.get_extra_info("sockname")[1]
    config.add_v1_system(receiver, "receive", "sim-read")
    if notification_version == "v3":
        config.add_v3_user(receiver, "sim-user", config.USM_AUTH_HMAC96_SHA, "sim-auth-password", config.USM_PRIV_CFB128_AES, "sim-priv-password",
            securityEngineId=agent.engine.snmpEngineID if notification_type == "trap" else None)
    received = asyncio.get_running_loop().create_future()
    def on_notification(snmp_engine, reference, context_id, name, varbinds, cb_ctx):
        source = receiver.observer.get_execution_context("rfc3412.receiveMessage:request")["transportAddress"]
        if not received.done():
            received.set_result((source, varbinds))
    ntfrcv.NotificationReceiver(receiver, on_notification)
    sender = NotificationSender(agent, [{"address": "127.0.0.1", "port": port, "type": notification_type, "version": notification_version, "retries": 0, "timeout": .5}], Metrics())
    await client.transact([{"kind": "interface", "port": "p1", "fields": {"admin": False}}])
    event = next(e for e in runtime.controller.store.events() if e["type"] == "linkDown")
    try:
        await sender.send(event, 0, sender.targets[0])
        source, binds = await asyncio.wait_for(received, 2)
        assert source[0] == "127.0.0.1"
        assert tuple(binds[0][0]) == (1,3,6,1,2,1,1,3,0)
        assert str(binds[1][1]) == "1.3.6.1.6.3.1.1.5.3"
        assert int(binds[2][1]) == 10
        if notification_type == "inform":
            assert runtime.controller.store.deliveries()[0]["status"] == "confirmed", runtime.controller.store.deliveries()
    finally:
        receiver.close_dispatcher()


async def test_notification_queue_bounded_and_drop_persisted(services):
    runtime, client, agent, _, _, _, _ = services
    sender = NotificationSender(agent, [{"address": "127.0.0.1", "port": free_port(socket.SOCK_DGRAM)}], Metrics(), capacity=1)
    runtime.controller.inject_notifications("local-demo", ["sw-a"], count=5)
    task = asyncio.create_task(sender.poll())
    try:
        await asyncio.sleep(.1)
        assert sender.queue.qsize() == 1
        assert sender.metrics.counts["notifications_dropped_total"] == 4
        pending = await client.call("events")
        assert sum(e["completed"] for e in pending) == 4
    finally:
        task.cancel(); await asyncio.gather(task, return_exceptions=True)
