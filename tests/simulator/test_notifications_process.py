import asyncio
import socket
from pysnmp.entity import engine, config
from pysnmp.carrier.asyncio.dgram import udp
from pysnmp.entity.rfc3413 import ntfrcv
from idc_simulator.runtime import Runtime, credentials_from_env
from test_protocols import free_port


async def test_v2_notifications_separate_process_and_source(tmp_path, raw_lab):
    receiver = engine.SnmpEngine()
    transport = udp.UdpTransport().open_server_mode(("127.0.0.1", 0))
    config.add_transport(receiver, udp.DOMAIN_NAME, transport)
    await transport._lport
    port = transport.transport.get_extra_info("sockname")[1]
    config.add_v1_system(receiver, "receive", "sim-read")
    result = asyncio.get_running_loop().create_future()
    def received(snmp_engine, reference, context_id, name, binds, cb_context):
        source = receiver.observer.get_execution_context("rfc3412.receiveMessage:request")["transportAddress"][0]
        if not result.done():
            result.set_result((source, str(binds[1][1])))
    ntfrcv.NotificationReceiver(receiver, received)
    credentials = credentials_from_env()
    credentials["notifications"] = [{"address": "127.0.0.1", "port": port, "version": "v2c", "type": "trap"}]
    runtime = Runtime(tmp_path, snmp_port=free_port(socket.SOCK_DGRAM), ssh_port=free_port(), credentials=credentials)
    runtime.controller.create(raw_lab)
    await runtime.start()
    try:
        await runtime.supervisor.start("local-demo")
        assert len(runtime.supervisor.processes["local-demo"]) == 2
        assert runtime.supervisor.processes["local-demo"]["sw-a"].pid != runtime.supervisor.processes["local-demo"]["sw-a:notification"].pid
        runtime.controller.inject_notifications("local-demo", ["sw-a"], 1)
        assert await asyncio.wait_for(result, 3) == ("127.0.0.1", "1.3.6.1.6.3.1.1.5.1")
    finally:
        await runtime.close(); receiver.close_dispatcher()
