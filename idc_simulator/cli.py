"""Brand session state machines. Commands become semantic operations, never shell."""
from idc_simulator.cli_plugins import plugin_for
from copy import deepcopy
from ipaddress import IPv4Address, IPv4Network
import shlex
from idc_simulator.errors import SimulationError, Unavailable
from idc_simulator.profiles import load_profile


def parse_vlans(text):
    result = set()
    for chunk in text.replace(" to ", "-").split(","):
        chunk = chunk.strip()
        if not chunk:
            continue
        low, separator, high = chunk.partition("-")
        if separator:
            if not (low.isdigit() and high.isdigit()):
                raise SimulationError(f"Invalid VLAN range: {chunk}")
            start, end = int(low), int(high)
            if start > end:
                raise SimulationError(f"Invalid VLAN range: {chunk}")
            result.update(range(start, end+1))
        else:
            if not chunk.isdigit():
                raise SimulationError(f"Invalid VLAN: {chunk}")
            result.add(int(chunk))
    if not result:
        raise SimulationError("VLAN list is empty")
    if min(result) < 1 or max(result) > 4094:
        raise SimulationError("VLAN must be 1-4094")
    return sorted(result)


class CliSession:
    def __init__(self, client):
        self.client = client
        self.family = load_profile(client.current["device"]["profile"])["cli"]["family"]
        self.mode = "exec"
        self.port = None
        self.l3_vlan = None
        self.vlan = None
        self.tree = None
        self.edit = None
        self.edit_fields = {}
        self.candidate = []
        self.revision = client.current["revision"]
        self.page_length = 24
        self.width = 80
        self.closed = False

    @property
    def prompt(self):
        host = self.client.current["device"]["hostname"]
        if self.family in {"vrp", "comware"}:
            return f"<{host}>" if self.mode == "exec" else f"[{host}{'-'+self.port if self.port else ''}]"
        if self.family == "junos":
            return f"sim@{host}{'>' if self.mode == 'exec' else '#'}"
        if self.family == "fortios":
            return f"{host}{' ('+str(self.edit or self.tree)+')' if self.tree else ''} #"
        return f"{host}{'(config-if)' if self.port or self.l3_vlan else '(config)' if self.mode != 'exec' else ''}#"

    def find_port(self, name):
        for p in self.client.current["device"]["interfaces"].values():
            if p["id"] == name or p["name"].lower() == name.lower():
                return p["id"]
        import re
        match = re.search(r"(\d+)$", name)
        if match:
            for p in self.client.current["device"]["interfaces"].values():
                if p["name"].rsplit("/", 1)[-1] == match.group(1):
                    return p["id"]
        raise SimulationError(f"Unknown interface: {name}")

    async def change(self, operations):
        if self.mode == "exec":
            raise SimulationError("Configuration mode required")
        if self.family == "junos":
            self.candidate.extend(operations)
        else:
            result = await self.client.transact(operations, self.revision)
            self.revision = result["revision"]

    async def execute(self, command):
        command = command.strip()
        if not command:
            return ""
        try:
            try:
                await self.client.refresh()
            except Unavailable:
                if not self.is_query(command):
                    raise
            return await self._execute(command)
        except (SimulationError, ValueError, IndexError, KeyError) as exc:
            prefix = "Error:" if self.family in {"vrp", "comware", "fortios", "junos"} else "% Error:"
            return f"{prefix} {exc}"

    def is_query(self, command):
        return command.startswith(("show ", "display ", "get ", "uname", "ip link", "ip neigh", "ping ")) or command == "show" and self.family == "fortios"

    async def _execute(self, command):
        lower = command.lower()
        if lower.startswith("ping "):
            args = shlex.split(command)[1:]
            source, vrf, count, destination = None, None, 5, None
            i = 0
            while i < len(args):
                token = args[i]
                if token in {"-c", "-a", "source", "vrf", "-vpn-instance"}:
                    if i+1 >= len(args):
                        raise SimulationError("Missing ping option value")
                    value = args[i+1]
                    if token == "-c":
                        count = int(value)
                    elif token in {"-a", "source"}:
                        source = value
                    else:
                        vrf = value
                    i += 2
                else:
                    if token.startswith("-") or destination is not None:
                        raise SimulationError("Unsupported ping syntax")
                    destination = token; i += 1
            if destination is None:
                raise SimulationError("Ping destination required")
            result = await self.client.call("ping", source=source, destination=destination, vrf=vrf, count=count)
            return (f"Simulated ping {result['source']} -> {result['destination']} (VRF {result['vrf']})\n"
                f"{result['transmitted']} packets transmitted, {result['received']} received, {result['loss_percent']}% packet loss\n"
                f"Result: {result['reason']}")
        if lower in {"terminal length 0", "screen-length disable", "screen-length 0 temporary", "set cli screen-length 0", "no page"}:
            self.page_length = 0; return ""
        if lower.startswith(("terminal width ", "set cli screen-width ")):
            self.width = max(20, min(512, int(command.split()[-1]))); return ""
        if lower in {"enable", "disable"} and self.family in {"ios", "nxos", "eos"}:
            return ""
        enter = {"ios": "configure terminal", "nxos": "configure terminal", "eos": "configure terminal", "vrp": "system-view", "comware": "system-view", "junos": "configure", "fortios": "config system interface"}
        if lower == enter.get(self.family):
            self.mode = "config"; self.revision = self.client.current["revision"]
            if self.family == "fortios":
                self.tree = "system interface"
            return "Enter configuration mode" if self.family in {"vrp", "comware"} else ""
        if lower in {"end", "return"}:
            if self.family == "fortios" and self.edit:
                await plugin_for(self.family).finish(self)
            self.mode = "exec"; self.port = None; self.l3_vlan = None; self.vlan = None; self.tree = None; self.edit = None
            return ""
        if lower in {"exit", "quit"}:
            if self.port or self.l3_vlan or self.vlan:
                self.port = None; self.l3_vlan = None; self.vlan = None
            elif self.mode != "exec":
                self.mode = "exec"
                if self.family == "junos":
                    self.candidate.clear()
            else:
                self.closed = True
            return ""
        if lower in {"write memory", "write mem", "copy running-config startup-config", "save", "save force", "save force vrpcfg.zip"}:
            allowed = self.family in ({"ios", "eos", "nxos"} if lower.startswith(("write", "copy")) else {"vrp", "comware"})
            if not allowed:
                raise SimulationError("Unsupported save command for this brand")
            result = await self.client.transact([{"kind": "save"}], self.client.current["revision"])
            self.revision = result["revision"]
            return "Configuration saved"
        if lower in {"reload", "reboot", "request system reboot", "execute reboot"}:
            await self.client.transact([{"kind": "reboot"}], self.client.current["revision"])
            return "Device rebooted"
        result = await plugin_for(self.family).execute(self, command)
        if result is not None:
            return result
        if self.is_query(command) or (self.family == "fortios" and lower == "show"):
            return await self.query(command)
        if self.mode == "exec":
            raise SimulationError("Unknown command or configuration mode required")
        args = shlex.split(command)
        if args[0] in {"hostname", "sysname"}:
            if args[0] != ("sysname" if self.family in {"vrp", "comware"} else "hostname"):
                raise SimulationError("Unknown command")
            await self.change([{"kind": "system", "fields": {"hostname": args[1]}}]); return ""
        if args[0] == "interface":
            import re
            name = "".join(args[1:])
            match = re.fullmatch(r"(?:Vlan|Vlanif|Vlan-interface)(\d+)",name,re.IGNORECASE)
            if match:
                vlan = int(match[1])
                if str(vlan) not in self.client.current["device"]["vlans"]:
                    raise SimulationError("Undefined VLAN interface")
                self.l3_vlan = vlan; self.port = None
            else:
                self.port = self.find_port(name); self.l3_vlan = None
            self.vlan = None; return ""
        if args[0] == "vlan":
            self.vlan = int(args[1]); self.port = None; self.l3_vlan = None
            await self.change([{"kind": "vlan", "vlan": self.vlan}]); return ""
        if self.vlan and args[0] == "name":
            await self.change([{"kind": "vlan", "vlan": self.vlan, "name": " ".join(args[1:])}]); return ""
        if lower.startswith(("no vlan ", "undo vlan ")):
            await self.change([{"kind": "vlan", "vlan": int(args[2]), "delete": True}]); return ""
        if self.port:
            fields = None
            if lower.startswith(("least active-linknumber ", "port-channel min-links ")):
                await self.change([{"kind":"lag","port":self.port,"fields":{"min_links":int(args[-1])}}])
                return ""
            if lower in {"shutdown", "no shutdown", "undo shutdown"}:
                if lower == "no shutdown" and self.family in {"vrp", "comware"} or lower == "undo shutdown" and self.family not in {"vrp", "comware"}:
                    raise SimulationError("Unknown command")
                fields = {"admin": lower != "shutdown"}
            elif args[0] == "description":
                fields = {"description": " ".join(args[1:])}
            elif lower in {"no description", "undo description"}:
                fields = {"description": ""}
            elif args[0] == "mtu":
                fields = {"mtu": int(args[1])}
            elif lower in {"no switchport", "undo portswitch"}:
                fields = {"mode": "routed"}
            elif lower in {"switchport", "portswitch"}:
                fields = {"mode": "switchport"}
            elif lower.startswith(("switchport access vlan ", "port default vlan ", "port access vlan ")):
                access = parse_vlans(" ".join(args[args.index("vlan")+1:]))
                if len(access) != 1:
                    raise SimulationError("Access VLAN takes exactly one VLAN")
                fields = {"pvid": access[0], "allowed_vlans": access}
            elif lower.startswith(("switchport trunk allowed vlan ", "port trunk allow-pass vlan ", "port trunk permit vlan ")):
                tail = args[args.index("vlan")+1:]
                mode = tail[0] if tail and tail[0] in {"add", "remove"} else None
                values = parse_vlans(" ".join(tail[1:] if mode else tail))
                current = list(self.client.current["device"]["interfaces"][self.port]["allowed_vlans"])
                if mode == "add":
                    values = sorted(set(current) | set(values))
                elif mode == "remove":
                    values = sorted(set(current) - set(values))
                    if not values:
                        raise SimulationError("Trunk must keep at least one allowed VLAN")
                fields = {"allowed_vlans": values}
            elif lower.startswith("ip address "):
                address = args[2] if "/" in args[2] else f"{args[2]}/{IPv4Network('0.0.0.0/'+args[3]).prefixlen}"
                p = self.client.current["device"]["interfaces"][self.port]
                await self.change([{"kind": "l3", "value": {"id": self.port, "interface": self.port, "address": address, "vrf": "default", "ifindex": p["ifindex"]}}]); return ""
            if fields is not None:
                await self.change([{"kind": "interface", "port": self.port, "fields": fields}]); return ""
        if self.l3_vlan:
            value = deepcopy(next((l for l in self.client.current["device"]["l3_interfaces"] if l.get("vlan") == self.l3_vlan), {"id": f"vlan{self.l3_vlan}", "vlan": self.l3_vlan}))
            if lower.startswith("ip address "):
                value["address"] = args[2] if "/" in args[2] else f"{args[2]}/{IPv4Network('0.0.0.0/'+args[3]).prefixlen}"
            elif lower in {"shutdown", "no shutdown", "undo shutdown"}:
                if lower == "no shutdown" and self.family in {"vrp", "comware"} or lower == "undo shutdown" and self.family not in {"vrp", "comware"}:
                    raise SimulationError("Unknown command")
                if "address" not in value:
                    raise SimulationError("Configure an IP address before changing SVI state")
                value["admin"] = lower != "shutdown"
            else:
                raise SimulationError("Unsupported VLAN interface command")
            await self.change([{"kind": "l3", "value": value}]); return ""
        if lower.startswith("ip route "):
            prefix = args[2] if "/" in args[2] else f"{args[2]}/{IPv4Network('0.0.0.0/'+args[3]).prefixlen}"
            await self.change([{"kind": "route", "value": {"prefix": prefix, "next_hop": args[-1], "vrf": "default"}}]); return ""
        if lower.startswith("ip route-static "):
            prefix = f"{args[2]}/{args[3]}"
            await self.change([{"kind": "route", "value": {"prefix": prefix, "next_hop": args[4], "vrf": "default"}}]); return ""
        raise SimulationError("Unsupported command")

    async def query(self, command):
        lower = command.lower()
        aliases = {"show version": "version", "display version": "version", "get system status": "version", "uname -a": "version",
            "show interfaces": "interfaces", "show interface": "interfaces", "display interface": "interfaces", "ip link": "interfaces",
            "show lldp neighbors": "lldp", "show lldp neighbors detail": "lldp", "display lldp neighbor": "lldp",
            "display lldp neighbor-information": "lldp", "display lldp neighbor-information verbose": "lldp",
            "show cdp neighbors": "cdp", "show cdp neighbors detail": "cdp",
            "show ip arp": "arp", "show arp": "arp", "display arp": "arp", "ip neigh": "arp",
            "show mac address-table": "fdb", "display mac-address": "fdb",
            "display eth-trunk": "lags", "show etherchannel summary": "lags", "show port-channel summary": "lags",
            "display link-aggregation verbose": "lags",
            "show running-config": "running", "show run": "running", "show configuration": "running",
            "display current-configuration": "running", "show": "running",
            "show startup-config": "startup", "show start": "startup", "display saved-configuration": "startup",
            "show vlan": "vlans", "show vlan brief": "vlans", "display vlan": "vlans",
            "show ip route": "routes", "display ip routing-table": "routes",
            "display ip routing-table verbose": "routes", "show route": "routes",
            "show inventory": "inventory", "display device": "inventory",
            "show sensors": "sensors", "display environment": "sensors", "show env all": "sensors",
            "display temperature all": "sensors", "display fan": "sensors", "display power": "sensors",
            "get system performance status": "cpu", "show processes cpu": "cpu",
            "show processes cpu history": "cpu", "display cpu-usage": "cpu",
            "display memory": "memory", "show memory": "memory",
            "show ip interface brief": "if_brief", "display interface brief": "if_brief",
            "show interfaces counters": "counters", "show interface counters": "counters",
            "display interface counters": "counters", "display counters": "counters",
            "show interfaces switchport": "switchport", "display interface switchport": "switchport",
            "show hostname": "sysname", "display sysname": "sysname",
            "show logging": "logging", "display logbuffer": "logging",
            "display esn": "esn", "display device manuinfo": "manuinfo",
            "show cross-connects": "cross_connects",
            "show clock": "clock", "display clock": "clock"}
        kind = aliases.get(lower)
        selected = None
        if kind is None:
            kind, selected = parametric_query(lower, command)
        if not kind:
            raise SimulationError("Unsupported query")
        if command.startswith("display ") and self.family not in {"vrp", "comware"} or command.startswith("get ") and self.family != "fortios":
            raise SimulationError("Unknown query for this brand")
        if kind in {"interfaces", "counters"} and selected:
            selected = self.find_port(selected)
        s = deepcopy(self.client.current); d, t = s["device"], s["tables"]
        counters = (s.get("runtime") or {}).get("counters", {})
        family = self.family
        if kind == "version":
            return render_version(d, family)
        if kind == "interfaces":
            if selected:
                return render_interface_detail(d["interfaces"][selected], counters.get(selected, {}), family)
            return "\n".join(f"{p['name']} is {'UP' if p['oper'] else 'DOWN'}, admin {'UP' if p['admin'] else 'DOWN'}\n  ifIndex {p['ifindex']}, MAC {p['mac']}, MTU {p['mtu']}, BW {p['speed_bps']//1000} Kbit\n  Description: {p['description']}" for p in d["interfaces"].values() if selected is None or p["id"] == selected)
        if kind == "counters":
            return render_counters(d, counters, family, selected)
        if kind == "lldp":
            result = "Local Interface  Neighbor  Port ID  Management Address\n"+"\n".join(f"{n['local_name']} {n['hostname']} {n['remote_port']} {n['management_ip']}" for n in t["lldp"])
            if lower.endswith("detail") or lower.startswith("display lldp neighbor"):
                result += "\n"+"\n".join(f"{n['local_name']}: Chassis ID {n['chassis_mac']}, Port MAC {n['remote_port_mac']}, remote ifIndex {n['remote_ifindex']}" for n in t["lldp"])
            return result
        if kind == "cdp":
            return render_cdp(t)
        if kind == "arp":
            return "IP Address  MAC Address  Interface  VRF  State\n"+"\n".join(f"{n['ip']} {n['mac']} {n['ifindex']} {n['vrf']} {n['state']}" for n in t["arp"])
        if kind == "fdb":
            return "VLAN  MAC Address  Port\n"+"\n".join(f"{n['vlan']} {n['mac']} {n['bridge_port']}" for n in t["fdb"])
        if kind == "lags":
            return render_lags(d)
        if kind == "startup":
            d = await self.client.call("startup")
        if kind in {"startup", "running"}:
            if family == "junos" and self.candidate and self.mode != "exec":
                d = await self.client.call("preview", operations=self.candidate, revision=self.revision)
            return render_config(d, family)
        if kind == "running_port":
            return render_running_port(d, family, selected, self.find_port)
        if kind == "vlans":
            return render_vlans(d, family, selected)
        if kind == "routes":
            return render_routes(d, family)
        if kind == "inventory":
            return render_inventory(d, family)
        if kind == "sensors":
            return render_sensors(d, family)
        if kind in {"cpu", "memory"}:
            return render_resources(d, family, kind)
        if kind == "if_brief":
            return render_if_brief(d, family, counters)
        if kind == "switchport":
            return render_switchport(d, family)
        if kind == "sysname":
            return d["hostname"]
        if kind == "logging":
            return "%SYS-5-CONFIG_I: Configured from console by sim on vty0\n%LINK-3-UPDOWN: Interface GigabitEthernet1/0/1, changed state to up"
        if kind == "esn":
            return f"ESN of slot 0: {d['serial']}"
        if kind == "manuinfo":
            return render_manuinfo(d)
        if kind == "clock":
            return "14:23:45 GMT+8 Mon 11/09/2026"
        if kind == "policy":
            return "Traffic policy: not configured"
        if kind == "cross_connects":
            return render_cross_connects(d)
        raise SimulationError("Unsupported query")


