"""Declarative topology validation and stable identities; no protocol imports."""
from copy import deepcopy
from hashlib import sha256
from ipaddress import ip_address, ip_interface, ip_network
from pathlib import Path
import re
import yaml
from idc_simulator.errors import SimulationError
from idc_simulator.profiles import load_profile


def stable_hex(seed, value, length=12):
    return sha256(f"{seed}:{value}".encode()).hexdigest()[:length]


def mac_bytes(value):
    if not isinstance(value, str) or not re.fullmatch(r"[0-9a-fA-F]{2}(:[0-9a-fA-F]{2}){5}", value):
        raise SimulationError(f"Invalid MAC: {value}")
    return bytes.fromhex(value.replace(":", ""))


def integer(value, minimum, maximum, label):
    if type(value) is not int or not minimum <= value <= maximum:
        raise SimulationError(f"{label} must be an integer in {minimum}..{maximum}")


def normalize_l3(l3, device):
    ip_interface(l3["address"])
    l3.setdefault("vrf", "default")
    l3.setdefault("admin", True)
    if l3.get("interface"):
        port = device["interfaces"].get(l3["interface"])
        if not port:
            raise SimulationError("Unknown routed interface")
        port["mode"] = "routed"
        l3.setdefault("ifindex", port["ifindex"])
        l3.setdefault("mac", port["mac"])
    else:
        l3.setdefault("ifindex", 10000+l3.get("vlan", 0))
        l3.setdefault("mac", device["chassis_mac"])
    return l3


def validate_l3(l3, interfaces, vlans):
    address = ip_interface(l3["address"])
    if bool(l3.get("interface")) == (l3.get("vlan") is not None):
        raise SimulationError("L3 interface requires exactly one physical interface or VLAN")
    if not isinstance(l3.get("vrf", "default"), str) or not re.fullmatch(r"[A-Za-z0-9_.-]{1,64}",l3.get("vrf","default")):
        raise SimulationError("Invalid VRF name")
    if not isinstance(l3.get("admin",True),bool):
        raise SimulationError("L3 admin state must be boolean")
    integer(l3["ifindex"],1,2147483647,"L3 ifIndex")
    mac_bytes(l3["mac"])
    if l3.get("interface"):
        p = interfaces.get(l3["interface"])
        if not p or l3["ifindex"] != p["ifindex"] or l3["mac"].lower() != p["mac"].lower():
            raise SimulationError("Routed interface identity must match its physical interface")
    elif str(l3["vlan"]) not in vlans:
        raise SimulationError("L3 interface references undefined VLAN")
    return address


MAX_YAML_BYTES = 4*1024*1024
MAX_YAML_NODES = 200_000


def safe_load(text):
    if not isinstance(text, str):
        raise SimulationError("Topology content must be YAML/JSON text")
    if len(text.encode("utf-8")) > MAX_YAML_BYTES:
        raise SimulationError(f"Topology content exceeds {MAX_YAML_BYTES} bytes")
    try:
        document = yaml.compose(text)
        nodes, stack = 0, [document]
        while stack:
            node = stack.pop()
            if node is None:
                continue
            nodes += 1
            if nodes > MAX_YAML_NODES:
                raise SimulationError(f"Topology expands beyond {MAX_YAML_NODES} nodes")
            if isinstance(node, yaml.MappingNode):
                for key, value in node.value:
                    stack.extend((key, value))
            elif isinstance(node, yaml.SequenceNode):
                stack.extend(node.value)
        return yaml.safe_load(text)
    except yaml.YAMLError as exc:
        raise SimulationError("Invalid YAML/JSON topology") from exc


def load_lab(path):
    return normalize_lab(safe_load(Path(path).read_text()))


def normalize_lab(raw):
    try:
        return _normalize_lab(raw)
    except (KeyError, TypeError, ValueError) as exc:
        if isinstance(exc, SimulationError):
            raise
        raise SimulationError(f"Invalid topology: {exc}") from exc


