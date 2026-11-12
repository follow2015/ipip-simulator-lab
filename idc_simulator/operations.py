"""State operation handlers. The coordinator alone validates and commits drafts."""
from copy import deepcopy
from dataclasses import dataclass
from idc_simulator.errors import SimulationError, NotFound
from idc_simulator.model import endpoint, normalize_l3


@dataclass
class OperationContext:
    state: dict
    dev: str
    now: int
    events: list
    topology_changed: bool = False

    @property
    def device(self):
        return self.state["lab"]["devices"][self.dev]

    @property
    def runtime(self):
        return self.state["runtime"][self.dev]


HANDLERS = {}


def handles(*kinds):
    def register(handler):
        for kind in kinds:
            HANDLERS[kind] = handler
        return handler
    return register


def apply(context, operation):
    kind = operation["kind"]
    if kind not in HANDLERS:
        raise SimulationError(f"Unsupported operation: {kind}")
    HANDLERS[kind](context, operation)


@handles('interface')
def interface(ctx, op):
    d = ctx.device
    if op["port"] not in d["interfaces"]:
        raise NotFound("Unknown interface")
    fields = op["fields"]
    if set(fields) - {"admin", "description", "mtu", "speed_bps", "pvid", "allowed_vlans", "lldp", "trap_enabled", "mode"}:
        raise SimulationError("Unsupported interface field")
    d["interfaces"][op["port"]].update(fields)


@handles('lag')
def lag(ctx, op):
    d = ctx.device
    p = d["interfaces"].get(op["port"])
    if not p or not p.get("members"):
        raise SimulationError("LAG operation requires an existing aggregate interface")
    if not op["fields"] or set(op["fields"]) - {"members", "min_links"}:
        raise SimulationError("Unsupported LAG field")
    p.update(deepcopy(op["fields"]))
    if not p.get("members"):
        raise SimulationError("LAG requires members")
    ctx.topology_changed = True


@handles('system')
def system(ctx, op):
    d = ctx.device
    if set(op["fields"]) - {"hostname", "location", "contact"}:
        raise SimulationError("Unsupported system field")
    d.update(op["fields"])


@handles('vlan')
def vlan(ctx, op):
    d = ctx.device
    v = int(op["vlan"])
    if op.get("delete"):
        d["vlans"].pop(str(v), None)
    else:
        d["vlans"][str(v)] = {"vlan_id": v, "fdb_id": op.get("fdb_id", v), "name": op.get("name", f"VLAN{v}")}


@handles('l3', 'route', 'firewall', 'cross_connect')
def l3(ctx, op):
    d = ctx.device
    kind = op["kind"]
    field = {"l3": "l3_interfaces", "route": "routes", "firewall": "firewall", "cross_connect": "cross_connects"}[kind]
    value = deepcopy(op["value"])
    if kind == "route":
        def matches(v):
            same_target = (v["prefix"], v.get("vrf", "default")) == (value["prefix"], value.get("vrf", "default"))
            return same_target and ("next_hop" not in value or v["next_hop"] == value["next_hop"])
    else:
        def matches(v):
            return v["id"] == value["id"]
    d[field] = [v for v in d[field] if not matches(v)]
    if kind == "l3":
        ctx.topology_changed = True
    if not op.get("delete"):
        if kind == "l3":
            normalize_l3(value, d)
        d[field].append(value)


@handles('save')
def save(ctx, op):
    state = ctx.state
    dev = ctx.dev
    now = ctx.now
    events = ctx.events
    d = ctx.device
    state["startup"][dev] = deepcopy(d)
    events.append({"type": "configSaved", "device_id": dev, "time_ns": now})


@handles('reboot')
def reboot(ctx, op):
    state = ctx.state
    dev = ctx.dev
    now = ctx.now
    events = ctx.events
    r = ctx.runtime
    state["lab"]["devices"][dev] = deepcopy(state["startup"][dev])
    r["boot_ns"] = now; r["engine_boots"] += 1; r["online"] = True
    r["engine_start_ns"] = now
    r["counters"] = {}; r["lldp_cache"] = {}; r["lldp_since"] = {}; r["fdb_cache"] = {}; r["arp_cache"] = {}
    events.append({"type": "coldStart", "device_id": dev, "time_ns": now})
    ctx.topology_changed = True