def parametric_query(lower, command):
    parts = command.split()
    if lower.startswith(("show interface ", "show interfaces ", "display interface ")) and parts[-1].lower() in {"counters", "counter"}:
        return "counters", parts[-2] if len(parts) > 3 else None
    if lower.startswith(("show interfaces statistics", "show interfaces extensive")):
        return "counters", parts[-1] if len(parts) > 3 else None
    if lower.startswith(("show interface ", "show interfaces ", "display interface ")):
        return "interfaces", command.split()[-1]
    if lower.startswith("display current-configuration interface "):
        return "running_port", command.split()[-1]
    if lower.startswith("show vlan id "):
        return "vlans", lower.split()[-1]
    if lower.startswith("display vlan ") and lower.split()[-1].isdigit():
        return "vlans", lower.split()[-1]
    if lower.startswith(("show etherchannel ", "display eth-trunk ", "display link-aggregation verbose ")):
        return "lags", lower.split()[-1]
    if lower.startswith(("display traffic policy ", "display qos policy ")):
        return "policy", lower.split()[-1]
    return None, None


def vendor(d, family):
    meta = load_profile(d["profile"])["metadata"]
    import re
    match = re.search(r"Version\s+([\d.()\w]+)", meta["sys_descr"])
    return meta, match.group(1) if match else meta["version"]