def _normalize_lab(raw):
    lab = deepcopy(raw)
    if not isinstance(lab, dict) or lab.get("schema_version") != 1:
        raise SimulationError("schema_version must be 1")
    if not re.fullmatch(r"[a-zA-Z0-9_-]{1,48}", lab["name"]):
        raise SimulationError("Invalid lab name")
    lab.setdefault("seed", 1)
    lab.setdefault("defaults", {})
    lab.setdefault("links", [])
    lab.setdefault("hosts", [])
    lab.setdefault("flows", [])
    lab.setdefault("scenarios", {})
    devices, addresses = {}, set()
    network = ip_network(lab["management"]["subnet"])
    if ip_address(lab["management"]["gateway"]) not in network:
        raise SimulationError("Management gateway outside subnet")
    for d in lab["devices"]:
        if not re.fullmatch(r"[a-zA-Z0-9_-]{1,48}", d["id"]) or d["id"] in devices:
            raise SimulationError("Invalid or duplicate device ID")
        addr = ip_address(d["management_ip"])
        if addr.version != 4 or addr not in network or str(addr) in addresses or str(addr) == lab["management"]["gateway"]:
            raise SimulationError("Duplicate/invalid management IPv4")
        addresses.add(str(addr))
        profile = load_profile(d["profile"])
        d.setdefault("forwarding", profile["cli"]["family"] != "linux")
        if not isinstance(d["forwarding"], bool):
            raise SimulationError("Device forwarding must be boolean")
        d["profile_version"] = profile["metadata"]["version"]
        d.setdefault("hostname", d["id"])
        d.setdefault("serial", "SIM-" + stable_hex(lab["seed"], d["id"]).upper())
        raw_mac = "02" + stable_hex(lab["seed"], d["id"], 10)
        d.setdefault("chassis_mac", ":".join(raw_mac[i:i+2] for i in range(0, 12, 2)))
        mac_bytes(d["chassis_mac"])
        d.setdefault("vlans", [1])
        d["vlans"] = {str(v if isinstance(v, int) else v["vlan_id"]):
                       {"vlan_id": v, "fdb_id": v, "name": f"VLAN{v}"} if isinstance(v, int) else v
                       for v in (d["vlans"].values() if isinstance(d["vlans"], dict) else d["vlans"])}
        interfaces = {}
        indexes, names, bridge_ports, lldp_ports = set(), set(), set(), set()
        for n, p in enumerate(d["interfaces"], 1):
            port_mac = "02"+stable_hex(lab["seed"], d["id"]+":"+p["id"], 10)
            p.setdefault("mac", ":".join(port_mac[i:i+2] for i in range(0,12,2)))
            mac_bytes(p["mac"])
            p.setdefault("mode", "switchport")
            p.setdefault("bridge_port", n)
            p.setdefault("lldp_port", n)
            p.setdefault("speed_bps", 1_000_000_000)
            p.setdefault("admin", True)
            p.setdefault("description", "")
            p.setdefault("mtu", 1500)
            p.setdefault("type", 6)
            p.setdefault("lldp", True)
            p.setdefault("trap_enabled", True)
            p.setdefault("pvid", int(next(iter(d["vlans"]), 1)))
            p.setdefault("allowed_vlans", [int(v) for v in d["vlans"]])
            if p["id"] in interfaces or p["ifindex"] in indexes or p["name"] in names or p["bridge_port"] in bridge_ports or p["lldp_port"] in lldp_ports:
                raise SimulationError("Duplicate interface identity/index")
            if not isinstance(p["admin"], bool) or p["ifindex"] <= 0 or p["speed_bps"] <= 0 or p["bridge_port"] <= 0 or p["lldp_port"] <= 0:
                raise SimulationError("Invalid interface parameters")
            indexes.add(p["ifindex"]); names.add(p["name"])
            bridge_ports.add(p["bridge_port"]); lldp_ports.add(p["lldp_port"])
            interfaces[p["id"]] = p
        d["interfaces"] = interfaces
        d.setdefault("l3_interfaces", [])
        d.setdefault("routes", [])
        d.setdefault("firewall", [])
        d.setdefault("sensors", [])
        d.setdefault("cards", [])
        d.setdefault("cross_connects", [])
        d.setdefault("resources", {"cpu_percent": 10, "memory_total": 1048576, "memory_used": 262144})
        for l3 in d["l3_interfaces"]:
            normalize_l3(l3, d)
        for card in d["cards"]:
            card.setdefault("present", True)
        validate_config(d)
        devices[d["id"]] = d
    if not devices:
        raise SimulationError("At least one device required")
    lab["devices"] = devices
    l3_addresses = set()
    for d in devices.values():
        for l3 in d["l3_interfaces"]:
            identity = (l3["vrf"],str(ip_interface(l3["address"]).ip))
            if identity in l3_addresses:
                raise SimulationError("Duplicate L3 address in VRF")
            l3_addresses.add(identity)
    occupied, link_ids = set(), set()
    for link in lab["links"]:
        if link["id"] in link_ids or len(link["endpoints"]) != 2:
            raise SimulationError("Invalid/duplicate link")
        link_ids.add(link["id"])
        link.setdefault("up", True); link.setdefault("lldp", True)
        link.setdefault("blocked", False)
        if any(type(link[k]) is not bool for k in ("up","lldp","blocked")):
            raise SimulationError("Link flags must be boolean")
        link.setdefault("mode", "access" if "vlan" in link else "trunk")
        link.setdefault("lldp_scope", "logical")
        if link["lldp_scope"] not in {"logical", "members"}:
            raise SimulationError("LLDP scope must be logical/members")
        if link["mode"] not in {"access", "trunk", "routed"}:
            raise SimulationError("Link mode must be access/trunk/routed")
        link.setdefault("allowed_vlans", [] if link["mode"] == "routed" else [link.get("vlan", 1)])
        if link["mode"] == "access":
            if "vlan" not in link and not link["allowed_vlans"]:
                raise SimulationError("Access link requires a VLAN")
            link.setdefault("vlan", link["allowed_vlans"][0])
            link["allowed_vlans"] = [link["vlan"]]
        if link["mode"] == "routed" and link["allowed_vlans"]:
            raise SimulationError("Routed link cannot carry bridge VLANs")
        for vlan in link["allowed_vlans"]:
            integer(vlan,1,4094,"Link VLAN")
        for ep in link["endpoints"]:
            dev, port = endpoint(lab, ep)
            owned = {ep, *(f"{dev}:{m}" for m in port.get("members", []))}
            if occupied & owned:
                raise SimulationError(f"Interface in multiple links: {ep}; use explicit LAG")
            occupied.update(owned)
        _, ap = endpoint(lab, link["endpoints"][0]); _, bp = endpoint(lab, link["endpoints"][1])
        if bool(ap.get("members")) != bool(bp.get("members")) or len(ap.get("members", [])) != len(bp.get("members", [])):
            raise SimulationError("LAG link requires paired members at both ends")
    for vlan in {v for link in lab["links"] for v in link["allowed_vlans"]}:
        parent = {d: d for d in devices}
        def root(d):
            while parent[d] != d:
                d = parent[d]
            return d
        for link in lab["links"]:
            if link["mode"] == "routed" or link["blocked"] or vlan not in link["allowed_vlans"]:
                continue
            a, b = [ep.split(":")[0] for ep in link["endpoints"]]
            if not devices[a]["forwarding"] or not devices[b]["forwarding"]:
                continue
            if root(a) == root(b):
                raise SimulationError(f"Ambiguous L2 cycle in VLAN {vlan}; declare blocked link")
            parent[root(a)] = root(b)
    host_ids, host_macs, host_ips = set(), set(), set()
    for host in lab["hosts"]:
        if not isinstance(host.get("id"), str) or not re.fullmatch(r"[a-zA-Z0-9_-]{1,48}", host["id"]):
            raise SimulationError("Invalid host ID")
        endpoint(lab, f"{host['device']}:{host['interface']}")
        mac_bytes(host["mac"])
        address = ip_interface(host["address"])
        host.setdefault("active", True); host.setdefault("vrf", "default")
        if type(host["active"]) is not bool:
            raise SimulationError("Host active state must be boolean")
        integer(host["vlan"],1,4094,"Host VLAN")
        if str(host["vlan"]) not in devices[host["device"]]["vlans"]:
            raise SimulationError("Host references undefined VLAN")
        identity = (host["vrf"],str(address.ip))
        if host["id"] in host_ids or (host["vlan"], host["mac"].lower()) in host_macs or identity in host_ips:
            raise SimulationError("Duplicate host identity")
        if any(identity == (l["vrf"],str(ip_interface(l["address"]).ip)) for d in devices.values() for l in d["l3_interfaces"]):
            raise SimulationError("Host IP duplicates an L3 interface")
        host_ids.add(host["id"]); host_macs.add((host["vlan"], host["mac"].lower())); host_ips.add(identity)
    flow_ids = set()
    for flow in lab["flows"]:
        if flow["id"] in flow_ids or flow["source"] not in host_ids or flow["destination"] not in host_ids or flow["rate_bps"] < 0:
            raise SimulationError("Invalid logical flow")
        flow.setdefault("packet_size", 1000)
        if flow["packet_size"] <= 0:
            raise SimulationError("Invalid packet size")
        flow_ids.add(flow["id"])
    return lab


