"""One process per independent protocol identity. Cached reads survive control outages."""
import asyncio
import json
import os
from pathlib import Path
import signal
import time
from idc_simulator.deploy import write_ready
from idc_simulator.ipc import RpcClient
from idc_simulator.metrics import Metrics
from idc_simulator.snmp import SnmpAgent, NotificationAgent
from idc_simulator.ssh import SshServer
from idc_simulator.notifications import NotificationSender
from idc_simulator.profiles import load_profile
from idc_simulator.errors import Unavailable, SimulationError


async def run_worker(args):
    client = RpcClient(args.socket, args.device, args.role)
    for attempt in range(40):
        try:
            await client.refresh()
            break
        except (Unavailable, SimulationError):
            if attempt == 39:
                raise
            await asyncio.sleep(.05)
    credentials = json.loads(os.environ["IDC_SIM_CREDENTIALS"])
    metrics = Metrics()
    directory = Path(args.state_dir)
    address = client.current["device"]["management_ip"]
    query = args.role == "query"
    agent = SnmpAgent(client, address, args.snmp_port, credentials, metrics) if query else NotificationAgent(client, address, credentials)
    ssh = SshServer(client, address, args.ssh_port, directory/"ssh_host_key", credentials, metrics) if query else None
    targets = [t for t in credentials.get("notifications", []) if (t.get("version", "v2c") == "v3") == query]
    notifier = NotificationSender(agent, targets, metrics,
        capacity=credentials.get("notification_capacity", 1024), rate=credentials.get("notification_rate", 1000))
    stop = asyncio.Event()
    for sig in (signal.SIGTERM, signal.SIGINT):
        asyncio.get_running_loop().add_signal_handler(sig, stop.set)
    async def synchronize():
        backoff = .2
        while True:
            started = time.monotonic()
            try:
                await client.refresh()
                agent.synchronize_identity()
                metrics.gauges["state_revision"] = client.current["revision"]
                metrics.gauges["state_sync_full_total"] = client.sync_full
                metrics.gauges["state_sync_not_modified_total"] = client.sync_not_modified
                metrics.gauges["state_rpc_response_bytes_total"] = client.response_bytes
                metrics.observe("state_sync_seconds", time.monotonic()-started)
                backoff = .2
            except Unavailable:
                metrics.increment("state_sync_errors_total")
            except Exception:
                metrics.increment("state_sync_loop_errors_total")
                backoff = min(backoff*2, 5)
            metrics.gauges["state_sync_age_seconds"] = (time.monotonic_ns()-client.last_sync_ns)/1e9
            metrics.observe("event_loop_delay_seconds", max(0, time.monotonic()-started-.2))
            temporary = directory/f"metrics-{args.role}.tmp"
            temporary.write_text(metrics.render())
            temporary.replace(directory/f"metrics-{args.role}.prom")
            await asyncio.sleep(backoff)
    task = None
    try:
        load_profile(client.current["device"]["profile"])
        await agent.ready()
        if ssh:
            await ssh.start()
        if args.uid is not None:
            import pwd
            account = pwd.getpwuid(args.uid)
            os.chown(directory, args.uid, account.pw_gid)
            if ssh:
                os.chown(directory/"ssh_host_key", args.uid, account.pw_gid)
            os.setgroups([]); os.setgid(account.pw_gid); os.setuid(args.uid)
        await notifier.start()
        task = asyncio.create_task(synchronize())
        write_ready(directory/("ready.json" if query else "ready-notification.json"), {"pid": os.getpid(), "device": args.device})
        await stop.wait()
    finally:
        if task:
            task.cancel(); await asyncio.gather(task, return_exceptions=True)
        await notifier.close()
        if ssh:
            await ssh.close()
        await agent.close()
        (directory/("ready.json" if query else "ready-notification.json")).unlink(missing_ok=True)