def render_version(d, family):
    meta, sw = vendor(d, family)
    uptime = "1 day, 2 hours, 15 minutes"
    if family in {"vrp", "comware"}:
        huawei = family == "vrp"
        brand, model = ("HUAWEI", meta["model"].upper()) if huawei else ("H3C", meta["model"].upper())
        lines = ["Huawei Versatile Routing Platform Software" if huawei else "H3C Comware Software",
                 f"Copyright (c) 2000-2026 {brand} Technologies Co., Ltd.",
                 f"VRP (R) software, Version {sw} ({model} V200R021C00)" if huawei else f"Comware Software, Version {sw}",
                 f"Comware Version {sw}" if not huawei else f"Patch Version: V200R021SPH001",
                 f"{brand} {model} uptime is {uptime}",
                 f"ESN: {d['serial']}"]
        return "\n".join(lines)
    return "\n".join([meta["sys_descr"],
        "Copyright (c) 1986-2026 by Cisco Systems, Inc.",
        f"System image file is \"flash:{meta['model']}-universalk9.SSA.bin\"",
        f"{meta['model']} uptime is {uptime}",
        "System returned to ROM by reload",
        f"Processor board ID {d['serial']}",
        f"Serial Number: {d['serial']}",
        f"{len(d['interfaces'])} Gigabit Ethernet interfaces",
        "Last reset reason: Reload",
        "Configuration register is 0x2102"])


