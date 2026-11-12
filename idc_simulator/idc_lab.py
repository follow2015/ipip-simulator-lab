"""64 eight-GPU servers, two storage servers and four independent fabrics."""
from idc_simulator.model import stable_hex


def generate_gpu_idc():
    seed = 20261109
    lab = {"schema_version": 1, "name": "idc-64gpu", "seed": seed,
        "management": {"subnet": "10.200.0.0/24", "gateway": "10.200.0.1", "attachment": "routed-bridge"},
        "defaults": {"mac_aging_seconds": 300, "arp_aging_seconds": 300},
        "assumptions": ["Each GPU server uses one 100GE port for inference and one for storage.",
            "Each server additionally has one 25GE in-band interface and one 1GE BMC interface.",
            "Storage ports have separate IP/MAC identities; no cross-switch LACP/MLAG is assumed.",
            "Each fabric uses a chain of three two-member switch LAGs; unused ports are administratively down."],
        "devices": [], "links": [], "hosts": [], "flows": [], "scenarios": {}}
    device_map = {}

    def add_device(dev, role, ports, vlan, profile):
        d = {"id": dev, "hostname": dev, "role": role, "profile": profile,
            "management_ip": f"10.200.0.{len(lab['devices'])+11}", "vlans": [vlan], "interfaces": ports}
        lab["devices"].append(d); device_map[dev] = d
        return d

    def port(dev, ident, name, index, speed, vlan, **extra):
        raw = "02"+stable_hex(seed, dev+":"+ident, 10)
        return {"id": ident, "name": name, "ifindex": index, "mac": ":".join(raw[i:i+2] for i in range(0,12,2)),
            "speed_bps": speed, "pvid": vlan, "allowed_vlans": [vlan], "admin": False, **extra}

    def chassis_entities(dev, cpu=18):
        tag = stable_hex(seed, dev+":entity", 6).upper()
        cards = [
            {"id": "slot1", "index": 101, "name": "Line Card Slot 1", "class": 9, "parent": 1,
             "position": 1, "present": True, "serial": f"SIM-{tag}-LC1"},
            {"id": "psu1", "index": 102, "name": "Power Supply 1", "class": 6, "parent": 1,
             "position": 2, "present": True, "serial": f"SIM-{tag}-PS1"},
            {"id": "psu2", "index": 103, "name": "Power Supply 2", "class": 6, "parent": 1,
             "position": 3, "present": True, "serial": f"SIM-{tag}-PS2"}]
        sensors = [
            {"id": "temperature", "index": 201, "name": "Chassis inlet temperature", "type": 8,
             "value": 32, "threshold": 80, "clear_threshold": 70, "scale": 9, "precision": 0,
             "status": 1, "units": "Celsius", "parent": 1},
            {"id": "fan1", "index": 202, "name": "Fan tray 1 speed", "type": 10, "value": 4800,
             "threshold": 9000, "clear_threshold": 8000, "low_threshold": 1000, "low_clear": 3000,
             "scale": 9, "precision": 0, "status": 1, "units": "rpm", "parent": 1},
            {"id": "fan2", "index": 203, "name": "Fan tray 2 speed", "type": 10, "value": 4870,
             "threshold": 9000, "clear_threshold": 8000, "low_threshold": 1000, "low_clear": 3000,
             "scale": 9, "precision": 0, "status": 1, "units": "rpm", "parent": 1},
            {"id": "power1", "index": 204, "name": "PSU 1 present", "type": 12, "value": 1,
             "scale": 9, "precision": 0, "status": 1, "units": "", "parent": 1},
            {"id": "power2", "index": 205, "name": "PSU 2 present", "type": 12, "value": 1,
             "scale": 9, "precision": 0, "status": 1, "units": "", "parent": 1}]
        return {"cards": cards, "sensors": sensors,
                "resources": {"cpu_percent": cpu, "memory_total": 1048576, "memory_used": 262144 + cpu*4096},
                "thresholds": {"cpu": 80, "memory": 85}}

    def wire(ident, a, b, bridge_vlan, **extra):
        for ep in (a,b):
            dev, pid = ep.split(":")
            p = next(p for p in device_map[dev]["interfaces"] if p["id"] == pid)
            p["admin"] = True
            for member in p.get("members", []):
                next(m for m in device_map[dev]["interfaces"] if m["id"] == member)["admin"] = True
        lab["links"].append({"id": ident, "endpoints": [a,b], "mode": "trunk", "allowed_vlans": [bridge_vlan], **extra})

    fabrics = [("infer",100,"inference",10,64,64,100_000_000_000,100_000_000_000),
        ("storage",200,"storage",20,64,64,100_000_000_000,100_000_000_000),
        ("inband",300,"inband",30,56,48,25_000_000_000,100_000_000_000),
        ("oob",400,"oob",40,52,48,1_000_000_000,10_000_000_000)]
    for prefix,vlan,vrf,subnet,total,downlinks,low_speed,high_speed in fabrics:
        for n in range(1,5):
            dev = f"{prefix}-sw-{n}"
            ports = []
            for i in range(1,total+1):
                speed = low_speed if i <= downlinks else high_speed
                kind = {1_000_000_000:"GigabitEthernet",10_000_000_000:"10GE",25_000_000_000:"25GE",100_000_000_000:"100GE"}[speed]
                ports.append(port(dev,f"p{i}",f"{kind}1/0/{i}",i,speed,vlan,
                    medium="rj45" if prefix == "oob" and i <= downlinks else "optical"))
            d = add_device(dev,prefix+"-switch",ports,vlan,"generic/ethernet/switch")
            d.update(chassis_entities(dev, cpu=12+4*n))
            d["hardware"] = {"simulated": True, "downlink_ports": downlinks, "uplink_ports": total-downlinks,
                "note": "Generic VRP behavior; hardware port inventory overrides the profile model label."}
            d["l3_interfaces"] = [{"id": f"vlan{vlan}", "vlan": vlan, "address": f"10.{subnet}.0.{n}/24", "vrf": vrf}]
            for lag, members in (("lag-left",[f"p{total-3}",f"p{total-2}"]), ("lag-right",[f"p{total-1}",f"p{total}"])):
                ports.append(port(dev,lag,"Eth-Trunk1" if lag == "lag-left" else "Eth-Trunk2",1001 if lag == "lag-left" else 1002,
                    2*high_speed,vlan,type=161,members=members,min_links=1))
        for n in range(1,4):
            wire(f"{prefix}-isl-{n}",f"{prefix}-sw-{n}:lag-right",f"{prefix}-sw-{n+1}:lag-left",vlan,lldp_scope="members")

    def add_address(d, pid, ident, vlan, subnet, suffix, vrf):
        p = next(p for p in d["interfaces"] if p["id"] == pid)
        lab["hosts"].append({"id": ident, "device": d["id"], "interface": pid, "mac": p["mac"],
            "address": f"10.{subnet}.0.{suffix}/24", "vlan": vlan, "gateway": f"10.{subnet}.0.1", "vrf": vrf})

    for n in range(1,65):
        dev = f"gpu-{n:03}"
        ports = [port(dev,"infer","eth0",1,100_000_000_000,100), port(dev,"storage","eth1",2,100_000_000_000,200),
            port(dev,"mgmt","mgmt0",3,25_000_000_000,300),port(dev,"bmc","bmc0",4,1_000_000_000,400,lldp=False)]
        d = add_device(dev,"gpu-server",ports,100,"generic/linux/server")
        d["vlans"] = [100,200,300,400]
        d["resources"] = {"cpu_percent": 15+n%40, "memory_total": 2097152, "memory_used": 524288+n*1024}
        d["thresholds"] = {"cpu": 80, "memory": 85}
        d["hardware"] = {"gpu_model": "NVIDIA RTX 5090", "gpu_count": 8, "data_nics": "2 x 100GE",
            "management_assumption": "Additional 25GE in-band and 1GE BMC ports"}
        for prefix,vlan,vrf,subnet,pid in (("infer",100,"inference",10,"infer"),("storage",200,"storage",20,"storage"),
            ("inband",300,"inband",30,"mgmt"),("oob",400,"oob",40,"bmc")):
            wire(f"{dev}-{pid}",f"{dev}:{pid}",f"{prefix}-sw-{(n-1)//16+1}:p{(n-1)%16+1}",vlan,mode="access",vlan=vlan)
            add_address(d,pid,f"{dev}-{pid}",vlan,subnet,n+10,vrf)

    for n in range(1,3):
        dev = f"store-{n:02}"
        ports = [port(dev,f"data{i}",f"eth{i-1}",i,100_000_000_000,200) for i in range(1,5)]
        ports += [port(dev,"mgmt","mgmt0",5,25_000_000_000,300),port(dev,"bmc","bmc0",6,1_000_000_000,400,lldp=False)]
        d = add_device(dev,"storage-server",ports,200,"generic/linux/server")
        d["vlans"] = [200,300,400]
        d["resources"] = {"cpu_percent": 22, "memory_total": 4194304, "memory_used": 1572864}
        d["thresholds"] = {"cpu": 80, "memory": 85}
        for i in range(1,5):
            wire(f"{dev}-data{i}",f"{dev}:data{i}",f"storage-sw-{i}:p{16+n}",200,mode="access",vlan=200)
            add_address(d,f"data{i}",f"{dev}-data{i}",200,20,100+(n-1)*4+i,"storage")
        for prefix,pid,vlan,subnet,vrf in (("inband","mgmt",300,30,"inband"),("oob","bmc",400,40,"oob")):
            wire(f"{dev}-{pid}",f"{dev}:{pid}",f"{prefix}-sw-{n}:p17",vlan,mode="access",vlan=vlan)
            add_address(d,pid,f"{dev}-{pid}",vlan,subnet,100+n,vrf)

    lab["flows"] = [{"id":"inference-baseline","source":"gpu-001-infer","destination":"gpu-064-infer","rate_bps":10_000_000_000},
        {"id":"storage-baseline","source":"gpu-001-storage","destination":"store-01-data4","rate_bps":10_000_000_000},
        {"id":"inband-check","source":"gpu-001-mgmt","destination":"gpu-064-mgmt","rate_bps":1_000_000},
        {"id":"oob-check","source":"gpu-001-bmc","destination":"gpu-064-bmc","rate_bps":1_000_000}]
    lab["scenarios"]["gpu-link-flap"] = {"schema_version":1,"seed":seed,"steps":[
        {"at":0,"device":"infer-sw-1","operations":[{"kind":"link","id":"gpu-001-infer","up":False}]},
        {"at":2,"device":"infer-sw-1","operations":[{"kind":"link","id":"gpu-001-infer","up":True}]}]}
    return lab