def endpoint(lab, value):
    try:
        dev, port = value.split(":")
        return dev, lab["devices"][dev]["interfaces"][port]
    except (KeyError, ValueError):
        raise SimulationError(f"Unknown endpoint: {value}") from None


def validate_config(d):
    if not isinstance(d["hostname"], str) or not re.fullmatch(r"[A-Za-z0-9_.-]{1,64}", d["hostname"]):
        raise SimulationError("Invalid hostname")
    for v in d["vlans"].values():
        integer(v["vlan_id"],1,4094,"VLAN ID")
        integer(v["fdb_id"],1,0xffffffff,"FDB ID")
    lag_members = set()
    for p in d["interfaces"].values():
        if p["mode"] not in {"switchport", "routed"}:
            raise SimulationError("Interface mode must be switchport/routed")
        mac_bytes(p["mac"])
        for field, minimum, maximum in (("ifindex",1,2147483647),("bridge_port",1,65535),("lldp_port",1,2147483647),("mtu",576,9216),("speed_bps",1,10**15),("pvid",1,4094)):
            integer(p[field],minimum,maximum,field)
        if not isinstance(p["allowed_vlans"],list) or not isinstance(p["description"],str):
            raise SimulationError("Interface VLANs must be a list and description must be text")
        for vlan in p["allowed_vlans"]:
            integer(vlan,1,4094,"Interface VLAN")
        if any(not isinstance(p[f], bool) for f in ("admin", "lldp", "trap_enabled")) or not 576 <= p["mtu"] <= 9216 or p["speed_bps"] <= 0:
            raise SimulationError("Invalid interface configuration")
        if any(str(v) not in d["vlans"] for v in [p["pvid"], *p["allowed_vlans"]]):
            raise SimulationError("Interface references undefined VLAN")
        if len(p["description"].encode()) > 255:
            raise SimulationError("Interface description too long")
        if p.get("members"):
            if p["id"] in p["members"] or any(member not in d["interfaces"] or d["interfaces"][member].get("members") for member in p["members"]):
                raise SimulationError("Invalid/nested LAG members")
            if len(set(p["members"])) != len(p["members"]):
                raise SimulationError("Duplicate LAG member")
            if lag_members & set(p["members"]):
                raise SimulationError("Interface belongs to multiple LAGs")
            lag_members.update(p["members"])
            if not isinstance(p.get("min_links",1), int) or not 1 <= p.get("min_links",1) <= len(p["members"]):
                raise SimulationError("Invalid LAG min_links")
        if p.get("card") and not any(c["id"] == p["card"] for c in d["cards"]):
            raise SimulationError("Interface references unknown card")
    l3_ids, l3_indexes, l3_addresses = set(), set(), set()
    physical_indexes = {p["ifindex"] for p in d["interfaces"].values()}
    for l3 in d["l3_interfaces"]:
        address = validate_l3(l3,d["interfaces"],d["vlans"])
        identity = (l3["vrf"],str(address.ip))
        if l3["id"] in l3_ids or l3["ifindex"] in l3_indexes or identity in l3_addresses or (not l3.get("interface") and l3["ifindex"] in physical_indexes):
            raise SimulationError("Duplicate L3 identity, address or ifIndex")
        l3_ids.add(l3["id"]); l3_indexes.add(l3["ifindex"]); l3_addresses.add(identity)
    for route in d["routes"]:
        ip_network(route["prefix"], strict=False); ip_address(route["next_hop"])
        integer(route.get("distance",1),1,255,"Route distance")
    for rule in d["firewall"]:
        ip_network(rule["source"], strict=False); ip_network(rule["destination"], strict=False)
        if rule["action"] not in {"allow", "deny"}:
            raise SimulationError("Firewall action must be allow/deny")
    entity_indexes = {1}
    entity_ids = {"chassis"}
    for entity in d["cards"]+d["sensors"]:
        if entity["index"] <= 1 or entity["index"] in entity_indexes or entity["id"] in entity_ids:
            raise SimulationError("Duplicate/invalid ENTITY index")
        entity_indexes.add(entity["index"]); entity_ids.add(entity["id"])
    for entity in d["cards"]+d["sensors"]:
        if entity.get("parent", 1) not in entity_indexes or entity.get("parent", 1) == entity["index"]:
            raise SimulationError("Invalid ENTITY containment")
    for sensor in d["sensors"]:
        if not isinstance(sensor["value"], int) or not -2147483648 <= sensor["value"] <= 2147483647 or sensor.get("clear_threshold", 70) >= sensor.get("threshold", 80):
            raise SimulationError("Invalid sensor value/threshold")
        if sensor.get("status", 1) not in {1, 2, 3}:
            raise SimulationError("Sensor status must be ok(1), unavailable(2) or nonoperational(3)")
        if "low_threshold" in sensor or "low_clear" in sensor:
            low, low_clear = sensor.get("low_threshold"), sensor.get("low_clear")
            if not isinstance(low, int) or not isinstance(low_clear, int) or low >= low_clear:
                raise SimulationError("Sensor low_threshold must be an integer below low_clear")
    thresholds = d.get("thresholds") or {}
    if set(thresholds) - {"cpu", "memory"}:
        raise SimulationError("Unsupported threshold key")
    for value in thresholds.values():
        if not isinstance(value, int) or not 1 <= value <= 100:
            raise SimulationError("Thresholds must be percentages between 1 and 100")
    for connection in d["cross_connects"]:
        if connection["source"] not in d["interfaces"] or connection["destination"] not in d["interfaces"]:
            raise SimulationError("Cross-connect references unknown port")
