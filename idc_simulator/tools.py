"""Reproducible scale fixtures and wire load reporting."""
from idc_simulator.errors import SimulationError


def generate_lab(count=100, ports=48, local=False, layout="chain"):
    if not 1 <= count <= 240 or not 2 <= ports <= 512:
        raise SimulationError("Generator supports 1..240 devices and 2..512 ports")
    if layout == "chain" and count > 1 and ports < 3:
        raise SimulationError("Multi-device chain requires at least 3 ports")
    profiles = ["cisco/ios/c9300", "huawei/vrp/s5735", "h3c/comware/s5560x"]
    prefix = "127.20.0" if local else "10.200.0"
    lab = {"schema_version": 1, "name": "idc-scale", "seed": 20261109,
        "management": {"subnet": prefix+".0/24", "gateway": prefix+".1", "attachment": "routed-bridge"},
        "devices": [], "links": [], "hosts": [], "flows": []}
    for n in range(count):
        dev = f"sw-{n+1:03}"
        lab["devices"].append({"id": dev, "profile": profiles[n%3], "management_ip": prefix+f".{n+11}", "hostname": dev,
            "interfaces": [{"id": f"p{i}", "name": f"GigabitEthernet1/0/{i}", "ifindex": i, "speed_bps": 1_000_000_000} for i in range(1, ports+1)], "vlans": [100]})
        if n:
            lab["links"].append({"id": f"link-{n}", "endpoints": [f"sw-{n:03}:p{ports}", f"{dev}:p{ports-1}"], "allowed_vlans": [100], "lldp": True})
        lab["hosts"].append({"id": f"host-{n+1}", "device": dev, "interface": "p1", "mac": f"02:00:00:01:{n//256:02x}:{n%256:02x}", "address": f"192.0.2.{n+10}/24", "vlan": 100, "active": True})
    lab["devices"][0]["l3_interfaces"] = [{"id": "vlan100", "vlan": 100, "address": "192.0.2.1/24"}]
    if count > 1:
        lab["flows"].append({"id": "baseline", "source": "host-1", "destination": f"host-{count}", "rate_bps": 10_000_000, "packet_size": 1000})
    if layout == "chain":
        return lab
    if layout == "ring":
        if count < 3 or ports < 3:
            raise SimulationError("Ring requires at least 3 devices and 3 ports")
        lab["links"].append({"id": "ring-close", "endpoints": [f"sw-{count:03}:p{ports}", f"sw-001:p{ports-1}"], "allowed_vlans": [100], "blocked": True, "lldp": True})
        return lab
    if layout == "tree":
        if ports < 6:
            raise SimulationError("Tree requires at least 6 ports")
        lab["links"] = [{"id": f"tree-{n}", "endpoints": [f"sw-{(n-1)//4+1:03}:p{2+(n-1)%4}", f"sw-{n+1:03}:p{ports}"], "mode": "trunk", "allowed_vlans": [100]} for n in range(1,count)]
        return lab
    if layout == "mixed":
        return mixed_lab(lab, ports)
    raise SimulationError(f"Unknown layout: {layout}")


def mixed_lab(lab, ports):
    from collections import deque
    if len(lab["devices"]) < 20 or ports < 48:
        raise SimulationError("Mixed layout requires at least 20 devices and 48 ports")
    devices = lab["devices"]
    routers = devices[:12]
    graph = {d["id"]: [] for d in routers}
    lab["links"] = []; lab["hosts"] = []
    for n, d in enumerate(devices):
        d["role"] = "core" if n < 4 else "aggregation" if n < 12 else "access"
        d["l3_interfaces"] = []
        d["routes"] = []
    def routed_link(a, ap, b, bp):
        offset = 4*len(lab["links"])
        aa, ba = f"10.255.0.{offset+1}", f"10.255.0.{offset+2}"
        lab["links"].append({"id": f"l3-{len(lab['links'])}", "endpoints": [f"{a['id']}:p{ap}", f"{b['id']}:p{bp}"], "mode": "routed"})
        a["l3_interfaces"].append({"id": f"p{ap}", "interface": f"p{ap}", "address": aa+"/30"})
        b["l3_interfaces"].append({"id": f"p{bp}", "interface": f"p{bp}", "address": ba+"/30"})
        graph[a["id"]].append((b["id"], ba)); graph[b["id"]].append((a["id"], aa))
    for n in range(4):
        routed_link(devices[n], ports-3, devices[(n+1)%4], ports-2)
    for n in range(8):
        agg = devices[4+n]
        agg["vlans"] = [100+n]
        agg["l3_interfaces"].append({"id": f"vlan{100+n}", "vlan": 100+n, "address": f"10.10.{n}.1/24"})
        routed_link(agg, ports-1, devices[n%4], 1+n//4)
        routed_link(agg, ports, devices[(n+1)%4], 17+n//4)
    access_counts = [0]*8
    for n, d in enumerate(devices[12:]):
        agg_number = n%8; agg = devices[4+agg_number]
        access_counts[agg_number] += 1
        vlan = 100+agg_number
        d["vlans"] = [vlan]
        lab["links"].append({"id": f"access-{n}", "endpoints": [f"{agg['id']}:p{access_counts[agg_number]}", f"{d['id']}:p{ports}"], "mode": "trunk", "allowed_vlans": [vlan]})
        lab["hosts"].append({"id": f"host-{n+1}", "device": d["id"], "interface": "p1", "mac": f"02:00:01:00:{n//256:02x}:{n%256:02x}", "address": f"10.10.{agg_number}.{10+access_counts[agg_number]}/24", "gateway": f"10.10.{agg_number}.1", "vlan": vlan})
    def hops(source, destination, forbidden):
        queue = deque([(source, 0)]); seen = {forbidden, source}
        while queue:
            dev, distance = queue.popleft()
            if dev == destination:
                return distance
            for peer, _ in graph[dev]:
                if peer not in seen:
                    seen.add(peer); queue.append((peer,distance+1))
        return None
    for d in routers:
        for n in range(8):
            target = devices[4+n]["id"]
            if d["id"] == target:
                continue
            for peer, next_hop in graph[d["id"]]:
                distance = hops(peer, target, d["id"])
                if distance is not None:
                    d["routes"].append({"prefix": f"10.10.{n}.0/24", "next_hop": next_hop, "vrf": "default", "distance": distance+1})
    lab["flows"] = [{"id": "cross-subnet", "source": "host-1", "destination": f"host-{len(lab['hosts'])}", "rate_bps": 10000000}]
    lab["name"] = "idc-mixed"
    return lab
