"""Bounded notification outbox. v3 shares the query engine identity and timeline."""
import asyncio
import time
from pysnmp.entity import config
from pysnmp.entity.rfc3413 import ntforg
from pysnmp.carrier.asyncio.dgram import udp
from pysnmp.proto import rfc1902 as types
from idc_simulator.oid import oid_tuple, IFTABLE
from idc_simulator.errors import Unavailable
from idc_simulator.operations import sensor_direction
from idc_simulator.profiles import load_profile

STANDARD = {"coldStart": 1, "warmStart": 2, "linkDown": 3, "linkUp": 4, "authenticationFailure": 5}
SENSOR_EVENTS = {"sensorAlarm", "sensorClear"}
RMON_RISING = "1.3.6.1.2.1.16.0.1"
RMON_FALLING = "1.3.6.1.2.1.16.0.2"
RMON_COLUMNS = {"index": (1,3,6,1,2,1,16,3,1,1), "variable": (1,3,6,1,2,1,16,3,1,3),
                "sample": (1,3,6,1,2,1,16,3,1,4), "value": (1,3,6,1,2,1,16,3,1,5),
                "rising": (1,3,6,1,2,1,16,3,1,6), "falling": (1,3,6,1,2,1,16,3,1,7)}
RESOURCE_EVENTS = {"resourceAlarm": "rising", "resourceClear": "falling",
                   "interfaceErrors": "rising", "interfaceErrorsClear": "falling"}
ENTITY_CHANGE = "1.3.6.1.2.1.47.2.0.1"
ENTITY_LAST_CHANGE = (1,3,6,1,2,1,47,1,4,1,0)
ENTITY_EVENTS = {"cardInserted", "cardRemoved"}
ENTITY_COLUMNS = {"index": (1,3,6,1,2,1,47,1,1,1,1), "class": (1,3,6,1,2,1,47,1,1,1,5),
                  "name": (1,3,6,1,2,1,47,1,1,1,7), "serial": (1,3,6,1,2,1,47,1,1,1,11),
                  "fru": (1,3,6,1,2,1,47,1,1,1,16)}
RESOURCE_FALLBACK = {"cpu": "1.3.6.1.2.1.25.3.3.1.2.1", "memory": "1.3.6.1.2.1.25.2.3.1.6.1"}


def resource_variable(snapshot, resource, value, threshold):
    private = load_profile(snapshot["device"]["profile"])["mib"].get("private") or []
    field = "cpu_percent" if resource == "cpu" else "memory_percent"
    for entry in private:
        if entry.get("field") == field:
            return entry["oid"], value, threshold
    if resource == "cpu":
        return RESOURCE_FALLBACK["cpu"], value, threshold
    total = snapshot["device"].get("resources", {}).get("memory_total", 1048576)
    return RESOURCE_FALLBACK["memory"], total*value//100, total*threshold//100


