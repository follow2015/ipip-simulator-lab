"""Brand syntax plugins translate commands into shared state operations."""
import shlex
from ipaddress import IPv4Network
from idc_simulator.errors import SimulationError


class CommonPlugin:
    async def execute(self, session, command):
        return None

    async def finish(self, session):
        raise SimulationError("This brand has no edit/next syntax")


class JunosPlugin(CommonPlugin):
    async def execute(self, session, command):
        lower = command.lower()
        if lower == "rollback 0":
            session.candidate.clear(); session.revision = session.client.current["revision"]; return ""
        if lower == "commit":
            if session.mode == "exec":
                raise SimulationError("Configuration mode required")
            result = await session.client.transact(session.candidate+[{"kind": "save"}], session.revision)
            session.candidate.clear(); session.revision = result["revision"]
            return "commit complete"
        if lower.startswith(("set ", "delete ")):
            return await self.parse(session, command)

    async def parse(self, session, command):
        args = shlex.split(command)
        delete = args[0] == "delete"
        if args[1:3] == ["system", "host-name"] and not delete:
            await session.change([{"kind": "system", "fields": {"hostname": args[3]}}]); return ""
        if args[1] == "interfaces":
            port = session.find_port(args[2])
            if args[3] == "disable":
                await session.change([{"kind": "interface", "port": port, "fields": {"admin": delete}}]); return ""
            if args[3] == "description":
                await session.change([{"kind": "interface", "port": port, "fields": {"description": "" if delete else " ".join(args[4:])}}]); return ""
            if args[3:8] == ["unit", "0", "family", "inet", "address"]:
                p = session.client.current["device"]["interfaces"][port]
                await session.change([{"kind": "l3", "delete": delete, "value": {"id": port, "interface": port, "address": args[8], "ifindex": p["ifindex"], "vrf": "default"}}]); return ""
        if args[1:3] == ["routing-options", "static"] and args[3] == "route":
            value = {"prefix": args[4], "vrf": "default"}
            if len(args) > 6:
                value["next_hop"] = args[6]
            elif not delete:
                raise SimulationError("Junos static route requires next-hop")
            await session.change([{"kind": "route", "delete": delete, "value": value}]); return ""
        if args[1] == "vlans" and args[3] == "vlan-id":
            await session.change([{"kind": "vlan", "vlan": int(args[4]), "name": args[2], "delete": delete}]); return ""
        raise SimulationError("Unsupported Junos statement")

class FortiosPlugin(CommonPlugin):
    async def execute(self, session, command):
        lower = command.lower()
        if lower in {"config system interface", "config firewall policy", "config router static"}:
            if session.edit:
                raise SimulationError("Finish edit with next")
            session.mode = "config"; session.tree = command[7:]; session.revision = session.client.current["revision"]; return ""
        if lower.startswith("edit "):
            if not session.tree:
                raise SimulationError("config tree required")
            session.edit = shlex.split(command)[1]; session.edit_fields = {}
            if session.tree == "system interface":
                session.port = session.find_port(session.edit)
            return ""
        if lower == "next":
            await self.finish(session); return ""
        if lower.startswith("set "):
            if not session.edit:
                raise SimulationError("edit required")
            args = shlex.split(command)
            allowed = {"system interface": {"status", "alias", "ip", "vlanid"}, "firewall policy": {"srcaddr", "dstaddr", "action"}, "router static": {"dst", "gateway"}}[session.tree]
            if args[1] not in allowed:
                raise SimulationError("Unsupported FortiOS field")
            session.edit_fields[args[1]] = args[2:]; return ""

    async def finish(self, session):
        if not session.edit:
            raise SimulationError("edit required")
        fields = session.edit_fields
        operations = []
        if session.tree == "system interface":
            patch = {}
            for key, value in fields.items():
                if key == "status":
                    if value[0] not in {"up", "down"}:
                        raise SimulationError("status must be up/down")
                    patch["admin"] = value[0] == "up"
                elif key == "alias":
                    patch["description"] = " ".join(value)
                elif key == "vlanid":
                    patch["pvid"] = int(value[0]); patch["allowed_vlans"] = [int(value[0])]
                elif key == "ip":
                    address = value[0] if "/" in value[0] else f"{value[0]}/{IPv4Network('0.0.0.0/'+value[1]).prefixlen}"
                    p = session.client.current["device"]["interfaces"][session.port]
                    operations.append({"kind": "l3", "value": {"id": session.port, "interface": session.port, "address": address, "ifindex": p["ifindex"], "vrf": "default"}})
            operations.append({"kind": "interface", "port": session.port, "fields": patch})
        elif session.tree == "firewall policy":
            operations = [{"kind": "firewall", "value": {"id": session.edit, "source": fields["srcaddr"][0], "destination": fields["dstaddr"][0], "action": {"accept": "allow", "deny": "deny"}[fields["action"][0]]}}]
        elif session.tree == "router static":
            dst = fields["dst"]
            prefix = dst[0] if "/" in dst[0] else f"{dst[0]}/{IPv4Network('0.0.0.0/'+dst[1]).prefixlen}"
            operations = [{"kind": "route", "value": {"prefix": prefix, "next_hop": fields["gateway"][0], "vrf": "default"}}]
        await session.change(operations+[{"kind": "save"}])
        session.edit = None; session.port = None; session.edit_fields = {}


PLUGINS = {"junos": JunosPlugin(), "fortios": FortiosPlugin()}
DEFAULT_PLUGIN = CommonPlugin()

def plugin_for(family):
    return PLUGINS.get(family, DEFAULT_PLUGIN)
