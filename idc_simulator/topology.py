"""Deterministic VLAN paths, neighbor aging and time-integrated traffic."""
from collections import deque
from functools import lru_cache
from copy import deepcopy
from ipaddress import ip_interface as _parse_interface, ip_address, ip_network
from idc_simulator.model import endpoint
from idc_simulator.errors import SimulationError
from idc_simulator.synchronization import publish_state, semantic_header, same_tables

_iface = lru_cache(maxsize=65536)(_parse_interface)
_net = lru_cache(maxsize=4096)(lambda text: ip_network(text, strict=False))


def settle_counter(counter, now):
    elapsed = max(0, now - counter["time_ns"])
    for key in {k[:-5] for k in counter if k.endswith("_rate")}:
        denominator = counter.get(key+"_denominator")
        if denominator is None:
            denominator = 8_000_000_000 if key.endswith("_bytes") else counter.get("packet_denominator", 8_000_000_000_000)
        numerator = counter.get(key+"_rate", 0)*elapsed + counter.get(key+"_remainder", 0)
        whole, remainder = divmod(numerator, denominator)
        counter[key] = counter.get(key, 0) + whole
        counter[key+"_remainder"] = remainder
    counter["time_ns"] = now


def sample_counter(counter, now):
    result = dict(counter)
    settle_counter(result, now)
    return result


def vlan_path(graph, source, destination, vlan, devices=None, source_ep=None, destination_ep=None):
    queue = deque([source]); predecessors = {source: None}
    while queue:
        dev = queue.popleft()
        if dev == destination:
            edges = []
            while predecessors[dev] is not None:
                dev, edge = predecessors[dev]
                edges.append(edge)
            return edges[::-1]
        if devices and dev != source and not devices[dev].get("forwarding", devices[dev]["profile"] != "generic/linux/server"):
            continue
        for peer, out_ep, in_ep, allowed in graph[dev]:
            if dev == source and source_ep is not None and out_ep != source_ep:
                continue
            if peer == destination and destination_ep is not None and in_ep != destination_ep:
                continue
            if peer not in predecessors and vlan in allowed:
                predecessors[peer] = (dev, (out_ep, in_ep)); queue.append(peer)
    return None


def host_tree(graph, devices, host):
    root = host["device"]
    attached = f"{root}:{host['interface']}"
    next_hops = {root: attached}
    queue = deque([root])
    while queue:
        dev = queue.popleft()
        if dev != root and not devices[dev]["forwarding"]:
            continue
        for peer, out_ep, in_ep, allowed in graph[dev]:
            if dev == root and not devices[root]["forwarding"] and out_ep != attached:
                continue
            if peer not in next_hops and host["vlan"] in allowed:
                next_hops[peer] = in_ep
                queue.append(peer)
    return next_hops


def forwarding_graph(lab):
    graph = {dev: [] for dev in lab["devices"]}
    for link in lab["links"]:
        a, b = link["endpoints"]
        ad, ap = endpoint(lab, a); bd, bp = endpoint(lab, b)
        if link["blocked"] or link["mode"] == "routed" or not ap["oper"] or not bp["oper"]:
            continue
        allowed = set(link["allowed_vlans"]) & set(ap["allowed_vlans"]) & set(bp["allowed_vlans"])
        if link["mode"] == "access":
            allowed &= {ap["pvid"]} & {bp["pvid"]}
        if ap["mode"] == "routed" or bp["mode"] == "routed":
            allowed.clear()
        graph[ad].append((bd, a, b, allowed))
        graph[bd].append((ad, b, a, allowed))
    return graph