@handles('worker_started')
def worker_started(ctx, op):
    dev = ctx.dev
    now = ctx.now
    events = ctx.events
    r = ctx.runtime
    r["engine_boots"] += 1
    r["engine_start_ns"] = now
    events.append({"type": "workerStarted", "device_id": dev, "time_ns": now})


@handles('online')
def online(ctx, op):
    r = ctx.runtime
    if not isinstance(op["value"], bool):
        raise SimulationError("Online state must be boolean")
    r["online"] = bool(op["value"])


@handles('link')
def link(ctx, op):
    state = ctx.state
    if "up" in op and not isinstance(op["up"], bool):
        raise SimulationError("Link state must be boolean")
    link = next((l for l in state["lab"]["links"] if l["id"] == op["id"]), None)
    if not link:
        raise NotFound("Unknown link")
    if "up" in op:
        link["up"] = op["up"]
    fields = op.get("fields", {})
    if set(fields) - {"endpoints","mode","allowed_vlans","vlan","blocked","lldp","lldp_scope"}:
        raise SimulationError("Unsupported link field")
    if any(not isinstance(fields[k],bool) for k in ("blocked","lldp") if k in fields):
        raise SimulationError("Link flags must be boolean")
    link.update(deepcopy(fields))
    if fields:
        ctx.topology_changed = True


@handles('host')
def host(ctx, op):
    state = ctx.state
    dev = ctx.dev
    now = ctx.now
    events = ctx.events
    host = next((h for h in state["lab"]["hosts"] if h["id"] == op["id"]), None)
    if not host or set(op["fields"]) - {"device", "interface", "active"}:
        raise SimulationError("Invalid host operation")
    if "active" in op["fields"] and not isinstance(op["fields"]["active"], bool):
        raise SimulationError("Host active state must be boolean")
    moved = any(k in op["fields"] and op["fields"][k] != host[k] for k in ("device", "interface"))
    host.update(op["fields"])
    endpoint(state["lab"], f"{host['device']}:{host['interface']}")
    if moved:
        for rt in state["runtime"].values():
            rt["fdb_cache"] = {k: v for k, v in rt.get("fdb_cache", {}).items() if v["mac"] != host["mac"]}
        events.append({"type": "hostMoved", "device_id": dev, "time_ns": now, "payload": op})


@handles('flow')
def flow(ctx, op):
    state = ctx.state
    flow = next((f for f in state["lab"]["flows"] if f["id"] == op["id"]), None)
    if not flow or op["rate_bps"] < 0:
        raise SimulationError("Invalid flow operation")
    flow["rate_bps"] = int(op["rate_bps"])


@handles('counter_reset')
def counter_reset(ctx, op):
    now = ctx.now
    r = ctx.runtime
    r["counter_generation"] = r.get("counter_generation",0)+1
    for c in r["counters"].values():
        for k in ("in_bytes", "out_bytes", "in_packets", "out_packets",
                  "in_errors", "out_errors", "in_discards", "out_discards"):
            c[k] = 0; c[k+"_remainder"] = 0
        c["discontinuity"] = (now-r["boot_ns"])//10_000_000


@handles('sensor')
def sensor(ctx, op):
    dev = ctx.dev
    now = ctx.now
    events = ctx.events
    d = ctx.device
    r = ctx.runtime
    sensor = next((s for s in d["sensors"] if s["id"] == op["id"]), None)
    if not sensor:
        raise NotFound("Unknown sensor")
    status = op.get("status")
    if status is not None and status not in {1, 2, 3}:
        raise SimulationError("Sensor status must be ok(1), unavailable(2) or nonoperational(3)")
    previous = sensor["value"]
    sensor["value"] = op["value"]
    if status is not None:
        sensor["status"] = status
    active = r.setdefault("alarms", {}).get(op["id"], False)
    new = sensor_alarm(sensor, active, op["value"], status)
    if new != active:
        events.append({"type": "sensorAlarm" if new else "sensorClear", "device_id": dev, "time_ns": now,
                       "payload": {"sensor": op["id"], "value": op["value"], "status": sensor.get("status", 1),
                                   "direction": sensor_direction(sensor, op["value"], previous)}})
    r["alarms"][op["id"]] = new