def render_esn(d):
    return f"ESN of slot 0: {d['serial']}"


def render_manuinfo(d):
    return "\n".join(["Chassis 1", "Slot 1",
        f"DEVICE_NAME           : {d['hostname']}",
        f"DEVICE_SERIAL_NUMBER  : {d['serial']}",
        f"SN: {d['serial']}",
        f"MAC_ADDRESS           : {d['chassis_mac']}",
        "MANUFACTURING_DATE    : 2026-01-01",
        "VENDOR_NAME           : H3C"])


def render_inventory(d, family):
    meta, _ = vendor(d, family)
    if family not in {"vrp", "comware"}:
        lines = [f"NAME: \"Chassis\", DESCR: \"{meta['sys_descr']}\"",
                 f"PID: {meta['model'].upper():<18}, VID: V01  , SN: {d['serial']}"]
        for n, card in enumerate(d.get("cards", []), 1):
            lines.extend([f"NAME: \"{card.get('name', card['id'])}\", DESCR: \"Line Card {n}\"",
                          f"PID: {meta['model'].upper():<18}, VID: V01  , SN: {card.get('serial', '')}"])
        return "\n".join(lines)
    lines = ["Slot  Type    Online   Status   Serial",
             f"0     {meta['model'].upper():<7} Present  Normal   {d['serial']}"]
    for n, card in enumerate(d.get("cards", []), 1):
        lines.append(f"{n}     {'LPU':<7} {'Present' if card.get('present', True) else 'Absent ':8} Normal   {card.get('serial', '')}")
    return "\n".join(lines)