def evaluate_flow(lab, graph, src, dst):
    def path(source, destination, vlan):
        return vlan_path(graph, source, destination, vlan, lab["devices"],
            source_ep=f"{src['device']}:{src['interface']}" if source == src["device"] and not lab["devices"][source].get("forwarding", True) else None,
            destination_ep=f"{dst['device']}:{dst['interface']}" if destination == dst["device"] and not lab["devices"][destination].get("forwarding", True) else None)
    same_subnet = _iface(dst["address"]).ip in _iface(src["address"]).network
    edges = path(src["device"], dst["device"], src["vlan"]) if same_subnet and src["vlan"] == dst["vlan"] and src["vrf"] == dst["vrf"] else None
    if edges is None:
        edges = routed_path(lab, graph, src, dst, path)
    src_ep = f"{src['device']}:{src['interface']}"; dst_ep = f"{dst['device']}:{dst['interface']}"
    if src["device"] == dst["device"] and src_ep != dst_ep and not lab["devices"][src["device"]].get("forwarding", True):
        edges = None
    if not src["active"] or not dst["active"]:
        return {"reachable": False, "path": edges or [], "reason": "endpoint-inactive"}
    for ep, host in ((src_ep, src), (dst_ep, dst)):
        port = endpoint(lab, ep)[1]
        if not port["oper"]:
            return {"reachable": False, "path": edges or [], "reason": "endpoint-down"}
        if host["vlan"] not in port["allowed_vlans"]:
            return {"reachable": False, "path": edges or [], "reason": "endpoint-vlan-filtered"}
    if edges is None:
        return {"reachable": False, "path": [], "reason": "no-forwarding-path"}
    visited = {src["device"], dst["device"], *(ep.split(":")[0] for edge in edges for ep in edge)}
    for dev in visited:
        for rule in lab["devices"][dev]["firewall"]:
            if _iface(src["address"]).ip in _net(rule["source"]) and _iface(dst["address"]).ip in _net(rule["destination"]):
                if rule["action"] == "deny":
                    return {"reachable": False, "path": edges, "reason": "firewall-deny"}
                break
    return {"reachable": True, "path": edges, "reason": "reachable"}