def sensor_direction(sensor, value=None, previous=None):
    if value is not None and previous is not None and value != previous:
        return "rising" if value > previous else "falling"
    return "falling" if isinstance(sensor.get("low_threshold"), int) or sensor.get("type") == 12 else "rising"


def sensor_alarm(sensor, active, value, status=None):
    if status is None:
        status = sensor.get("status", 1)
    if status != 1:
        return True
    if sensor.get("type") == 12:
        return value == 0
    low, low_clear = sensor.get("low_threshold"), sensor.get("low_clear")
    if isinstance(low, int) and isinstance(low_clear, int):
        return value <= low if not active else value <= low_clear
    high, clear = sensor.get("threshold", 80), sensor.get("clear_threshold", 70)
    return value >= high if not active else value > clear


@handles('resources')
def resources(ctx, op):
    dev, now, events = ctx.dev, ctx.now, ctx.events
    resources = ctx.device.setdefault("resources", {"cpu_percent": 10, "memory_total": 1048576, "memory_used": 262144})
    alarms = ctx.runtime.setdefault("alarms", {})
    thresholds = ctx.device.get("thresholds") or {}
    declared = {"cpu": (op.get("cpu_percent"), thresholds.get("cpu", 80)),
                "memory": (op.get("memory_percent"), thresholds.get("memory", 85))}
    for name, (value, threshold) in declared.items():
        if value is None:
            continue
        if not isinstance(value, int) or not 0 <= value <= 100:
            raise SimulationError("Resource percentage must be 0-100")
        key = f"resource:{name}"
        active = alarms.get(key, False)
        new = value >= threshold if not active else value > threshold-5
        if new != active:
            events.append({"type": "resourceAlarm" if new else "resourceClear", "device_id": dev, "time_ns": now,
                           "payload": {"resource": name, "value": value, "threshold": threshold}})
        alarms[key] = new
        if name == "cpu":
            resources["cpu_percent"] = value
        else:
            total = resources.get("memory_total", 1048576)
            resources["memory_percent"] = value
            resources["memory_used"] = total*value//100


@handles('errors')
def errors(ctx, op):
    d = ctx.device
    port = d["interfaces"].get(op["port"])
    if not port:
        raise NotFound("Unknown interface")
    fields = {key: op.get(key, 0) for key in ("in_errors", "out_errors", "in_discards", "out_discards")}
    if any(not isinstance(v, int) or v < 0 for v in fields.values()):
        raise SimulationError("Error and discard rates must be non-negative integers")
    counters = ctx.runtime.setdefault("counters", {})
    counter = counters.setdefault(op["port"], {"time_ns": ctx.now, "discontinuity": 0})
    for key, value in fields.items():
        counter[f"{key}_rate"] = value
        counter[f"{key}_denominator"] = 1_000_000_000
    active = ctx.runtime.setdefault("alarms", {}).get(f"errors:{op['port']}", False)
    new = any(fields.values())
    if new != active:
        ctx.events.append({"type": "interfaceErrors" if new else "interfaceErrorsClear", "device_id": ctx.dev,
                           "time_ns": ctx.now, "payload": {"port": op["port"], "ifindex": port["ifindex"], **fields}})
    ctx.runtime["alarms"][f"errors:{op['port']}"] = new


@handles('card')
def card(ctx, op):
    dev = ctx.dev
    now = ctx.now
    events = ctx.events
    d = ctx.device
    card = next((c for c in d["cards"] if c["id"] == op["id"]), None)
    if not card or not isinstance(op["present"], bool):
        raise SimulationError("Invalid card operation")
    if card["present"] != op["present"]:
        card["present"] = op["present"]
        events.append({"type": "cardInserted" if card["present"] else "cardRemoved", "device_id": dev, "time_ns": now,
                       "payload": {"card": op["id"], "index": card["index"], "class": card.get("class", 9),
                                   "name": card.get("name", op["id"]), "serial": card.get("serial", ""),
                                   "fru": card.get("fru", True)}})


@handles('trap')
def trap(ctx, op):
    dev = ctx.dev
    now = ctx.now
    events = ctx.events
    if op["type"] not in {"linkDown", "linkUp", "coldStart", "warmStart", "authenticationFailure"}:
        raise SimulationError("Unknown standard notification")
    events.append({"type": op["type"], "device_id": dev, "time_ns": now, "payload": op.get("payload", {})})
