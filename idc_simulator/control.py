"""The sole state writer; all protocols use this same transaction interface."""
from copy import deepcopy
import time
from uuid import uuid4
from hashlib import sha256
import json
from idc_simulator.identity import host_boot_id
from idc_simulator.errors import SimulationError, Conflict, NotFound
from idc_simulator.model import normalize_lab, validate_config, stable_hex, endpoint, normalize_l3
from idc_simulator.topology import derive, settle_counter
from idc_simulator.storage import Store
from idc_simulator.operations import OperationContext, apply as apply_operation

INJECTABLE = {
    "coldStart": (), "warmStart": (), "authenticationFailure": (),
    "linkUp": ("ifindex",), "linkDown": ("ifindex",),
    "sensorAlarm": ("sensor",), "sensorClear": ("sensor",),
    "resourceAlarm": ("resource", "value", "threshold"), "resourceClear": ("resource", "value", "threshold"),
    "interfaceErrors": ("ifindex",), "interfaceErrorsClear": ("ifindex",),
    "cardInserted": ("index",), "cardRemoved": ("index",),
}

_RUNTIME_ROW_CONTAINERS = ("oper", "last_change", "lldp_since", "lldp_cache",
                           "fdb_cache", "arp_cache", "alarms")


def draft_runtime_row(row):
    draft = dict(row)
    for key in _RUNTIME_ROW_CONTAINERS:
        value = row.get(key)
        if isinstance(value, dict):
            draft[key] = {entry: dict(item) if isinstance(item, dict) else item
                          for entry, item in value.items()}
    counters = row.get("counters")
    if isinstance(counters, dict):
        draft["counters"] = {port: dict(counter) if isinstance(counter, dict) else counter
                             for port, counter in counters.items()}
    return draft