def render_vlans(d, family, selected=None):
    vlans = [v for v in d["vlans"].values() if selected is None or str(v["vlan_id"]) == str(selected)]
    huawei = family in {"vrp", "comware"}
    if family == "comware":
        blocks = []
        for v in vlans:
            ports = [f"   {p['name']}" for p in d["interfaces"].values() if v["vlan_id"] in p.get("allowed_vlans", [])]
            blocks.extend([f"VLAN ID: {v['vlan_id']}", f"Name: {v['name']}", "VLAN Type: static",
                           f"Description: {v['name']}", "Tagged Ports: none", "Untagged Ports:", *ports, ""])
        return "\n".join(blocks)
    if huawei:
        lines = ["VID  Status  Property  MAC-LRN  Statistics  Description",
                 "---------------------------------------------------------------"]
        for v in vlans:
            lines.append(f"{v['vlan_id']:<5}{'enable':<8}{'default':<10}{'enable':<9}{'disable':<12}{v['name']}")
        lines.append(f"The total number of vlans is : {len(vlans)}")
        return "\n".join(lines)
    lines = ["VLAN Name                             Status    Ports",
             "---- -------------------------------- --------- -------------------------------"]
    for v in vlans:
        ports = [p["name"] for p in d["interfaces"].values() if v["vlan_id"] in p.get("allowed_vlans", [])]
        lines.append(f"{v['vlan_id']:<5}{v['name']:<34}{'active':<10}{', '.join(ports)}")
    return "\n".join(lines)


def l3_name(d, l3, family):
    port = d.get("interfaces", {}).get(l3.get("interface"))
    if port:
        return port["name"]
    vlan = l3.get("vlan", 0)
    return {"vrp": f"Vlanif{vlan}", "comware": f"Vlan-interface{vlan}",
            "junos": f"irb.{vlan}", "fortios": "port1"}.get(family, f"Vlan{vlan}")


def route_out_interface(d, route, family):
    peers = [l3 for l3 in d.get("l3_interfaces", []) if l3.get("vrf", "default") == route.get("vrf", "default")]
    if route.get("next_hop"):
        try:
            hop = IPv4Address(route["next_hop"])
            for l3 in peers:
                if hop in IPv4Network(l3["address"], strict=False):
                    return l3_name(d, l3, family)
        except (ValueError, TypeError, KeyError):
            pass
    return l3_name(d, peers[0], family) if peers else "-"


