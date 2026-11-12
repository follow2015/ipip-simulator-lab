"""Read-only probes and ARP/FDB correlation; no operating-system networking."""
from ipaddress import ip_address, ip_interface
from idc_simulator.errors import SimulationError, NotFound
from idc_simulator.model import mac_bytes
from idc_simulator.topology import forwarding_graph, evaluate_flow


def network_endpoints(state):
    result = [dict(h) for h in state["lab"]["hosts"]]
    for dev, d in state["lab"]["devices"].items():
        for l3 in d["l3_interfaces"]:
            if l3.get("vlan") is None:
                continue
            ports = [p for p in d["interfaces"].values() if p["mode"] != "routed" and l3["vlan"] in p["allowed_vlans"]]
            if not ports:
                continue
            ports.sort(key=lambda p: (not p["oper"], p["ifindex"]))
            result.append({"id": f"{dev}/{l3['id']}", "device": dev, "interface": ports[0]["id"],
                "address": l3["address"], "mac": l3["mac"], "vlan": l3["vlan"], "vrf": l3["vrf"],
                "gateway": str(ip_interface(l3["address"]).ip), "active": l3["oper"]})
    return result


def ping(state, source, destination, vrf=None, count=5, source_device=None):
    if not isinstance(count, int) or isinstance(count, bool) or not 1 <= count <= 20:
        raise SimulationError("Ping count must be 1..20")
    target = str(ip_address(destination))
    endpoints = network_endpoints(state)
    matches = [h for h in endpoints if str(ip_interface(h["address"]).ip) == target and (vrf is None or h["vrf"] == vrf)]
    if len(matches) > 1:
        raise SimulationError("Ambiguous destination IP; specify VRF")
    sources = [h for h in endpoints if source in (h["id"], h["device"], str(ip_interface(h["address"]).ip))
               and (vrf is None or h["vrf"] == vrf) and (source_device is None or h["device"] == source_device)]
    if not sources:
        raise NotFound("No modeled source; use a host ID, business IP or SVI device")
    if matches:
        dst = matches[0]
        sources.sort(key=lambda h: (h["vrf"] != dst["vrf"], h["vlan"] != dst["vlan"],
            ip_interface(dst["address"]).ip not in ip_interface(h["address"]).network, h["id"]))
    src = sources[0]
    forward = {"reachable": False, "path": [], "reason": "destination-not-modeled"}
    reverse = dict(forward)
    if matches:
        graph = forwarding_graph(state["lab"])
        forward = evaluate_flow(state["lab"], graph, src, matches[0])
        reverse = evaluate_flow(state["lab"], graph, matches[0], src)
    reachable = forward["reachable"] and reverse["reachable"]
    reason = forward["reason"] if not forward["reachable"] else "reachable" if reverse["reachable"] else "return-"+reverse["reason"]
    return {"mode": "simulated", "source": str(ip_interface(src["address"]).ip), "source_endpoint": src["id"],
        "destination": target, "vrf": src["vrf"], "reachable": reachable, "transmitted": count,
        "received": count if reachable else 0, "loss_percent": 0 if reachable else 100,
        "reason": reason, "forward": forward, "reverse": reverse}


def locate_address(state, now, ip=None, mac=None, vrf=None, vlan=None, device=None, port=None):
    if sum(value is not None for value in (ip, mac, device)) != 1 or (device is None) != (port is None):
        raise SimulationError("Specify ip, mac, or a device+port pair")
    address = str(ip_address(ip)) if ip is not None else None
    mac = mac_bytes(mac).hex(":") if mac is not None else None
    vlan = int(vlan) if vlan is not None else None
    lab, bindings, ports = state["lab"], [], []
    selected_port = None
    if device is not None:
        if device not in lab["devices"]:
            raise NotFound("Unknown device")
        selected_port = next((p["id"] for p in lab["devices"][device]["interfaces"].values() if port in (p["id"],p["name"])), None)
        if selected_port is None:
            raise NotFound("Unknown interface")
    for dev, tables in state["tables"].items():
        for row in tables["arp"]:
            selected = (address is None or row["ip"] == address) and (mac is None or row["mac"].lower() == mac)
            if (vrf is None or row["vrf"] == vrf) and selected:
                l3 = next((l for l in lab["devices"][dev]["l3_interfaces"] if l["ifindex"] == row["ifindex"] and l["vrf"] == row["vrf"]), {})
                if vlan is None or l3.get("vlan") == vlan:
                    bindings.append({"device": dev, **row, "vlan": l3.get("vlan"), "evidence": "arp"})
    macs = None
    if address is not None:
        macs = {b["mac"].lower() for b in bindings}
    elif mac is not None:
        macs = {mac}
    for dev, tables in state["tables"].items():
        d = lab["devices"][dev]
        if d["profile"] == "generic/linux/server":
            continue
        if device is not None and device != dev:
            continue
        for row in tables["fdb"]:
            if (macs is not None and row["mac"].lower() not in macs) or (vlan is not None and row["vlan"] != vlan):
                continue
            if address is not None or vrf is not None:
                if not any(b["mac"].lower() == row["mac"].lower() and b["vlan"] == row["vlan"] for b in bindings):
                    continue
            port = next(p for p in d["interfaces"].values() if p["ifindex"] == row["ifindex"])
            if selected_port is not None and port["id"] != selected_port:
                continue
            ep = f"{dev}:{port['id']}"
            peers = [e for link in lab["links"] if ep in link["endpoints"] for e in link["endpoints"] if e != ep]
            server_facing = any(lab["devices"][e.split(":")[0]]["profile"] == "generic/linux/server" for e in peers)
            direct = any(h["device"] == dev and h["interface"] == port["id"] and h["mac"].lower() == row["mac"].lower() for h in lab["hosts"])
            ips = sorted({b["ip"] for b in bindings if b["mac"].lower() == row["mac"].lower() and b["vlan"] == row["vlan"]})
            ports.append({"device": dev, "interface": port["id"], "name": port["name"], **row, "ips": ips,
                "oper": port["oper"], "members": port.get("members", []), "peer_endpoints": peers,
                "role": "attachment" if server_facing or direct else "transit",
                "fresh": row.get("state") == "learned",
                "evidence": "fdb"})
    if device is not None:
        bindings = [b for b in bindings if any(p["mac"].lower() == b["mac"].lower() and p["vlan"] == b["vlan"] for p in ports)]
    return {"mode": "simulated", "ip": address, "mac": mac, "vrf": vrf, "vlan": vlan,
        "observed_ns": now, "bindings": bindings, "ports": ports, "found": bool(ports),
        "ambiguous": address is not None and len({(b["vrf"], b["mac"].lower()) for b in bindings}) > 1,
        "note": "ARP/FDB may remain stale until aging; attachment roles use declared cabling."}