def derive(state, now, time_only=False, previous=None):
    plan = state.get("_derived_plan")
    if time_only and plan and now < plan["rebuild_ns"]:
        advance(state, now)
        return
    lab, runtime = state["lab"], state["runtime"]
    devices = lab["devices"]
    baseline = previous if previous is not None else state
    old_tables = baseline.get("tables", {})
    old_headers = {dev: semantic_header(baseline, dev) for dev in devices if dev in baseline["lab"]["devices"]}
    if previous is None:
        old_headers = deepcopy(old_headers)
    oper = {}
    connected = set()
    host_ports = {f"{h['device']}:{h['interface']}" for h in lab["hosts"]}
    def available(dev, p):
        d = devices[dev]
        card_ok = not p.get("card") or next(c["present"] for c in d["cards"] if c["id"] == p["card"])
        lag_ok = not p.get("members") or any(d["interfaces"][member]["admin"] for member in p["members"])
        return p["admin"] and card_ok and lag_ok and runtime[dev]["online"]
    for link in lab["links"]:
        a, b = link["endpoints"]
        ad, ap = endpoint(lab, a); bd, bp = endpoint(lab, b)
        up = link["up"] and available(ad, ap) and available(bd, bp)
        if ap.get("members"):
            active = 0
            for am, bm in zip(ap["members"], bp["members"], strict=True):
                member_up = link["up"] and available(ad,devices[ad]["interfaces"][am]) and available(bd,devices[bd]["interfaces"][bm])
                oper[f"{ad}:{am}"] = oper[f"{bd}:{bm}"] = bool(member_up)
                active += int(member_up)
            up = up and active >= max(ap.get("min_links", 1),bp.get("min_links", 1))
        oper[a] = oper[b] = bool(up)
        connected.update((a, b))
    for dev, d in devices.items():
        r = runtime[dev]
        r.setdefault("lldp_cache", {}); r.setdefault("fdb_cache", {}); r.setdefault("arp_cache", {}); r.setdefault("last_change", {})
        for port, p in d["interfaces"].items():
            ep = f"{dev}:{port}"
            oper.setdefault(ep, bool(available(dev, p) and ep in host_ports))
            p["oper"] = oper[ep]
            old = r.setdefault("oper", {}).get(port, p["oper"])
            if old != p["oper"]:
                r.setdefault("last_change", {})[port] = max(0, (now-r["boot_ns"]) // 10_000_000)
            r["oper"][port] = p["oper"]
        for l3 in d["l3_interfaces"]:
            if l3.get("interface"):
                port = d["interfaces"][l3["interface"]]
                l3["oper"] = l3.get("admin",True) and port["oper"] and port["mode"] == "routed"
            else:
                l3["oper"] = l3.get("admin",True) and any(p["oper"] and p["mode"] != "routed" and l3.get("vlan") in p["allowed_vlans"] for p in d["interfaces"].values())
    graph = forwarding_graph(lab)
    def path(source, destination, vlan, destination_ep=None):
        return vlan_path(graph, source, destination, vlan, devices, destination_ep=destination_ep)
    ttl = int(lab["defaults"].get("lldp", {}).get("ttl_seconds", 120)) * 1_000_000_000
    interval = int(lab["defaults"].get("lldp", {}).get("tx_interval_seconds", 0)) * 1_000_000_000
    immediate = lab["defaults"].get("lldp", {}).get("failure_policy", "immediate") == "immediate"
    live_lldp = {d: set() for d in devices}
    for link in lab["links"]:
        if not link["lldp"]:
            continue
        a, b = link["endpoints"]
        ad, ap = endpoint(lab,a); bd, bp = endpoint(lab,b)
        pairs = [(a,b)]
        if link.get("lldp_scope") == "members" and ap.get("members"):
            pairs = [(f"{ad}:{am}", f"{bd}:{bm}") for am,bm in zip(ap["members"],bp["members"],strict=True)]
        for local, remote in [pair for a,b in pairs for pair in ((a,b),(b,a))]:
            dev, p = endpoint(lab, local); peer, pp = endpoint(lab, remote)
            key = local
            r = runtime[dev]
            cache = r["lldp_cache"]
            if oper[local] and p["lldp"] and pp["lldp"]:
                since = r.setdefault("lldp_since", {}).setdefault(key, now)
                if now - since >= interval:
                    live_lldp[dev].add(key)
                    cache[key] = {"local_port": p["lldp_port"], "ifindex": p["ifindex"],
                                  "remote_device": peer, "remote_port": pp["name"],
                                  "remote_description": pp["description"], "hostname": devices[peer]["hostname"],
                                  "remote_ifindex": pp["ifindex"],
                                  "remote_port_mac": pp["mac"], "local_port_mac": p["mac"],
                                  "local_name": p["name"], "management_ifindex": devices[peer].get("management_ifindex", 0),
                                  "chassis_mac": devices[peer]["chassis_mac"], "management_ip": devices[peer]["management_ip"],
                                  "time_mark": cache.get(key, {}).get("time_mark", max(0, (now-r["boot_ns"])//10_000_000)),
                                  "expires_ns": now + ttl}
            else:
                r.setdefault("lldp_since", {}).pop(key, None)
    aging = int(lab["defaults"].get("mac_aging_seconds", 300)) * 1_000_000_000
    arp_aging = int(lab["defaults"].get("arp_aging_seconds", 300)) * 1_000_000_000
    live_arp = {dev: set() for dev in devices}
    live_fdb = {dev: set() for dev in devices}
    for link in lab["links"]:
        if link.get("mode") != "routed" or not oper[link["endpoints"][0]]:
            continue
        for local, remote in (link["endpoints"], link["endpoints"][::-1]):
            dev, p = endpoint(lab, local); peer, pp = endpoint(lab, remote)
            local_l3 = [l for l in devices[dev]["l3_interfaces"] if l.get("interface") == p["id"]]
            peer_l3 = [l for l in devices[peer]["l3_interfaces"] if l.get("interface") == pp["id"]]
            for l3 in local_l3:
                for neighbor in peer_l3:
                    address = _iface(neighbor["address"]).ip
                    if l3["oper"] and neighbor["oper"] and l3["vrf"] == neighbor["vrf"] and address in _iface(l3["address"]).network:
                        runtime[dev]["arp_cache"][f"{l3['vrf']}:{address}"] = {"vrf": l3["vrf"], "ifindex": l3["ifindex"],
                            "ip": str(address), "mac": pp["mac"], "state": "reachable", "expires_ns": now+arp_aging, "origin": "routed-neighbor"}
                        live_arp[dev].add(f"{l3['vrf']}:{address}")
    svis = [(dev,l) for dev,d in devices.items() for l in d["l3_interfaces"] if l.get("vlan") is not None and l["oper"]]
    svi_domains = {}
    for dev, local in svis:
        for peer, remote in svis:
            address = _iface(remote["address"]).ip
            if dev == peer or local["vlan"] != remote["vlan"] or local["vrf"] != remote["vrf"]:
                continue
            if address not in _iface(local["address"]).network:
                continue
            domain_key = (peer,local["vlan"])
            if domain_key not in svi_domains:
                svi_domains[domain_key] = host_tree(graph,devices,{"device":peer,"interface":"","vlan":local["vlan"]})
            if dev not in svi_domains[domain_key]:
                continue
            runtime[dev]["arp_cache"][f"{local['vrf']}:{address}"] = {"vrf":local["vrf"], "ifindex":local["ifindex"],
                "ip":str(address), "mac":remote["mac"], "state":"reachable", "expires_ns":now+arp_aging, "origin":"svi-neighbor"}
            live_arp[dev].add(f"{local['vrf']}:{address}")
    for host in lab["hosts"]:
        hdev, hp = endpoint(lab, f"{host['device']}:{host['interface']}")
        host_live = host["active"] and oper[f"{hdev}:{host['interface']}"] and host["vlan"] in hp["allowed_vlans"]
        if not host_live:
            continue
        next_hops = host_tree(graph, devices, host)
        for dev, d in devices.items():
            if not d.get("forwarding", d["profile"] != "generic/linux/server"):
                continue
            if str(host["vlan"]) not in d["vlans"]:
                continue
            out_ep = next_hops.get(dev)
            if out_ep is None:
                continue
            _, port = endpoint(lab, out_ep)
            fdb_id = d["vlans"][str(host["vlan"])]["fdb_id"]
            key = f"{fdb_id}:{host['mac']}"
            runtime[dev]["fdb_cache"][key] = {"fdb_id": fdb_id, "vlan": host["vlan"], "mac": host["mac"],
                "bridge_port": port["bridge_port"], "ifindex": port["ifindex"], "origin": "learned", "expires_ns": now+aging}
            live_fdb[dev].add(key)
            for l3 in d["l3_interfaces"]:
                if not l3["oper"] or l3.get("vlan") != host["vlan"] or l3["vrf"] != host["vrf"] or _iface(host["address"]).ip not in _iface(l3["address"]).network:
                    continue
                ip = str(_iface(host["address"]).ip)
                runtime[dev]["arp_cache"][f"{l3['vrf']}:{ip}"] = {"vrf": l3["vrf"], "ifindex": l3["ifindex"], "ip": ip,
                    "mac": host["mac"], "state": "reachable", "expires_ns": now+arp_aging}
                live_arp[dev].add(f"{l3['vrf']}:{ip}")
    rates = {ep: {"in": 0, "out": 0, "in_packets": 0, "out_packets": 0} for ep in oper}
    hosts = {h["id"]: h for h in lab["hosts"]}
    flows = []
    for flow in lab["flows"]:
        src, dst = hosts[flow["source"]], hosts[flow["destination"]]
        result = evaluate_flow(lab, graph, src, dst)
        edges, reachable = result["path"], result["reachable"]
        src_ep = f"{src['device']}:{src['interface']}"; dst_ep = f"{dst['device']}:{dst['interface']}"
        entries = [(src_ep, "in")] if devices[src["device"]].get("forwarding",True) else []
        if devices[dst["device"]].get("forwarding",True):
            entries.append((dst_ep,"out"))
        for out_ep, in_ep in edges or []:
            entries.extend(((out_ep, "out"), (in_ep, "in")))
        if reachable:
            for ep, direction in entries:
                rates[ep][direction] += flow["rate_bps"]
                rates[ep][direction+"_packets"] += flow["rate_bps"] * 1_000_000_000 // (8*flow["packet_size"])
        flows.append({"id": flow["id"], "reachable": reachable, "path": edges or [], "rate_bps": flow["rate_bps"] if reachable else 0})
    for dev, d in devices.items():
        for p in d["interfaces"].values():
            members = [d["interfaces"][m] for m in p.get("members", []) if d["interfaces"][m]["oper"]]
            if not members:
                continue
            for key, value in rates[f"{dev}:{p['id']}"].items():
                share, remainder = divmod(value, len(members))
                for n, member in enumerate(members):
                    rates[f"{dev}:{member['id']}"][key] += share+int(n < remainder)
    tables = {}
    for dev, d in devices.items():
        r = runtime[dev]
        r["lldp_cache"] = {k: v for k, v in r["lldp_cache"].items() if v["expires_ns"] > now and (not immediate or k in live_lldp[dev])}
        r["fdb_cache"] = {k: dict(v, state="learned" if k in live_fdb[dev] else "stale") for k, v in r["fdb_cache"].items() if v["expires_ns"] > now}
        r["arp_cache"] = {k: dict(v, state="reachable" if k in live_arp[dev] else "stale") for k, v in r["arp_cache"].items() if v["expires_ns"] > now}
        for port, p in d["interfaces"].items():
            ep = f"{dev}:{port}"
            counter = r.setdefault("counters", {}).setdefault(port, {"time_ns": now, "discontinuity": 0})
            settle_counter(counter, now)
            for direction in ("in", "out"):
                if rates[ep][direction] > p["speed_bps"] and not lab["defaults"].get("allow_oversubscription", False):
                    raise SimulationError(f"Logical flows exceed port speed: {ep}")
                counter[direction+"_bytes_rate"] = rates[ep][direction]
                counter[direction+"_packets_rate"] = rates[ep][direction+"_packets"]
            counter["packet_denominator"] = 1_000_000_000_000_000_000
        tables[dev] = {"lldp": list(r["lldp_cache"].values()), "fdb": list(r["fdb_cache"].values()), "arp": list(r["arp_cache"].values()), "flows": flows}
    state["tables"] = tables
    state["derived_ns"] = now
    pending = [since+interval for dev,r in runtime.items() for key,since in r.get("lldp_since",{}).items()
               if key not in live_lldp[dev] and since+interval > now]
    state["_derived_plan"] = {"lldp":live_lldp,"fdb":live_fdb,"arp":live_arp,
        "rebuild_ns":min(pending,default=float("inf"))}
    dirty = {dev for dev in devices if dev not in old_tables or
             old_headers.get(dev) != semantic_header(state, dev) or not same_tables(old_tables[dev], tables[dev])}
    if previous is None and not time_only:
        dirty.update(devices)
    publish_state(state, dirty)


def advance(state, now):
    plan = state["_derived_plan"]
    defaults = state["lab"]["defaults"]
    intervals = {"lldp":int(defaults.get("lldp",{}).get("ttl_seconds",120)),
        "fdb":int(defaults.get("mac_aging_seconds",300)),"arp":int(defaults.get("arp_aging_seconds",300))}
    dirty = set()
    for dev, runtime in state["runtime"].items():
        changed = False
        for kind,seconds in intervals.items():
            cache = runtime[kind+"_cache"]
            live = plan[kind][dev]
            for key in tuple(cache):
                row = cache[key]
                if key in live:
                    row["expires_ns"] = now+seconds*1_000_000_000
                elif row["expires_ns"] <= now:
                    del cache[key]
                    changed = True
            state["tables"][dev][kind] = list(cache.values())
        for counter in runtime.get("counters",{}).values():
            settle_counter(counter,now)
        if changed:
            dirty.add(dev)
    publish_state(state, dirty)
    state["derived_ns"] = now


def routed_path(lab, graph, src, dst, path):
    if src["vrf"] != dst["vrf"]:
        return None
    devices = lab["devices"]
    destination = _iface(dst["address"]).ip
    vrf = src["vrf"]
    def l3s(dev):
        return [l for l in devices[dev]["l3_interfaces"] if l["vrf"] == vrf and l.get("oper",True) and
                (not l.get("interface") or devices[dev]["interfaces"][l["interface"]]["oper"])]
    def next_hop_path(dev, local, next_hop):
        if local.get("interface"):
            ep = f"{dev}:{local['interface']}"
            for link in lab["links"]:
                if link.get("mode") != "routed" or ep not in link["endpoints"] or not link["up"]:
                    continue
                remote = next(e for e in link["endpoints"] if e != ep)
                peer, pp = endpoint(lab, remote)
                if pp["oper"] and any(l.get("interface") == pp["id"] and _iface(l["address"]).ip == next_hop for l in l3s(peer)):
                    return peer, [(ep, remote)]
        elif local.get("vlan") is not None:
            for peer in sorted(devices):
                if any(_iface(l["address"]).ip == next_hop and l.get("vlan") == local["vlan"] for l in l3s(peer)):
                    edges = path(dev, peer, local["vlan"])
                    if edges is not None:
                        return peer, edges
        return None, None
    def route(dev, visited):
        if dev in visited:
            return None
        visited = visited | {dev}
        choices = [(_iface(l["address"]).network.prefixlen, 0, "connected", l) for l in l3s(dev) if destination in _iface(l["address"]).network]
        choices.extend((_net(r["prefix"]).prefixlen, r.get("distance", 1), "static", r) for r in devices[dev]["routes"] if r.get("vrf", "default") == vrf and destination in _net(r["prefix"]))
        choices.sort(key=lambda c: (-c[0], c[1]))
        if not choices:
            return None
        for _, _, kind, entry in choices:
            if kind == "connected":
                if entry.get("vlan") == dst["vlan"]:
                    connected = path(dev, dst["device"], entry["vlan"])
                    if connected is not None:
                        return connected
                continue
            next_hop = ip_address(entry["next_hop"])
            for local in l3s(dev):
                if next_hop not in _iface(local["address"]).network:
                    continue
                peer, transport = next_hop_path(dev, local, next_hop)
                tail = route(peer, visited) if peer is not None else None
                if transport is not None and tail is not None:
                    return transport+tail
        return None
    for dev in sorted(devices):
        if any(l.get("vlan") == src["vlan"] and str(_iface(l["address"]).ip) == src.get("gateway") for l in l3s(dev)):
            head, tail = path(src["device"], dev, src["vlan"]), route(dev, set())
            if head is not None and tail is not None:
                return head+tail
    return None