def render_routes(d, family):
    lines = ["Destination/Mask    Proto   Pre  Cost  Flags NextHop         Interface",
             "------------------------------------------------------------------------------"]
    for l3 in d.get("l3_interfaces", []):
        prefix = l3["address"]
        lines.append(f"{prefix:<20}{'Direct':<8}{'0':<5}{'0':<6}{'D':<6}{str(prefix).split('/')[0]:<16}{l3_name(d, l3, family)}")
    for route in d.get("routes", []):
        lines.append(f"{route['prefix']:<20}{'Static':<8}{'60':<5}{'0':<6}{'R':<6}{route['next_hop']:<16}{route_out_interface(d, route, family)}")
    return "\n".join(lines)


def render_sensors(d, family):
    meta, _ = vendor(d, family)
    lines = ["Slot  Sensor                Type       Status   Value  Unit      Upper  Lower",
             "--------------------------------------------------------------------------------"]
    for sensor in d.get("sensors", []):
        unit = sensor.get("units", "Celsius")
        lines.append(f"{'0':<6}{sensor.get('name', sensor['id'])[:20]:<21}{unit:<11}{'Normal':<9}"
                     f"{sensor['value']:<7}{unit:<10}{sensor.get('threshold', 80):<7}{sensor.get('clear_threshold', 70)}")
    return "\n".join(lines)


def render_resources(d, family, kind):
    res = d.get("resources", {})
    cpu = res.get("cpu_percent", 10)
    used = res.get("memory_used", 262144)
    total = res.get("memory_total", 1048576)
    pct = round(100*used/max(1, total))
    if kind == "memory":
        return "\n".join([f"Memory Using Percentage: {pct}%",
                          f"System Total Memory: {total} KB",
                          f"System Used Memory: {used} KB"])
    if family in {"vrp", "comware"}:
        return "\n".join(["CPU Usage Stat. Cycle: 60 (Second)",
                          f"CPU Usage            : {cpu}% Max: {max(cpu, 20)}%",
                          f"CPU Usage Stat. Time : 2026-11-09 14:23:45"])
    return f"CPU utilization for five seconds: {cpu}%/0%; one minute: {cpu}%; five minutes: {cpu}%"


