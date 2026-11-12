"""Real PySNMP agent; asynchronous SETs commit at the control plane."""
import asyncio
import time
from pysnmp.entity import config, engine
from pysnmp.entity.rfc3413 import cmdrsp, context
from pysnmp.carrier.asyncio.dgram import udp
from pysnmp.proto import rfc1902, rfc1905
from pysnmp.proto.api import v2c
from pysnmp.smi import error
from idc_simulator.oid import OidProvider
from idc_simulator.errors import Conflict, Unavailable, SimulationError


class Instrumentation:
    def __init__(self, client, context_name="default"):
        self.client = client
        self.context_name = context_name
        self.provider = None

    def begin(self):
        self.provider = OidProvider(self.client.current, time.monotonic_ns(), self.context_name)

    @staticmethod
    def access(view, pair, ctx):
        if ctx.get("acFun"):
            return ctx["acFun"](view, pair, **ctx)
        return False

    def read_variables(self, *varbinds, **ctx):
        result = []
        for i, (oid, _) in enumerate(varbinds):
            value = self.provider.get(oid)
            if self.access("read", (oid, value), dict(ctx, idx=i)):
                value = rfc1905.NoSuchObject()
            result.append((oid, value))
        return result

    def read_next_variables(self, *varbinds, **ctx):
        result = []
        for i, (oid, _) in enumerate(varbinds):
            current = tuple(oid)
            while True:
                next_oid, value = self.provider.get_next(current)
                if next_oid == current:
                    break
                try:
                    denied = self.access("read", (rfc1902.ObjectIdentifier(next_oid), value), dict(ctx, idx=i))
                except error.NoAccessError:
                    denied = True
                if not denied:
                    break
                current = next_oid
            result.append((rfc1902.ObjectIdentifier(next_oid), value))
        return result


class FrozenRequest:
    def handle_management_operation(self, snmpEngine, reference, contextName, pdu):
        started = time.monotonic()
        if not self.snmpContext.get_mib_instrum(contextName).client.current["runtime"]["online"]:
            return
        self.snmpContext.get_mib_instrum(contextName).begin()
        count = len(v2c.apiPDU.get_varbinds(pdu))
        if count > 64:
            self.send_varbinds(snmpEngine, reference, "tooBig", 0, [])
            return
        try:
            super().handle_management_operation(snmpEngine, reference, contextName, pdu)
        finally:
            if self.cbCtx:
                self.cbCtx.increment("snmp_requests_total")
                self.cbCtx.observe("snmp_response_seconds", time.monotonic()-started)


class GetResponder(FrozenRequest, cmdrsp.GetCommandResponder):
    pass


class NextResponder(FrozenRequest, cmdrsp.NextCommandResponder):
    pass


class BulkResponder(FrozenRequest, cmdrsp.BulkCommandResponder):
    max_varbinds = 256

    def handle_management_operation(self, snmpEngine, reference, contextName, pdu):
        if not int(v2c.apiBulkPDU.get_max_repetitions(pdu)) and not int(v2c.apiBulkPDU.get_non_repeaters(pdu)):
            self.send_varbinds(snmpEngine, reference, 0, 0, [])
            return
        super().handle_management_operation(snmpEngine, reference, contextName, pdu)


class SetResponder(cmdrsp.SetCommandResponder):
    def __init__(self, *args, **kwargs):
        self.pending = set()
        self.tasks = set()
        super().__init__(*args, **kwargs)

    def release_state_information(self, reference):
        if reference not in self.pending:
            super().release_state_information(reference)

    def handle_management_operation(self, snmpEngine, reference, contextName, pdu):
        instr = self.snmpContext.get_mib_instrum(contextName)
        if not instr.client.current["runtime"]["online"]:
            return
        instr.begin()
        varbinds = v2c.apiPDU.get_varbinds(pdu)
        if len(varbinds) > 64:
            raise error.TooBigError(idx=0)
        for i, pair in enumerate(varbinds):
            if self.verify_access("write", pair, snmpEngine=snmpEngine, idx=i):
                raise error.NotWritableError(idx=i)
        operations = instr.provider.prepare_set(varbinds)
        if len(self.pending) >= 32:
            raise error.ResourceUnavailableError(idx=0)
        self.pending.add(reference)
        task = asyncio.create_task(self.commit(snmpEngine, reference, instr, operations, varbinds, instr.provider.snapshot["revision"]))
        self.tasks.add(task); task.add_done_callback(self.tasks.discard)

    async def commit(self, snmpEngine, reference, instr, operations, varbinds, revision):
        status = 0
        try:
            await instr.client.transact(operations, revision)
        except Conflict:
            status = "inconsistentValue"
        except Unavailable:
            status = "resourceUnavailable"
        except SimulationError:
            status = "wrongValue"
        try:
            self.send_varbinds(snmpEngine, reference, status, 1 if status else 0, varbinds)
        finally:
            self.pending.discard(reference)
            super().release_state_information(reference)