class Controller:
    def __init__(self, database, clock=time.monotonic_ns):
        self.store = Store(database)
        self.clock = clock
        self.labs = self.store.load()
        self.subscribers = []
        self.control_epoch = str(uuid4())
        self.busy_labs = set()
        now = self.clock()
        for state in self.labs.values():
            for configs in (state["lab"]["devices"], state["startup"]):
                for dev, config in configs.items():
                    config.setdefault("forwarding", config["profile"] != "generic/linux/server")
                    for port, p in config["interfaces"].items():
                        raw_mac = "02"+stable_hex(state["lab"]["seed"], dev+":"+port, 10)
                        p.setdefault("mac", ":".join(raw_mac[i:i+2] for i in range(0,12,2)))
                        p.setdefault("mode", "routed" if any(l.get("interface") == port for l in config["l3_interfaces"]) else "switchport")
                    for l3 in config["l3_interfaces"]:
                        normalize_l3(l3, config)
            for link in state["lab"]["links"]:
                link.setdefault("mode", "access" if "vlan" in link else "trunk")
                link.setdefault("lldp_scope", "logical")
            identity = host_boot_id()
            same_host_boot = state.get("host_boot_id") == identity if state.get("host_boot_id") and identity else all(now >= r["boot_ns"] for r in state["runtime"].values())
            for r in state["runtime"].values():
                if same_host_boot:
                    continue
                r["boot_ns"] = now
                r["engine_start_ns"] = now
                for c in r.get("counters", {}).values():
                    c["time_ns"] = now
                r["lldp_cache"] = {}; r["fdb_cache"] = {}; r["arp_cache"] = {}
            state["host_boot_epoch"] = int(time.time()-time.monotonic())
            state["host_boot_id"] = identity
            derive(state, now)

    def create(self, raw):
        lab = normalize_lab(raw)
        if lab["name"] in self.labs:
            raise Conflict("Lab already exists")
        existing = {d for s in self.labs.values() for d in s["lab"]["devices"]}
        if existing & set(lab["devices"]):
            raise Conflict("Device IDs must be globally unique")
        addresses = {d["management_ip"] for s in self.labs.values() for d in s["lab"]["devices"].values()}
        if addresses & {d["management_ip"] for d in lab["devices"].values()}:
            raise Conflict("Management IPs must be globally unique")
        now = self.clock()
        state = {"lab": lab, "revision": 1, "status": "stopped", "runtime": {}, "startup": deepcopy(lab["devices"]),
                 "host_boot_epoch": int(time.time()-time.monotonic()), "host_boot_id":host_boot_id()}
        for dev in lab["devices"]:
            state["runtime"][dev] = {"engine_id": "80004fb805"+stable_hex(lab["seed"], lab["name"]+":"+dev, 22),
                "engine_boots": 0, "boot_ns": now, "online": True, "counters": {}, "last_change": {}}
        derive(state, now)
        self.store.commit(lab["name"], state, [{"type": "labCreated", "time_ns": now}])
        self.labs[lab["name"]] = state
        return {"id": lab["name"], "revision": 1, "status": "stopped"}

    def locate(self, dev):
        for lab_id, state in self.labs.items():
            if dev in state["lab"]["devices"]:
                return lab_id, state
        raise NotFound(f"Unknown device: {dev}")

    def snapshot(self, dev, control_epoch=None, state_seq=None, conditional=False):
        lab_id, state = self.locate(dev)
        now = self.clock()
        self._fresh(state, now)
        version = {"lab_id":lab_id,"revision":state["revision"],"config_revision":state["runtime"][dev].get("config_revision",1),
            "topology_revision":state.get("topology_revision",1),"control_epoch":self.control_epoch,
            "state_seq":state["_state_sequences"][dev],"effective_ns":now}
        if conditional and control_epoch == self.control_epoch and state_seq == version["state_seq"]:
            return {"mode":"not_modified",**version}
        snapshot = deepcopy({**version,"device":state["lab"]["devices"][dev],
            "runtime":state["runtime"][dev],"tables":state["tables"][dev]})
        return {"mode":"full","snapshot":snapshot,**version} if conditional else snapshot

    def transact(self, dev, operations, revision, request_id=None, preview=False, scope="lab"):
        lab_id, old = self.locate(dev)
        if lab_id in self.busy_labs:
            raise Conflict("Lab lifecycle operation is in progress")
        key = f"{dev}:{request_id}" if request_id else None
        fingerprint = sha256(json.dumps({"device":dev,"operations":operations,"revision":revision},sort_keys=True,separators=(",",":"),allow_nan=False).encode()).hexdigest()
        if key and not preview:
            cached = self.store.operation(key)
            if cached:
                if self.store.operation_fingerprint(key) != fingerprint:
                    raise Conflict("Idempotency-Key reused with a different request (or an unverifiable legacy request)")
                return cached
        scoped_valid = scope == "device" and old["runtime"][dev].get("config_revision",1) <= revision <= old["revision"] and old.get("topology_revision",1) <= revision
        if revision != old["revision"] and not scoped_valid:
            raise Conflict(f"Revision conflict: expected {revision}, current {old['revision']}")
        now = self.clock()
        state = self._transaction_draft(old, dev, operations)
        for runtime in state["runtime"].values():
            for counter in runtime.get("counters", {}).values():
                settle_counter(counter, now)
        events = []
        lag_changed = False
        context = OperationContext(state, dev, now, events)
        for op in operations:
            apply_operation(context, op)
        d = context.device
        lag_changed = context.topology_changed
        validate_config(d)
        if lag_changed:
            raw = deepcopy(state["lab"])
            raw["devices"] = list(raw["devices"].values())
            for config in raw["devices"]:
                config["interfaces"] = list(config["interfaces"].values())
            state["lab"] = normalize_lab(raw)
            d = state["lab"]["devices"][dev]
        derive(state, now, previous=old)
        for peer, config in state["lab"]["devices"].items():
            for port, p in config["interfaces"].items():
                before = old["runtime"][peer].get("oper", {}).get(port)
                if before is not None and before != p["oper"] and p["trap_enabled"]:
                    events.append({"type": "linkUp" if p["oper"] else "linkDown", "device_id": peer, "time_ns": now,
                        "payload": {"ifindex": p["ifindex"], "ifname": p["name"], "admin": p["admin"], "oper": p["oper"]}})
        config_changed = any(op["kind"] != "worker_started" for op in operations)
        state["revision"] += int(config_changed)
        if config_changed:
            state["runtime"][dev]["config_revision"] = state["revision"]
            if any(op["kind"] in {"lag","l3","link","host","vlan","route","firewall","reboot","cross_connect"} or
                   (op["kind"] == "interface" and set(op["fields"]) & {"admin","mode","pvid","allowed_vlans","speed_bps"}) for op in operations):
                state["topology_revision"] = state["revision"]
        events.append({"type": "configChanged", "device_id": dev, "time_ns": now, "payload": {"operations": operations}})
        result = {"operation_id": request_id or str(uuid4()), "revision": state["revision"], "status": "complete"}
        if preview:
            return deepcopy(d)
        touched = None if lag_changed else {dev}
        self.store.commit(lab_id, state, events, (key or f"{dev}:{result['operation_id']}", result, fingerprint), devices=touched, runtime=touched)
        self.labs[lab_id] = state
        for callback in self.subscribers:
            callback(events)
        return result

    @staticmethod
    def _transaction_draft(old, dev, operations):
        global_kinds = {"lag", "l3", "link", "host", "flow", "vlan", "route", "firewall", "cross_connect"}
        if any(op["kind"] in global_kinds for op in operations):
            lab = deepcopy(old["lab"])
        else:
            lab = dict(old["lab"])
            devices = dict(lab["devices"])
            for name, config in devices.items():
                if name == dev:
                    devices[name] = deepcopy(config)
                    continue
                peer = dict(config)
                peer_interfaces = dict(config["interfaces"])
                for port, interface in peer_interfaces.items():
                    peer_interfaces[port] = dict(interface)
                peer["interfaces"] = peer_interfaces
                peer["l3_interfaces"] = [dict(item) for item in config["l3_interfaces"]]
                devices[name] = peer
            lab["devices"] = devices

        runtime = {name: draft_runtime_row(row) for name, row in old["runtime"].items()}
        startup = dict(old["startup"])
        draft = {key: value for key, value in old.items()
                 if key not in {"tables", "_derived_plan", "lab", "runtime", "startup"}}
        for key in ("_state_signatures", "_state_sequences"):
            if isinstance(draft.get(key), dict):
                draft[key] = dict(draft[key])
        draft.update(lab=lab, runtime=runtime, startup=startup)
        return draft

    def lab_status(self, lab_id, status):
        if lab_id not in self.labs:
            raise NotFound("Unknown lab")
        state = dict(self.labs[lab_id]); state["status"] = status
        self.store.commit(lab_id, state, devices=(), runtime=())
        self.labs[lab_id] = state
        return {"id": lab_id, "status": status, "revision": state["revision"]}

    def checkpoint(self):
        self.store.checkpoint(self.labs)

    def preview_lab(self, lab_id, raw, revision, save_startup=False):
        from idc_simulator.lifecycle import replacement
        _, summary = replacement(self,lab_id,raw,revision,save_startup)
        return summary

    def replace_lab(self, lab_id, raw, revision, save_startup=False):
        from idc_simulator.lifecycle import replacement
        if lab_id in self.busy_labs:
            raise Conflict("Lab lifecycle operation is in progress")
        if lab_id not in self.labs:
            raise NotFound("Unknown lab")
        if self.labs[lab_id]["status"] != "stopped":
            raise Conflict("Stop the lab before replacing its configuration")
        state, summary = replacement(self,lab_id,raw,revision,save_startup)
        self.store.replace_lab(lab_id,state,summary["removed_devices"],
            [{"type":"labReconfigured","time_ns":self.clock(),"payload":summary}])
        self.labs[lab_id] = state
        return {**summary,"revision":state["revision"],"status":"stopped"}

    def delete_lab(self, lab_id, revision):
        if lab_id not in self.labs:
            raise NotFound("Unknown lab")
        state = self.labs[lab_id]
        if state["revision"] != revision:
            raise Conflict("Lab revision changed")
        if state["status"] != "stopped":
            raise Conflict("Lab workers must be stopped before deletion")
        self.store.delete_lab(lab_id,state,self.clock())
        del self.labs[lab_id]
        return {"id":lab_id,"revision":revision,"status":"deleted"}

    def prune(self, **options):
        return self.store.prune(**options)

    def _fresh(self, state, now):
        if now - state.get("derived_ns", 0) >= 200_000_000:
            derive(state, now, time_only=True)

    def inject_notifications(self, lab_id, device_ids, count=1, kind="coldStart", payload=None):
        if lab_id not in self.labs or not 1 <= count <= 10000 or not device_ids:
            raise SimulationError("Invalid notification pressure batch")
        state = self.labs[lab_id]
        if kind not in INJECTABLE:
            raise SimulationError("Unknown notification type")
        if any(dev not in state["lab"]["devices"] for dev in device_ids):
            raise SimulationError("Unknown pressure device")
        missing = [key for key in INJECTABLE[kind] if key not in (payload or {})]
        if missing:
            raise SimulationError(f"{kind} requires payload: {', '.join(missing)}")
        now = self.clock()
        events = [{"device_id": device_ids[n%len(device_ids)], "type": kind, "time_ns": now,
                  "payload": {**(payload or {}), "pressure": True}} for n in range(count)]
        from idc_simulator.notifications import STANDARD, RESOURCE_EVENTS, ENTITY_EVENTS, SENSOR_EVENTS
        from idc_simulator.profiles import load_profile
        unmapped = []
        for dev in dict.fromkeys(device_ids):
            config = state["lab"]["devices"][dev]
            if kind in SENSOR_EVENTS:
                sensors = config.get("sensors") or []
                if not any(s["id"] == (payload or {}).get("sensor") for s in sensors):
                    unmapped.append(dev)
                continue
            if kind in {"interfaceErrors", "interfaceErrorsClear"}:
                indexes = {p.get("ifindex") for p in (config.get("interfaces") or {}).values()}
                if (payload or {}).get("ifindex") not in indexes:
                    unmapped.append(dev)
                continue
            if kind in STANDARD or kind in RESOURCE_EVENTS or kind in ENTITY_EVENTS:
                continue
            private = load_profile(config["profile"])["traps"].get("private") or {}
            if kind not in private:
                unmapped.append(dev)
        self.store.append_events(lab_id, state["revision"], events)
        return {"events": count, "revision": state["revision"], "first_event_id": events[0]["id"],
                "last_event_id": events[-1]["id"], "unmapped_devices": unmapped}

    def topology(self, lab_id):
        if lab_id not in self.labs:
            raise NotFound("Unknown lab")
        state = self.labs[lab_id]
        self._fresh(state, self.clock())
        lab = state["lab"]
        def port_identity(ep):
            dev, p = endpoint(lab, ep)
            return {"device": dev, "port": p["id"], "name": p["name"], "ifindex": p["ifindex"], "mac": p["mac"],
                "chassis_mac": lab["devices"][dev]["chassis_mac"], "admin": p["admin"], "oper": p["oper"], "members": p.get("members", []),
                "member_ports": [dict(lab["devices"][dev]["interfaces"][member]) for member in p.get("members",[])]}
        links = []
        for link in lab["links"]:
            a, b = link["endpoints"]
            links.append({**link, "source": port_identity(a), "target": port_identity(b),
                "oper": endpoint(lab,a)[1]["oper"] and endpoint(lab,b)[1]["oper"],
                "lldp_discovered": [any(n["remote_device"] == peer and n["ifindex"] in {endpoint(lab,local)[1]["ifindex"], *(lab["devices"][dev]["interfaces"][m]["ifindex"] for m in endpoint(lab,local)[1].get("members",[]))} for n in state["tables"][dev]["lldp"])
                    for local, dev, peer in ((a,a.split(":")[0],b.split(":")[0]),(b,b.split(":")[0],a.split(":")[0]))]})
        return deepcopy({"lab_id": lab_id, "revision": state["revision"], "devices": list(lab["devices"].values()), "links": links})

    def diagnose_ping(self, lab_id, source, destination, vrf=None, count=5, source_device=None):
        from idc_simulator.diagnostics import ping
        if lab_id not in self.labs:
            raise NotFound("Unknown lab")
        state = self.labs[lab_id]
        self._fresh(state, self.clock())
        return ping(state, source, destination, vrf, count, source_device)

    def locate_address(self, lab_id, **query):
        from idc_simulator.diagnostics import locate_address
        if lab_id not in self.labs:
            raise NotFound("Unknown lab")
        state, now = self.labs[lab_id], self.clock()
        self._fresh(state, now)
        return locate_address(state, now, **query)
