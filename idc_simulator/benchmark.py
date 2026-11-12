"""Wire workload runner. Reports measurements, never extrapolates a soak pass."""
import asyncio
from collections import deque
from importlib.metadata import version
import json
import math
from pathlib import Path
import platform
import resource
import time
import asyncssh
from pysnmp.hlapi.v3arch.asyncio import SnmpEngine, CommunityData, UsmUserData, UdpTransportTarget, ContextData, ObjectType, ObjectIdentity, get_cmd, bulk_cmd, usmHMACSHAAuthProtocol, usmAesCfb128Protocol
from pysnmp.proto.rfc1905 import EndOfMibView
from idc_simulator.model import load_lab
from idc_simulator.runtime import credentials_from_env


async def benchmark(args):
    lab = load_lab(args.topology)
    credentials = credentials_from_env()
    manager = SnmpEngine()
    latencies = deque(maxlen=100000)
    counts = {"requests": 0, "values": 0, "timeouts": 0, "errors": 0, "ssh_commands": 0, "ssh_errors": 0, "cycles": 0}
    semaphore = asyncio.Semaphore(args.concurrency)
    start = time.monotonic()
    deadline = start+args.duration
    devices = list(lab["devices"].values())
    v3_count = math.ceil(len(devices)*args.v3_percent/100)
    v3 = UsmUserData(credentials["v3_user"], credentials["v3_auth"], credentials["v3_priv"], authProtocol=usmHMACSHAAuthProtocol, privProtocol=usmAesCfb128Protocol)
    v2 = CommunityData(credentials["community"])
    targets = {d["id"]: await UdpTransportTarget.create((d["management_ip"], args.snmp_port), timeout=args.timeout, retries=args.retries) for d in devices}
    async def request(auth, target, objects, root=None):
        began = time.monotonic()
        try:
            if root:
                result = await bulk_cmd(manager, auth, target, ContextData(), 0, args.max_repetitions, *objects)
            else:
                result = await get_cmd(manager, auth, target, ContextData(), *objects)
            counts["requests"] += 1
            latencies.append(time.monotonic()-began)
            if result[0]:
                counts["timeouts"] += 1
            elif result[1]:
                counts["errors"] += 1
            else:
                counts["values"] += len(result[3])
            return result
        except Exception:
            counts["requests"] += 1; counts["errors"] += 1
            return None
    async def poll(n, d):
        async with semaphore:
            auth, target = (v3 if n < v3_count else v2), targets[d["id"]]
            if args.walk_root:
                root = tuple(int(x) for x in args.walk_root.split("."))
                previous = root
                for _ in range(10000):
                    result = await request(auth, target, [ObjectType(ObjectIdentity(previous))], root=True)
                    if not result or result[0] or result[1] or not result[3]:
                        break
                    for oid, value in result[3]:
                        if isinstance(value, EndOfMibView) or tuple(oid)[:len(root)] != root:
                            return
                        if tuple(oid) <= previous:
                            counts["errors"] += 1; return
                        previous = tuple(oid)
            else:
                oids = [f"1.3.6.1.2.1.2.2.1.{col}.{p['ifindex']}" for col in (2, 5, 7, 8, 10, 11, 13, 14, 16, 17) for p in d["interfaces"].values()]
                for begin in range(0, len(oids), 48):
                    await request(auth, target, [ObjectType(ObjectIdentity(o)) for o in oids[begin:begin+48]])
    async def ssh_load(n):
        d = devices[n%len(devices)]
        while time.monotonic() < deadline:
            try:
                async with asyncssh.connect(d["management_ip"], port=args.ssh_port, username=credentials["ssh_user"], password=credentials["ssh_password"], known_hosts=None, config=None) as connection:
                    command = "display interface" if d["profile"].startswith(("huawei", "h3c")) else "show interfaces"
                    result = await connection.run(command, timeout=args.timeout*5)
                    counts["ssh_commands"] += 1
                    counts["ssh_errors"] += int(result.exit_status != 0)
            except Exception:
                counts["ssh_errors"] += 1
            await asyncio.sleep(max(.1, args.period))
    ssh_tasks = [asyncio.create_task(ssh_load(n)) for n in range(args.ssh_concurrency)]
    try:
        while time.monotonic() < deadline:
            began = time.monotonic()
            await asyncio.gather(*(poll(n, d) for n, d in enumerate(devices)))
            counts["cycles"] += 1
            await asyncio.sleep(max(0, min(deadline-time.monotonic(), args.period-(time.monotonic()-began))))
    finally:
        for task in ssh_tasks:
            task.cancel()
        await asyncio.gather(*ssh_tasks, return_exceptions=True)
        manager.close_dispatcher()
    elapsed = time.monotonic()-start
    ordered = sorted(latencies)
    report = {"schema_version": 1, "lab": lab["name"], "seed": lab["seed"], "duration_seconds": elapsed,
        "devices": len(devices), "ports": sum(len(d["interfaces"]) for d in devices), "counts": counts,
        "latency_seconds": {str(q): ordered[max(0, math.ceil(len(ordered)*q)-1)] if ordered else None for q in (.5, .95, .99)},
        "values_per_second": counts["values"]/elapsed, "timeout_rate": counts["timeouts"]/max(1, counts["requests"]),
        "parameters": vars(args), "platform": platform.platform(), "python": platform.python_version(),
        "components": {name: version(name) for name in ("pysnmp", "asyncssh", "aiohttp", "cryptography", "pyasn1")},
        "client_maxrss_native": resource.getrusage(resource.RUSAGE_SELF).ru_maxrss,
        "notes": ["Latency includes client encoding/crypto/network; this is not agent-only latency.", "Trap receipt/loss is measured separately at the receiver."]}
    Path(args.output).write_text(json.dumps(report, indent=2)+"\n")
    print(json.dumps(report, indent=2))
    return report