def event_varbinds(event, snapshot):
    number = STANDARD.get(event["type"])
    mapping = None
    if number is None:
        if event["type"] in RESOURCE_EVENTS:
            return rmon_varbinds(event, snapshot)
        if event["type"] in ENTITY_EVENTS:
            return entity_varbinds(event, snapshot)
        private = load_profile(snapshot["device"]["profile"])["traps"].get("private") or {}
        mapping = private.get(event["type"])
        if not mapping:
            if event["type"] in SENSOR_EVENTS:
                return sensor_rmon_varbinds(event, snapshot)
            return None
    uptime = max(0, (event["time_ns"]-snapshot["runtime"]["boot_ns"])//10_000_000) % 2**32
    trap_oid = f"1.3.6.1.6.3.1.1.5.{number}" if number is not None else mapping["oid"]
    varbinds = [(oid_tuple("1.3.6.1.2.1.1.3.0"), types.TimeTicks(uptime)),
               (oid_tuple("1.3.6.1.6.3.1.1.4.1.0"), types.ObjectIdentifier(trap_oid))]
    payload = event.get("payload", {})
    if event["type"] in {"linkDown", "linkUp"}:
        i = payload.get("ifindex")
        if i is None:
            return None
        varbinds.extend(((IFTABLE+(1, i), types.Integer32(i)),
                         (IFTABLE+(7, i), types.Integer32(1 if payload.get("admin", True) else 2)),
                         (IFTABLE+(8, i), types.Integer32(1 if payload.get("oper", event["type"] == "linkUp") else 2))))
    elif event["type"] in SENSOR_EVENTS:
        sensor = next((s for s in snapshot["device"].get("sensors", []) if s["id"] == payload.get("sensor")), None)
        if sensor:
            i = sensor["index"]
            varbinds.extend(((oid_tuple("1.3.6.1.2.1.99.1.1.1.1")+(i,), types.Integer32(sensor.get("type", 8))),
                             (oid_tuple("1.3.6.1.2.1.99.1.1.1.4")+(i,), types.Integer32(payload.get("value", sensor["value"]))),
                             (oid_tuple("1.3.6.1.2.1.99.1.1.1.5")+(i,), types.Integer32(payload.get("status", sensor.get("status", 1)))),
                             (oid_tuple("1.3.6.1.2.1.47.1.1.1.1.7")+(i,), types.OctetString(sensor.get("name", sensor["id"])))))
    return varbinds


def sensor_rmon_varbinds(event, snapshot):
    payload = event.get("payload", {})
    sensor = next((s for s in snapshot["device"].get("sensors", []) if s["id"] == payload.get("sensor")), None)
    if not sensor:
        return None
    value = payload.get("value", sensor["value"])
    direction = payload.get("direction") or sensor_direction(sensor)
    rising = direction == "rising"
    index = sensor.get("index", 1)
    varbinds = [(oid_tuple("1.3.6.1.2.1.1.3.0"),
                 types.TimeTicks(max(0, (event["time_ns"]-snapshot["runtime"]["boot_ns"])//10_000_000) % 2**32)),
                (oid_tuple("1.3.6.1.6.3.1.1.4.1.0"), types.ObjectIdentifier(RMON_RISING if rising else RMON_FALLING)),
                (RMON_COLUMNS["index"], types.Integer32(index)),
                (RMON_COLUMNS["variable"], types.ObjectIdentifier(oid_tuple("1.3.6.1.2.1.99.1.1.1.4")+(index,))),
                (RMON_COLUMNS["sample"], types.Integer32(1)),
                (RMON_COLUMNS["value"], types.Integer32(max(-2**31, min(2**31-1, int(value))))),
                (RMON_COLUMNS["rising" if rising else "falling"],
                 types.Integer32(sensor.get("threshold", 80) if rising else sensor.get("low_threshold", 0)))]
    return varbinds


def rmon_varbinds(event, snapshot):
    payload = event.get("payload", {})
    direction = RESOURCE_EVENTS[event["type"]]
    uptime = max(0, (event["time_ns"]-snapshot["runtime"]["boot_ns"])//10_000_000) % 2**32
    if payload.get("resource"):
        variable, value, threshold = resource_variable(snapshot, payload["resource"], payload["value"], payload["threshold"])
        index, sample = {"cpu": 1, "memory": 2}[payload["resource"]], 1
    elif payload.get("ifindex") is not None:
        ifindex = payload["ifindex"]
        if not any(p.get("ifindex") == ifindex for p in (snapshot["device"].get("interfaces") or {}).values()):
            return None
        counts = {key: payload.get(key, 0) for key in ("in_errors", "out_errors", "in_discards", "out_discards")}
        if not any(counts.values()) and not any(key in payload for key in counts):
            counts["in_errors"] = 1
        inbound = counts["in_errors"]+counts["in_discards"]
        variable = oid_tuple(f"1.3.6.1.2.1.2.2.1.{14 if inbound else 20}.{ifindex}")
        value = inbound or counts["out_errors"]+counts["out_discards"]
        threshold, index, sample = 1, 1000+ifindex, 2
    else:
        return None
    varbinds = [(oid_tuple("1.3.6.1.2.1.1.3.0"), types.TimeTicks(uptime)),
                (oid_tuple("1.3.6.1.6.3.1.1.4.1.0"), types.ObjectIdentifier(RMON_RISING if direction == "rising" else RMON_FALLING)),
                (RMON_COLUMNS["index"], types.Integer32(index)),
                (RMON_COLUMNS["variable"], types.ObjectIdentifier(variable)),
                (RMON_COLUMNS["sample"], types.Integer32(sample)),
                (RMON_COLUMNS["value"], types.Integer32(max(0, min(2**31-1, int(value))))),
                (RMON_COLUMNS["rising" if direction == "rising" else "falling"], types.Integer32(max(0, min(2**31-1, int(threshold)))))]
    return varbinds


def entity_varbinds(event, snapshot):
    payload = event.get("payload", {})
    if "index" not in payload:
        return None
    uptime = max(0, (event["time_ns"]-snapshot["runtime"]["boot_ns"])//10_000_000) % 2**32
    index = payload["index"]
    def column(name):
        return ENTITY_COLUMNS[name]+(index,)
    varbinds = [(oid_tuple("1.3.6.1.2.1.1.3.0"), types.TimeTicks(uptime)),
                (oid_tuple("1.3.6.1.6.3.1.1.4.1.0"), types.ObjectIdentifier(ENTITY_CHANGE)),
                (ENTITY_LAST_CHANGE, types.TimeTicks(uptime)),
                (column("index"), types.Integer32(index)),
                (column("class"), types.Integer32(payload.get("class", 9))),
                (column("name"), types.OctetString(payload.get("name", payload.get("card", "entity"))))]
    if "serial" in payload:
        varbinds.append((column("serial"), types.OctetString(payload["serial"])))
    varbinds.append((column("fru"), types.Integer32(1 if payload.get("fru", True) else 2)))
    return varbinds


class NotificationSender:
    def __init__(self, agent, targets, metrics, capacity=1024, rate=1000):
        if capacity <= 0 or rate <= 0:
            raise ValueError("Notification capacity and rate must be positive")
        self.agent, self.client, self.metrics = agent, agent.client, metrics
        self.targets = targets
        self.queue = asyncio.Queue(maxsize=capacity)
        self.rate = rate
        self.cursor = 0
        self.tasks = []
        self.originator = ntforg.NotificationOriginator()
        for n, target in enumerate(targets):
            name = f"notify-{n}"
            version = target.get("version", "v2c")
            community = target.get("community", agent.credentials["community"])
            security = self.agent.credentials["v3_user"] if version == "v3" else ("read" if community == agent.credentials["community"] else f"notify-community-{n}")
            if version != "v3":
                config.add_v1_system(agent.engine, name+"-community", community, transportTag=name, securityName=security)
                if security != "read":
                    config.add_vacm_user(agent.engine, 2, security, "noAuthNoPriv", (1,), (0, 0), (1,))
            config.add_target_parameters(agent.engine, name, security, "authPriv" if version == "v3" else "noAuthNoPriv", mpModel=3 if version == "v3" else 1)
            config.add_target_address(agent.engine, name, udp.DOMAIN_NAME, (target["address"], target.get("port", 162)), name,
                timeout=int(target.get("timeout", 1)*100), retryCount=target.get("retries", 2), tagList=name)
            config.add_notification_target(agent.engine, name, name, name, notifyType=target.get("type", "trap"))

    async def start(self):
        if self.targets:
            self.cursor = await self.client.call("notification_cursor")
            self.tasks = [asyncio.create_task(self.poll()), asyncio.create_task(self.consume())]

    async def record(self, event, status, **details):
        await self.client.call("delivery", event_id=event["id"], status=status, payload=details)

    async def poll(self):
        while True:
            try:
                events = await self.client.call("events", after=self.cursor, limit=1000)
                for event in events:
                    if not event.get("completed") and event_varbinds(event, self.client.current) is not None:
                        try:
                            self.queue.put_nowait(event)
                            self.metrics.increment("notifications_queued_total")
                        except asyncio.QueueFull:
                            self.metrics.increment("notifications_dropped_total")
                            await self.record(event, "dropped", reason="queue_full")
                    self.cursor = event["id"]
                self.metrics.gauges["notification_queue_depth"] = self.queue.qsize()
                self.metrics.gauges["notification_poll_age_seconds"] = 0
            except Unavailable:
                self.metrics.increment("notification_control_errors_total")
            except Exception:
                self.metrics.increment("notification_poll_errors_total")
                self.metrics.gauges["notification_poll_age_seconds"] = .1
                await asyncio.sleep(.5)
            await asyncio.sleep(.1)

    async def send(self, event, n, target):
        self.agent.synchronize_identity()
        binds = event_varbinds(event, self.client.current)
        if binds is None:
            return
        future = asyncio.get_running_loop().create_future()
        def complete(snmp_engine, handle, indication, status, index, varbinds, context):
            if not future.done():
                future.set_result((indication, int(status)))
        started = time.monotonic()
        try:
            self.originator.send_varbinds(self.agent.engine, f"notify-{n}", None, b"", binds, cbFun=complete)
            self.metrics.increment("notifications_sent_total")
            if target.get("type", "trap") == "inform":
                indication, status = await asyncio.wait_for(future, (target.get("retries", 2)+6)*target.get("timeout", 1)+2)
                outcome = "confirmed" if indication is None and not status else "failed"
                self.metrics.increment(f"notifications_{outcome}_total")
                await self.record(event, outcome, target=n, error=str(indication) if indication else status, retries=target.get("retries", 2))
            else:
                await self.record(event, "sent", target=n)
        except Unavailable:
            raise
        except Exception as exc:
            self.metrics.increment("notifications_failed_total")
            await self.record(event, "failed", target=n, error=str(exc))
        self.metrics.observe("notification_send_seconds", time.monotonic()-started)

    async def consume(self):
        while True:
            event = await self.queue.get()
            try:
                attempts = 0
                while True:
                    try:
                        for n, target in enumerate(self.targets):
                            await self.send(event, n, target)
                        await self.record(event, "processed")
                        await self.client.call("notification_cursor", value=event["id"])
                        break
                    except Unavailable:
                        self.metrics.increment("notification_control_errors_total")
                        await asyncio.sleep(.1)
                    except Exception:
                        attempts += 1
                        self.metrics.increment("notification_consume_errors_total")
                        if attempts >= 5:
                            self.metrics.increment("notifications_dropped_total")
                            break
                        await asyncio.sleep(min(2**attempts, 30))
            finally:
                self.queue.task_done()
            await asyncio.sleep(1/max(1, self.rate))

    async def close(self):
        for task in self.tasks:
            task.cancel()
        await asyncio.gather(*self.tasks, return_exceptions=True)
