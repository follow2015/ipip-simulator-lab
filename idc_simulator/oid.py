"""Typed, numeric ordered OID snapshots. Each request samples exactly once."""
from bisect import bisect_right
from ipaddress import ip_address, ip_interface
from pysnmp.proto import rfc1902 as types, rfc1905
from pysnmp.smi import error
from idc_simulator.model import mac_bytes
from idc_simulator.profiles import load_profile
from idc_simulator.topology import sample_counter

SYSTEM = (1, 3, 6, 1, 2, 1, 1)
IFTABLE = (1, 3, 6, 1, 2, 1, 2, 2, 1)
IFX = (1, 3, 6, 1, 2, 1, 31, 1, 1, 1)
LLDP = (1, 0, 8802, 1, 1, 2)


def oid_tuple(value):
    return tuple(int(x) for x in value.strip(".").split(".")) if isinstance(value, str) else tuple(value)


class OidProvider:
    def __init__(self, snapshot, now, context_name="default"):
        self.snapshot = snapshot
        self.values = {}
        self.context_name = context_name
        self._build(now)
        self.index = tuple(sorted(self.values))
        columns = [IFTABLE, IFX, LLDP+(1,3,7,1), LLDP+(1,3,8,1), LLDP+(1,4,1,1), LLDP+(1,4,2,1)]
        columns.append(oid_tuple("1.3.6.1.2.1.10.7.2.1"))
        columns.extend(oid_tuple(v) for v in ("1.3.6.1.2.1.17.1.4.1", "1.3.6.1.2.1.17.7.1.4.5.1", "1.3.6.1.2.1.17.7.1.2.2.1", "1.3.6.1.2.1.17.4.3.1", "1.3.6.1.2.1.17.7.1.4.2.1", "1.3.6.1.2.1.17.7.1.4.3.1", "1.3.6.1.2.1.4.22.1", "1.3.6.1.2.1.4.35.1", "1.3.6.1.2.1.47.1.1.1.1", "1.3.6.1.2.1.99.1.1.1", "1.3.6.1.2.1.25.1.1", "1.3.6.1.2.1.25.2.3.1", "1.3.6.1.2.1.25.3.3.1", "1.3.6.1.2.1.31.1.2.1"))
        columns.extend(oid_tuple(entry["oid"])[:-1] for entry in
                       load_profile(snapshot["device"]["profile"])["mib"].get("private", []))
        self.objects = {next((o[:len(base)+1] for base in columns if o[:len(base)] == base), o[:-1]) for o in self.index}
        for base, fields in ((LLDP+(1,4,1,1), range(4,13)), (LLDP+(1,4,2,1), range(3,6)),
                (oid_tuple("1.3.6.1.2.1.4.20.1"), range(1,6)), (oid_tuple("1.3.6.1.2.1.4.21.1"), range(1,14)),
                (oid_tuple("1.3.6.1.2.1.4.22.1"), range(1,5)), (oid_tuple("1.3.6.1.2.1.4.35.1"), range(1,9)),
                (oid_tuple("1.3.6.1.2.1.17.7.1.2.2.1"), range(1,4)), (oid_tuple("1.3.6.1.2.1.17.4.3.1"), range(1,4))):
            self.objects.update(base+(field,) for field in fields)

    def put(self, oid, kind, value):
        oid = oid_tuple(oid)
        if oid in self.values:
            raise ValueError(f"Duplicate OID {oid}")
        self.values[oid] = getattr(types, kind)(value)

    def _build(self, now):
        s = self.snapshot; d, r, tables = s["device"], s["runtime"], s["tables"]
        profile = load_profile(d["profile"])
        ticks = max(0, (now-r["boot_ns"])//10_000_000) % 2**32
        for col, kind, value in ((1, "OctetString", bytes.fromhex(r["engine_id"])), (2, "Integer32", r["engine_boots"]),
                (3, "Integer32", max(0, (now-r.get("engine_start_ns", r["boot_ns"]))//1_000_000_000)), (4, "Integer32", 65507)):
            self.put((1, 3, 6, 1, 6, 3, 10, 2, 1, col, 0), kind, value)
        self.put(SYSTEM+(1, 0), "OctetString", profile["metadata"]["sys_descr"])
        self.put(SYSTEM+(2, 0), "ObjectIdentifier", profile["metadata"]["sys_object_id"])
        self.put(SYSTEM+(3, 0), "TimeTicks", ticks)
        for col, field in ((4, "contact"), (5, "hostname"), (6, "location")):
            self.put(SYSTEM+(col, 0), "OctetString", d.get(field, ""))
        self.put(SYSTEM+(7, 0), "Integer32", 72)
        ports = list(d["interfaces"].values()) + [dict(id=l["id"], ifindex=l["ifindex"], name=l["id"], type=53, speed_bps=1_000_000_000,
            admin=l.get("admin",True), oper=l.get("oper",True), mac=l.get("mac",d["chassis_mac"]), description="L3 interface", mtu=1500, trap_enabled=False) for l in d["l3_interfaces"] if l["vrf"] == self.context_name and l["ifindex"] not in {p["ifindex"] for p in d["interfaces"].values()}]
        self.put("1.3.6.1.2.1.2.1.0", "Integer32", len(ports))
        for p in ports:
            i = p["ifindex"]
            counter = sample_counter(r["counters"].get(p["id"], {"time_ns": now}), now)
            columns = {1: ("Integer32", i), 2: ("OctetString", p["name"]), 3: ("Integer32", p["type"]),
                4: ("Integer32", p["mtu"]), 5: ("Gauge32", min(2**32-1, p["speed_bps"])),
                6: ("OctetString", mac_bytes(p.get("mac", d["chassis_mac"]))), 7: ("Integer32", 1 if p["admin"] else 2),
                8: ("Integer32", 1 if p["oper"] else 2), 9: ("TimeTicks", r.get("last_change", {}).get(p["id"], 0) % 2**32),
                10: ("Counter32", counter.get("in_bytes", 0) % 2**32), 11: ("Counter32", counter.get("in_packets", 0) % 2**32),
                13: ("Counter32", counter.get("in_discards", 0) % 2**32), 14: ("Counter32", counter.get("in_errors", 0) % 2**32),
                16: ("Counter32", counter.get("out_bytes", 0) % 2**32), 17: ("Counter32", counter.get("out_packets", 0) % 2**32),
                19: ("Counter32", counter.get("out_discards", 0) % 2**32), 20: ("Counter32", counter.get("out_errors", 0) % 2**32)}
            for col in (12, 15, 18, 21):
                columns[col] = ("Counter32", 0)
            columns[22] = ("ObjectIdentifier", (0, 0))
            for col, (kind, value) in columns.items():
                self.put(IFTABLE+(col, i), kind, value)
            extended = {1: ("OctetString", p["name"]), 6: ("Counter64", counter.get("in_bytes", 0) % 2**64),
                7: ("Counter64", counter.get("in_packets", 0) % 2**64), 10: ("Counter64", counter.get("out_bytes", 0) % 2**64),
                11: ("Counter64", counter.get("out_packets", 0) % 2**64), 14: ("Integer32", 1 if p["trap_enabled"] else 2),
                15: ("Gauge32", p["speed_bps"]//1_000_000), 16: ("Integer32", 2), 17: ("Integer32", 1),
                18: ("OctetString", p["description"]), 19: ("TimeTicks", counter.get("discontinuity", 0) % 2**32)}
            for col in (2, 3, 4, 5):
                extended[col] = ("Counter32", 0)
            for col in (8, 9, 12, 13):
                extended[col] = ("Counter64", 0)
            for col, (kind, value) in extended.items():
                self.put(IFX+(col, i), kind, value)
            self.put(oid_tuple("1.3.6.1.2.1.10.7.2.1.19")+(i,), "Integer32", 3)
        for col, kind, value in ((1, "Integer32", 4), (2, "OctetString", mac_bytes(d["chassis_mac"])),
            (3, "OctetString", d["hostname"]), (4, "OctetString", profile["metadata"]["sys_descr"]),
            (5, "OctetString", b"\x20\x00"), (6, "OctetString", b"\x20\x00")):
            self.put(LLDP+(1, 3, col, 0), kind, value)
        management_index = (1,4)+tuple(ip_address(d["management_ip"]).packed)
        for col, kind, value in ((3, "Integer32", 5), (4, "Integer32", 2 if d.get("management_ifindex") else 1),
                (5, "Integer32", d.get("management_ifindex", 0)), (6, "ObjectIdentifier", (0,0))):
            self.put(LLDP+(1,3,8,1,col)+management_index, kind, value)
        for p in d["interfaces"].values():
            for member in p.get("members", []):
                self.put((1,3,6,1,2,1,31,1,2,1,3,p["ifindex"],d["interfaces"][member]["ifindex"]), "Integer32", 1)
            for col, kind, value in ((2, "Integer32", 5), (3, "OctetString", p["name"]), (4, "OctetString", p["description"])):
                self.put(LLDP+(1, 3, 7, 1, col, p["lldp_port"]), kind, value)
            if p.get("mode") != "routed":
                self.put(f"1.3.6.1.2.1.17.1.4.1.2.{p['bridge_port']}", "Integer32", p["ifindex"])
                self.put(f"1.3.6.1.2.1.17.7.1.4.5.1.1.{p['bridge_port']}", "Gauge32", p["pvid"])
        self.put("1.3.6.1.2.1.17.1.1.0", "OctetString", mac_bytes(d["chassis_mac"]))
        self.put("1.3.6.1.2.1.17.1.2.0", "Integer32", sum(p.get("mode") != "routed" for p in d["interfaces"].values()))
        self.put("1.3.6.1.2.1.17.1.3.0", "Integer32", 2)
        for neighbor in tables["lldp"]:
            idx = (neighbor["time_mark"] % 2**32, neighbor["local_port"], 1)
            for col, kind, value in ((4, "Integer32", 4), (5, "OctetString", mac_bytes(neighbor["chassis_mac"])),
                (6, "Integer32", 5), (7, "OctetString", neighbor["remote_port"]), (8, "OctetString", neighbor["remote_description"]),
                (9, "OctetString", neighbor["hostname"]), (10, "OctetString", "IDC simulator"),
                (11, "OctetString", b"\x20\x00"), (12, "OctetString", b"\x20\x00")):
                self.put(LLDP+(1, 4, 1, 1, col)+idx, kind, value)
            addr = tuple(ip_address(neighbor["management_ip"]).packed)
            for col, kind, value in ((3, "Integer32", 2 if neighbor["management_ifindex"] else 1), (4, "Integer32", neighbor["management_ifindex"]), (5, "ObjectIdentifier", (0, 0))):
                self.put(LLDP+(1, 4, 2, 1, col)+idx+(1, 4)+addr, kind, value)
        for fdb in tables["fdb"]:
            idx = (fdb["fdb_id"],)+tuple(mac_bytes(fdb["mac"]))
            for col, kind, value in ((1, "OctetString", mac_bytes(fdb["mac"])), (2, "Integer32", fdb["bridge_port"]), (3, "Integer32", 3)):
                self.put((1, 3, 6, 1, 2, 1, 17, 7, 1, 2, 2, 1, col)+idx, kind, value)
        bridge_vlan = d.get("bridge_vlan", min((int(v) for v in d["vlans"]), default=1))
        for fdb in tables["fdb"]:
            if fdb["vlan"] != bridge_vlan:
                continue
            idx = tuple(mac_bytes(fdb["mac"]))
            for col, kind, value in ((1, "OctetString", mac_bytes(fdb["mac"])), (2, "Integer32", fdb["bridge_port"]), (3, "Integer32", 3)):
                self.put((1, 3, 6, 1, 2, 1, 17, 4, 3, 1, col)+idx, kind, value)
        for vlan in d["vlans"].values():
            v = vlan["vlan_id"]
            members = [p["bridge_port"] for p in d["interfaces"].values() if p.get("mode") != "routed" and v in p["allowed_vlans"]]
            untagged = [p["bridge_port"] for p in d["interfaces"].values() if p.get("mode") != "routed" and v == p["pvid"]]
            length = (max((p["bridge_port"] for p in d["interfaces"].values()), default=0)+7)//8
            def bitmap(ports):
                data = bytearray(length)
                for port in ports:
                    data[(port-1)//8] |= 1 << (7-(port-1)%8)
                return bytes(data)
            self.put(f"1.3.6.1.2.1.17.7.1.4.2.1.3.0.{v}", "Gauge32", vlan["fdb_id"])
            for col, kind, value in ((1, "OctetString", vlan["name"]), (2, "OctetString", bitmap(members)),
                (3, "OctetString", bytes(length)), (4, "OctetString", bitmap(untagged)), (5, "Integer32", 1)):
                self.put((1, 3, 6, 1, 2, 1, 17, 7, 1, 4, 3, 1, col, v), kind, value)
        for arp in tables["arp"]:
            if arp["vrf"] != self.context_name:
                continue
            addr = ip_address(arp["ip"])
            if addr.version == 4:
                idx = (arp["ifindex"],)+tuple(addr.packed)
                for col, kind, value in ((1, "Integer32", arp["ifindex"]), (2, "OctetString", mac_bytes(arp["mac"])), (3, "IpAddress", arp["ip"]), (4, "Integer32", 3)):
                    self.put((1, 3, 6, 1, 2, 1, 4, 22, 1, col)+idx, kind, value)
            idx = (arp["ifindex"], 1 if addr.version == 4 else 2, len(addr.packed))+tuple(addr.packed)
            for col, kind, value in ((1, "Integer32", arp["ifindex"]), (2, "Integer32", 1 if addr.version == 4 else 2), (3, "OctetString", addr.packed), (4, "OctetString", mac_bytes(arp["mac"])), (5, "TimeTicks", ticks), (6, "Integer32", 3), (7, "Integer32", 1 if arp["state"] == "reachable" else 2), (8, "Integer32", 1)):
                self.put((1, 3, 6, 1, 2, 1, 4, 35, 1, col)+idx, kind, value)
        local_l3 = [l for l in d["l3_interfaces"] if l["vrf"] == self.context_name]
        for l3 in local_l3:
            address = ip_interface(l3["address"])
            if address.version == 4:
                idx = tuple(address.ip.packed)
                for col,kind,value in ((1,"IpAddress",str(address.ip)),(2,"Integer32",l3["ifindex"]),(3,"IpAddress",str(address.network.netmask)),(4,"Integer32",1),(5,"Integer32",65535)):
                    self.put((1,3,6,1,2,1,4,20,1,col)+idx,kind,value)
        route_rows = []
        for l3 in local_l3:
            addr = ip_interface(l3["address"])
            if addr.version == 4 and l3.get("oper",True):
                route_rows.append({"prefix": str(addr.network),"next_hop":"0.0.0.0","distance":0,"ifindex":l3["ifindex"],"direct":True})
        for route in d["routes"]:
            if route.get("vrf","default") != self.context_name or ip_address(route["next_hop"]).version != 4 or ip_interface(route["prefix"]).version != 4:
                continue
            local = next((l for l in local_l3 if l.get("oper",True) and ip_address(route["next_hop"]) in ip_interface(l["address"]).network),None)
            if local:
                route_rows.append({**route,"ifindex":local["ifindex"],"direct":False})
        emitted = set()
        from ipaddress import ip_network
        for route in sorted(route_rows,key=lambda r:r.get("distance",1)):
            network = ip_network(route["prefix"],strict=False)
            idx = tuple(network.network_address.packed)
            if idx in emitted:
                continue
            emitted.add(idx)
            for col,kind,value in ((1,"IpAddress",str(network.network_address)),(2,"Integer32",route["ifindex"]),(3,"Integer32",route.get("distance",1)),(4,"Integer32",-1),(5,"Integer32",-1),(6,"Integer32",-1),
                    (7,"IpAddress",route["next_hop"]),(8,"Integer32",3 if route["direct"] else 4),(9,"Integer32",2 if route["direct"] else 3),(10,"Integer32",0),(11,"IpAddress",str(network.netmask)),(12,"Integer32",-1),(13,"ObjectIdentifier",(0,0))):
                self.put((1,3,6,1,2,1,4,21,1,col)+idx,kind,value)
        entities = [{"id": "chassis", "index": 1, "class": 3, "name": d["hostname"], "parent": 0, "serial": d["serial"]}]+d["cards"]+d["sensors"]
        for entity in entities:
            i = entity["index"]
            for col, kind, value in ((2, "OctetString", entity.get("descr", entity.get("name", entity["id"]))),
                (3, "ObjectIdentifier", (0, 0)), (4, "Integer32", entity.get("parent", 1)),
                (5, "Integer32", entity.get("class", 8)), (6, "Integer32", entity.get("position", -1)), (7, "OctetString", entity.get("name", entity["id"])),
                (11, "OctetString", entity.get("serial", "")), (12, "OctetString", profile["metadata"]["brand"]),
                (13, "OctetString", profile["metadata"]["model"]), (16, "Integer32", 1)):
                self.put((1, 3, 6, 1, 2, 1, 47, 1, 1, 1, 1, col, i), kind, value)
        for sensor in d["sensors"]:
            i = sensor["index"]
            for col, kind, value in ((1, "Integer32", sensor.get("type", 8)), (2, "Integer32", sensor.get("scale", 9)),
                (3, "Integer32", sensor.get("precision", 0)), (4, "Integer32", sensor["value"]),
                (5, "Integer32", sensor.get("status", 1)), (6, "OctetString", sensor.get("units", "Celsius"))):
                self.put((1, 3, 6, 1, 2, 1, 99, 1, 1, 1, col, i), kind, value)
        resources = d.get("resources") or {}
        memory_total = resources.get("memory_total", 1048576)
        memory_used = resources.get("memory_used", 262144)
        self.put("1.3.6.1.2.1.25.1.1.0", "TimeTicks", ticks)
        self.put("1.3.6.1.2.1.25.2.2.0", "Integer32", memory_total)
        self.put("1.3.6.1.2.1.25.3.3.1.2.1", "Integer32", resources.get("cpu_percent", 10))
        for col, kind, value in ((1, "Integer32", 1), (2, "ObjectIdentifier", "1.3.6.1.2.1.25.2.1.2"), (3, "OctetString", "Physical memory"), (4, "Integer32", 1024), (5, "Integer32", memory_total), (6, "Integer32", memory_used)):
            self.put((1, 3, 6, 1, 2, 1, 25, 2, 3, 1, col, 1), kind, value)
        for entry in profile["mib"].get("private", []):
            if entry.get("sensor"):
                sensor = next((s for s in d["sensors"] if s["id"] == entry["sensor"]), None)
                if sensor is None:
                    continue
                value = sensor.get(entry.get("attribute", "value"), 1)
            else:
                field = entry["field"]
                value = resources.get(field)
                if value is None and field == "memory_percent":
                    value = round(100*memory_used/max(1, memory_total))
            if value is None:
                continue
            self.put(entry["oid"], entry["type"], value)

    def get(self, oid):
        oid = oid_tuple(oid)
        if oid in self.values:
            return self.values[oid]
        known = oid in self.objects or any(oid[:len(base)] == base for base in self.objects)
        return rfc1905.NoSuchInstance() if known else rfc1905.NoSuchObject()

    def get_next(self, oid):
        oid = oid_tuple(oid)
        i = bisect_right(self.index, oid)
        return (self.index[i], self.values[self.index[i]]) if i < len(self.index) else (oid, rfc1905.EndOfMibView())

    def prepare_set(self, varbinds):
        operations = []
        for index, (raw_oid, value) in enumerate(varbinds):
            oid = oid_tuple(raw_oid)
            if oid[:len(SYSTEM)] == SYSTEM and oid[len(SYSTEM):] in {(4, 0), (5, 0), (6, 0)}:
                if not isinstance(value, types.OctetString):
                    raise error.WrongTypeError(idx=index)
                if len(value.asOctets()) > 255:
                    raise error.WrongLengthError(idx=index)
                try:
                    text = value.asOctets().decode("utf-8")
                except UnicodeDecodeError:
                    raise error.WrongValueError(idx=index) from None
                field = {4: "contact", 5: "hostname", 6: "location"}[oid[-2]]
                operations.append({"kind": "system", "fields": {field: text}})
            elif oid[:len(IFTABLE)+1] == IFTABLE+(7,):
                if not isinstance(value, types.Integer32):
                    raise error.WrongTypeError(idx=index)
                if int(value) not in (1, 2):
                    raise error.WrongValueError(idx=index)
                port = next((p["id"] for p in self.snapshot["device"]["interfaces"].values() if IFTABLE+(7, p["ifindex"]) == oid), None)
                if not port:
                    raise error.NoCreationError(idx=index)
                operations.append({"kind": "interface", "port": port, "fields": {"admin": int(value) == 1}})
            else:
                raise error.NotWritableError(idx=index)
        return operations