def counter_rates(counter):
    return {"in_bps": counter.get("in_bytes_rate", 0), "out_bps": counter.get("out_bytes_rate", 0),
            "in_pps": counter.get("in_packets_rate", 0)//1_000_000_000,
            "out_pps": counter.get("out_packets_rate", 0)//1_000_000_000}


def util_percent(bits, speed_bps):
    if speed_bps <= 0:
        return 0
    return (bits*100 + speed_bps//2)//speed_bps


_ABBREVIATIONS = (("HundredGigE", "Hu"), ("TenGigabitEthernet", "Te"), ("GigabitEthernet", "Gi"),
                  ("FastEthernet", "Fa"), ("Ethernet", "Et"), ("Port-channel", "Po"))


def short_name(name):
    for prefix, brief in _ABBREVIATIONS:
        if name.lower().startswith(prefix.lower()):
            return brief+name[len(prefix):]
    return name


def counter_table(header, rows, name_width):
    name_width = max(name_width, len(header[0]))+2
    lines = [f"{header[0]:<{name_width}}"+"".join(f"{col:<15}" for col in header[1:]).rstrip()]
    for name, values in rows:
        lines.append(f"{name:<{name_width}}"+"".join(f"{str(v):<15}" for v in values).rstrip())
    return "\n".join(lines)


def render_interface_detail(p, counter, family):
    rates = counter_rates(counter)
    if family in {"vrp", "comware"}:
        state = "UP" if p["oper"] else "DOWN"
        return "\n".join([
            f"{p['name']} current state : {state}",
            f"Line protocol current state : {state}",
            f"Description:{p['description']}",
            f"Switch Port, PVID : {p['pvid']}, TPID : 8100(Hex)",
            f"Last 300 seconds input rate {rates['in_bps']} bits/sec, {rates['in_pps']} packets/sec",
            f"Last 300 seconds output rate {rates['out_bps']} bits/sec, {rates['out_pps']} packets/sec",
            f"    Input:  {counter.get('in_packets', 0)} packets, {counter.get('in_bytes', 0)} bytes",
            f"    Output: {counter.get('out_packets', 0)} packets, {counter.get('out_bytes', 0)} bytes",
            f"    Input errors: {counter.get('in_errors', 0)}, discards: {counter.get('in_discards', 0)}",
            f"    Output errors: {counter.get('out_errors', 0)}, discards: {counter.get('out_discards', 0)}",
            f"BW {p['speed_bps']//1000} Kbit, MAC {p['mac']}, MTU {p['mtu']}"])
    if family == "junos":
        return "\n".join([
            f"Physical interface: {p['name']}, Enabled, Physical link is {'Up' if p['oper'] else 'Down'}",
            f"  Interface index: {p['ifindex']}, SNMP ifIndex: {p['ifindex']}",
            f"  Link-level type: Ethernet, MTU: {p['mtu']}, Speed: {p['speed_bps']//1_000_000}mbps",
            f"  Input rate     : {rates['in_bps']} bps ({rates['in_pps']} pps)",
            f"  Output rate    : {rates['out_bps']} bps ({rates['out_pps']} pps)",
            f"  Input bytes    : {counter.get('in_bytes', 0)}",
            f"  Output bytes   : {counter.get('out_bytes', 0)}",
            f"  Input packets  : {counter.get('in_packets', 0)}",
            f"  Output packets : {counter.get('out_packets', 0)}",
            f"  Input errors   : {counter.get('in_errors', 0)}, discards: {counter.get('in_discards', 0)}",
            f"  Output errors  : {counter.get('out_errors', 0)}, discards: {counter.get('out_discards', 0)}"])
    state = "up" if p["oper"] else ("down" if p["admin"] else "administratively down")
    return "\n".join([
        f"{p['name']} is {state}, line protocol is {'up' if p['oper'] else 'down'}",
        f"  Hardware is Ethernet, address is {p['mac']}",
        f"  MTU {p['mtu']} bytes, BW {p['speed_bps']//1000} Kbit/sec",
        f"  5 minute input rate {rates['in_bps']} bits/sec, {rates['in_pps']} packets/sec",
        f"  5 minute output rate {rates['out_bps']} bits/sec, {rates['out_pps']} packets/sec",
        f"     {counter.get('in_packets', 0)} packets input, {counter.get('in_bytes', 0)} bytes",
        f"     {counter.get('in_errors', 0)} input errors, 0 CRC, 0 frame, 0 overrun, {counter.get('in_discards', 0)} ignored",
        f"     {counter.get('out_packets', 0)} packets output, {counter.get('out_bytes', 0)} bytes, {counter.get('out_discards', 0)} underruns",
        f"     {counter.get('out_errors', 0)} output errors, 0 collisions, 0 interface resets"])


def traffic_tuple(counter):
    return (counter.get("in_bytes", 0), counter.get("out_bytes", 0),
            counter.get("in_packets", 0), counter.get("out_packets", 0))


def error_tuple(counter):
    return (counter.get("in_errors", 0), counter.get("out_errors", 0),
            counter.get("in_discards", 0), counter.get("out_discards", 0))


def render_counters(d, counters, family, selected=None):
    ports = [p for p in d["interfaces"].values() if selected is None or p["id"] == selected]
    widest = max([len(p["name"]) for p in ports], default=8)
    if family in {"vrp", "comware"}:
        rows = [(p["name"], traffic_tuple(counters.get(p["id"], {}))) for p in ports]
        return counter_table(("Interface", "InOctets", "OutOctets", "InPkts", "OutPkts"), rows, widest)
    if family == "junos":
        rows = [(p["name"], traffic_tuple(counters.get(p["id"], {}))) for p in ports]
        return counter_table(("Interface", "In bytes", "Out bytes", "In packets", "Out packets"), rows, widest)
    rows = []
    for p in ports:
        c = counters.get(p["id"], {})
        traffic, errors = traffic_tuple(c), error_tuple(c)
        rows.append((short_name(p["name"]), (traffic[0], traffic[2], traffic[1], traffic[3], errors[0], errors[1])))
    return counter_table(("Port", "InOctets", "InUcastPkts", "OutOctets", "OutUcastPkts", "InErrors", "OutErrors"), rows, widest)


def render_if_brief(d, family, counters=None):
    huawei = family in {"vrp", "comware"}
    if huawei:
        lines = ["PHY: Physical", "*down: administratively down",
                 "InUti/OutUti: input utility/output utility",
                 "Interface                   PHY   Protocol  InUti  OutUti  inErrors  outErrors"]
        for p in d["interfaces"].values():
            state = "up" if p["oper"] else "*down"
            rates = counter_rates((counters or {}).get(p["id"]) or {})
            lines.append(f"{p['name']:<28}{state:<6}{state:<10}"
                         f"{str(util_percent(rates['in_bps'], p['speed_bps']))+'%':<7}"
                         f"{str(util_percent(rates['out_bps'], p['speed_bps']))+'%':<8}{'0':<10}0")
        return "\n".join(lines)
    lines = ["Interface              IP-Address      OK? Method Status                Protocol"]
    for l3 in d.get("l3_interfaces", []):
        name = d["interfaces"][l3["interface"]]["name"] if l3.get("interface") else f"Vlan{l3.get('vlan', 0)}"
        lines.append(f"{name:<23}{l3['address'].split('/')[0]:<16}{'YES':<4}{'manual':<7}{'up':<22}up")
    for p in d["interfaces"].values():
        state = "up" if p["oper"] else "down"
        lines.append(f"{p['name']:<23}{'unassigned':<16}{'YES':<4}{'unset':<7}{state:<22}{state}")
    return "\n".join(lines)


def render_switchport(d, family):
    blocks = []
    for p in d["interfaces"].values():
        blocks.extend([f"Name: {p['name']}", "Switchport: Enabled",
                       "Administrative Mode: static access", "Operational Mode: static access",
                       "Administrative Trunking Encapsulation: dot1q",
                       f"Access Mode VLAN: {p.get('pvid', 1)} (default)",
                       f"Trunking Native Mode VLAN: {p.get('pvid', 1)} (default)",
                       "Trunking VLANs Enabled: ALL", ""])
    return "\n".join(blocks)


def render_cdp(t):
    lines = ["Capability Codes: R - Router, T - Trans Bridge, B - Source Route Bridge",
             "Device ID        Local Intrfce     Holdtme  Capability  Platform    Port ID"]
    for n in t["lldp"]:
        lines.append(f"{n['hostname']:<17}{n['local_name']:<17}{'120':<9}{'R S I':<12}{'cisco':<12}{n['remote_port']}")
    return "\n".join(lines)


def render_lags(d):
    return "\n".join(f"{p['name']} {'UP' if p['oper'] else 'DOWN'}, min-links {p.get('min_links',1)}\n"+
        "\n".join(f"  {d['interfaces'][m]['name']} {'UP' if d['interfaces'][m]['oper'] else 'DOWN'}" for m in p["members"])
        for p in d["interfaces"].values() if p.get("members"))


def render_cross_connects(d):
    return "Source       Destination  State\n"+"\n".join(
        f"{c['source']:<13}{c['destination']:<13}up" for c in d.get("cross_connects", []))


def render_running_port(d, family, port_name, find_port):
    pid = find_port(port_name)
    p = d["interfaces"][pid]
    huawei = family in {"vrp", "comware"}
    lines = [f"interface {p['name']}", f" description {p['description']}", f" mtu {p['mtu']}"]
    lines.append(" undo shutdown" if huawei and p["admin"] else " no shutdown" if p["admin"] else " shutdown")
    if p.get("mode") == "routed":
        lines.append(" undo portswitch" if huawei else " no switchport")
    elif p.get("allowed_vlans") == [p.get("pvid")]:
        lines.append(f" {'port default vlan' if huawei else 'switchport access vlan'} {p.get('pvid')}")
    else:
        lines.append(f" {'port trunk allow-pass vlan' if family == 'vrp' else 'port trunk permit vlan' if huawei else 'switchport trunk allowed vlan'} {','.join(map(str, p['allowed_vlans']))}")
    lines.append("#")
    return "\n".join(lines)+"\n"


def render_config(d, family):
    huawei = family in {"vrp", "comware"}
    lines = []
    if family == "junos":
        lines.append(f"set system host-name {d['hostname']}")
        for p in d["interfaces"].values():
            if not p["admin"]:
                lines.append(f"set interfaces {p['name']} disable")
            if p["description"]:
                lines.append(f'set interfaces {p["name"]} description "{p["description"]}"')
        for v in d["vlans"].values():
            lines.append(f"set vlans {v['name']} vlan-id {v['vlan_id']}")
    elif family == "fortios":
        lines.extend(("config system global", f' set hostname "{d["hostname"]}"', "end", "config system interface"))
        for p in d["interfaces"].values():
            lines.extend((f' edit "{p["name"]}"', f"  set status {'up' if p['admin'] else 'down'}", f'  set alias "{p["description"]}"', " next"))
        lines.append("end")
    else:
        lines.append(f"{'sysname' if huawei else 'hostname'} {d['hostname']}")
        for v in d["vlans"].values():
            lines.extend((f"vlan {v['vlan_id']}", f" name {v['name']}"))
        for p in d["interfaces"].values():
            lines.extend((f"interface {p['name']}", f" description {p['description']}", f" mtu {p['mtu']}", f" {'undo' if huawei else 'no'} shutdown" if p["admin"] else " shutdown"))
            if p.get("mode") == "routed":
                lines.append(" undo portswitch" if huawei else " no switchport")
            elif p["allowed_vlans"] == [p["pvid"]]:
                lines.append(f" {'port default vlan' if huawei else 'switchport access vlan'} {p['pvid']}")
            else:
                lines.extend((" port link-type trunk" if huawei else " switchport mode trunk", f" {'port trunk allow-pass vlan' if family == 'vrp' else 'port trunk permit vlan' if huawei else 'switchport trunk allowed vlan'} {','.join(map(str,p['allowed_vlans']))}"))
    for l3 in d["l3_interfaces"]:
        name = l3_name(d, l3, family)
        if family == "junos":
            lines.append(f"set interfaces {name} unit 0 family inet address {l3['address']}")
        elif family == "fortios":
            lines.extend(("config system interface",f' edit "{name}"', f"  set ip {l3['address']}"," next","end"))
        else:
            lines.extend((f"interface {name}",f" ip address {l3['address']}"))
            if not l3.get("admin",True):
                lines.append(" shutdown")
    for route in d["routes"]:
        lines.append(f"{'set routing-options static route' if family == 'junos' else 'ip route-static' if huawei else 'ip route'} {route['prefix']} {'next-hop ' if family == 'junos' else ''}{route['next_hop']}")
    return "\n".join(lines)+"\n"
