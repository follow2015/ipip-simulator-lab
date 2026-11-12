"""Notification stress and receiver accounting, separated from state transitions."""
import asyncio
from collections import Counter
from hashlib import sha256
import json
import os
from pathlib import Path
import time
from aiohttp import ClientSession
from pysnmp.entity import engine, config
from pysnmp.entity.rfc3413 import ntfrcv
from pysnmp.carrier.asyncio.dgram import udp
from idc_simulator.model import load_lab, stable_hex
from idc_simulator.runtime import credentials_from_env


async def pressure(args):
    lab = load_lab(args.topology)
    emitted = 0
    start = time.monotonic()
    headers = {"Authorization": "Bearer "+os.environ["IDC_API_TOKEN"]} if os.getenv("IDC_API_TOKEN") else {}
    async with ClientSession(headers=headers) as client:
        while time.monotonic()-start < args.duration:
            desired = int(min(args.duration, time.monotonic()-start+.1)*args.rate)
            count = min(10000, max(0, desired-emitted))
            if count:
                async with client.post(args.api+f"/v1/labs/{lab['name']}/notifications", json={"devices": list(lab["devices"]), "count": count, "type": "coldStart"}) as response:
                    if response.status != 200:
                        raise ValueError(await response.text())
                    emitted += (await response.json())["events"]
            await asyncio.sleep(.1)
    report = {"schema_version": 1, "lab": lab["name"], "emitted": emitted, "duration_seconds": time.monotonic()-start, "target_rate": args.rate,
        "notes": ["Pure pressure events do not reboot devices or change configuration.", "Compare simulator sent/drop metrics with receiver count for transport accounting."]}
    Path(args.output).write_text(json.dumps(report, indent=2)+"\n")
    print(json.dumps(report, indent=2))


async def receive(args):
    receiver = engine.SnmpEngine()
    credentials = credentials_from_env()
    transport = udp.UdpTransport().open_server_mode((args.bind, args.port))
    config.add_transport(receiver, udp.DOMAIN_NAME, transport)
    await transport._lport
    config.add_v1_system(receiver, "receive", credentials["community"])
    if args.topology:
        lab = load_lab(args.topology)
        config.add_v3_user(receiver, credentials["v3_user"], config.USM_AUTH_HMAC96_SHA, credentials["v3_auth"], config.USM_PRIV_CFB128_AES, credentials["v3_priv"])
        for dev in lab["devices"]:
            identity = "80004fb805"+stable_hex(lab["seed"], lab["name"]+":"+dev, 22)
            from pysnmp.proto.rfc1902 import OctetString
            config.add_v3_user(receiver, credentials["v3_user"], config.USM_AUTH_HMAC96_SHA, credentials["v3_auth"], config.USM_PRIV_CFB128_AES, credentials["v3_priv"], securityEngineId=OctetString(hexValue=identity))
    sources = Counter(); types = Counter(); fingerprints = Counter()
    start = time.monotonic()
    def notified(snmp_engine, reference, context_id, name, binds, cb_context):
        source = receiver.observer.get_execution_context("rfc3412.receiveMessage:request")["transportAddress"][0]
        sources[source] += 1
        types[str(binds[1][1])] += 1
        key = sha256(repr([(str(oid), value.prettyPrint()) for oid, value in binds]).encode()).hexdigest()
        if len(fingerprints) < 100000 or key in fingerprints:
            fingerprints[key] += 1
    ntfrcv.NotificationReceiver(receiver, notified)
    try:
        await asyncio.sleep(args.duration)
    finally:
        receiver.close_dispatcher()
    total = sum(sources.values())
    report = {"schema_version": 1, "received": total, "duration_seconds": time.monotonic()-start,
        "sources": dict(sources), "notification_oids": dict(types), "identical_payloads": sum(v-1 for v in fingerprints.values()),
        "expected": args.expected, "difference_from_expected": args.expected-total if args.expected is not None else None,
        "notes": ["Standard Traps have no engineering event ID; identical payloads do not prove duplicates.", "Difference from expected includes sender queue drops if expected is emitted rather than sent."]}
    Path(args.output).write_text(json.dumps(report, indent=2)+"\n")
    print(json.dumps(report, indent=2))