class SnmpAgent:
    def __init__(self, client, address, port, credentials, metrics):
        self.client = client
        self.credentials = credentials
        self.metrics = metrics
        snapshot = client.current
        identity = snapshot["runtime"]
        self.engine = engine.SnmpEngine(snmpEngineID=rfc1902.OctetString(hexValue=identity["engine_id"]), maxMessageSize=65507)
        boots, = self.engine.get_mib_builder().import_symbols("__SNMP-FRAMEWORK-MIB", "snmpEngineBoots")
        boots.syntax = boots.syntax.clone(identity["engine_boots"])
        self.transport = udp.UdpTransport().open_server_mode((address, port))
        config.add_transport(self.engine, udp.DOMAIN_NAME, self.transport)
        self.context = context.SnmpContext(self.engine)
        self.context.unregister_context_name(b"")
        vrfs = {"default", *(l["vrf"] for l in snapshot["device"]["l3_interfaces"])}
        for vrf in vrfs:
            name = b"" if vrf == "default" else vrf.encode()
            self.context.register_context_name(name, Instrumentation(client, vrf))
        versions = credentials.get("versions", ["v1", "v2c", "v3"])
        readonly = credentials["community"]
        config.add_v1_system(self.engine, "read", readonly)
        if credentials.get("write_community"):
            config.add_v1_system(self.engine, "write", credentials["write_community"])
        for version, model in (("v1", 1), ("v2c", 2)):
            if version not in versions:
                continue
            for vrf in vrfs:
                name = b"" if vrf == "default" else vrf.encode()
                config.add_vacm_user(self.engine, model, "read", "noAuthNoPriv", (1,), (0, 0), (1,), contextName=name)
                if credentials.get("write_community"):
                    config.add_vacm_user(self.engine, model, "write", "noAuthNoPriv", (1,), (1,), (1,), contextName=name)
        if "v3" in versions:
            user = credentials["v3_user"]
            config.add_v3_user(self.engine, user, config.USM_AUTH_HMAC96_SHA, credentials["v3_auth"], config.USM_PRIV_CFB128_AES, credentials["v3_priv"])
            for vrf in vrfs:
                config.add_vacm_user(self.engine, 3, user, "authPriv", (1,), (1,) if credentials.get("v3_write", False) else (0, 0), (1,), contextName=b"" if vrf == "default" else vrf.encode())
        self.responders = [c(self.engine, self.context, cbCtx=metrics) for c in (GetResponder, NextResponder, BulkResponder, SetResponder)]
        client.on_snapshot = self.synchronize_identity
        self.synchronize_identity()

    def synchronize_identity(self):
        desired = {b"", *(l["vrf"].encode() for l in self.client.current["device"]["l3_interfaces"] if l["vrf"] != "default")}
        for name in set(self.context.context_names) - desired:
            self.context.unregister_context_name(name)
        for name in desired - set(self.context.context_names):
            self.context.register_context_name(name,Instrumentation(self.client,name.decode() if name else "default"))
            for version, model in (("v1",1),("v2c",2)):
                if version in self.credentials.get("versions",["v1","v2c","v3"]):
                    config.add_vacm_user(self.engine,model,"read","noAuthNoPriv",(1,),(0,0),(1,),contextName=name)
                    if self.credentials.get("write_community"):
                        config.add_vacm_user(self.engine,model,"write","noAuthNoPriv",(1,),(1,),(1,),contextName=name)
            if "v3" in self.credentials.get("versions",["v1","v2c","v3"]):
                config.add_vacm_user(self.engine,3,self.credentials["v3_user"],"authPriv",(1,),(1,) if self.credentials.get("v3_write") else (0,0),(1,),contextName=name)
        identity = self.client.current["runtime"]
        boots, clock = self.engine.get_mib_builder().import_symbols("__SNMP-FRAMEWORK-MIB", "snmpEngineBoots", "snmpEngineTime")
        boots.syntax = boots.syntax.clone(identity["engine_boots"])
        elapsed = max(0, (time.monotonic_ns()-identity.get("engine_start_ns", identity["boot_ns"]))/1e9)
        clock.syntax = clock.syntax.clone(int(time.time()-elapsed))

    async def ready(self):
        await self.transport._lport

    async def close(self):
        for responder in self.responders:
            if isinstance(responder, SetResponder):
                for task in tuple(responder.tasks):
                    task.cancel()
                await asyncio.gather(*tuple(responder.tasks), return_exceptions=True)
            responder.close(self.engine)
        self.engine.close_dispatcher()


class NotificationAgent:
    def __init__(self, client, address, credentials):
        self.client, self.credentials = client, credentials
        self.engine = engine.SnmpEngine()
        self.transport = udp.UdpTransport().open_server_mode((address, 0))
        config.add_transport(self.engine, udp.DOMAIN_NAME, self.transport)
        config.add_v1_system(self.engine, "read", credentials["community"])
        config.add_vacm_user(self.engine, 2, "read", "noAuthNoPriv", (0, 0), (0, 0), (1,))

    async def ready(self):
        await self.transport._lport

    def synchronize_identity(self):
        pass

    async def close(self):
        self.engine.close_dispatcher()
