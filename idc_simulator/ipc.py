"""Bounded JSON-line RPC over an owner-only Unix socket, shared across netns."""
import asyncio
import json
import os
from contextlib import suppress
from pathlib import Path
import socket
import struct
import sys
from idc_simulator.errors import SimulationError, Conflict, NotFound, Unavailable

MAX_MESSAGE = 16 * 1024 * 1024


class RpcServer:
    def __init__(self, path, handler):
        self.path = Path(path)
        self.handler = handler
        self.server = None
        self.peer_pid = None

    async def start(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        if self.path.exists():
            try:
                reader, writer = await asyncio.open_unix_connection(str(self.path))
            except (ConnectionRefusedError, FileNotFoundError):
                self.path.unlink(missing_ok=True)
            else:
                writer.close(); await writer.wait_closed()
                raise SimulationError("Control socket already in use")
        self.server = await asyncio.start_unix_server(self._connection, path=str(self.path), limit=MAX_MESSAGE)
        os.chmod(self.path, 0o600)

    async def _connection(self, reader, writer):
        try:
            if self.peer_pid is not None:
                sock = writer.get_extra_info("socket")
                if sys.platform == "linux":
                    pid,_,_ = struct.unpack("3i",sock.getsockopt(socket.SOL_SOCKET,socket.SO_PEERCRED,12))
                elif sys.platform == "darwin":
                    pid = struct.unpack("i",sock.getsockopt(0,2,4))[0]
                else:
                    raise SimulationError("Worker peer authentication unsupported on this OS")
                if pid != self.peer_pid:
                    with suppress(ConnectionError, asyncio.TimeoutError):
                        await asyncio.wait_for(reader.readline(), 1)
                    raise SimulationError("RPC peer is not the assigned worker")
            raw = await asyncio.wait_for(reader.readline(), 5)
            if not raw:
                writer.close()
                with suppress(ConnectionError):
                    await writer.wait_closed()
                return
            request = json.loads(raw)
            result = await self.handler(request)
            response = {"ok": True, "result": result}
        except SimulationError as exc:
            response = {"ok": False, "status": exc.status, "error": str(exc)}
            if isinstance(exc, Unavailable):
                response.update(outcome=exc.outcome, operation_id=exc.operation_id)
        except (KeyError, TypeError, ValueError, asyncio.TimeoutError) as exc:
            response = {"ok": False, "status": 400, "error": str(exc)}
        except Exception:
            import logging
            logging.exception("Control RPC failed")
            response = {"ok": False, "status": 500, "error": "Internal control error"}
        try:
            writer.write(json.dumps(response).encode()+b"\n")
            await writer.drain()
        except ConnectionError:
            pass
        finally:
            writer.close()
            with suppress(ConnectionError):
                await writer.wait_closed()

    async def close(self):
        if self.server:
            self.server.close(); await self.server.wait_closed()
            self.path.unlink(missing_ok=True)


class RpcClient:
    def __init__(self, path, dev=None, role="query"):
        self.path, self.dev = str(path), dev
        self.role = role
        self.current = None
        self.last_sync_ns = 0
        self.sync_full = 0
        self.sync_not_modified = 0
        self.response_bytes = 0

    async def call(self, method, **params):
        operation_id = params.get("request_id") or params.get("operation_id")
        try:
            reader, writer = await asyncio.wait_for(asyncio.open_unix_connection(self.path, limit=MAX_MESSAGE), 2)
        except (OSError, asyncio.TimeoutError, AttributeError) as exc:
            raise Unavailable(f"Control unavailable: {exc}", "not_executed", operation_id) from exc
        submitted = False
        try:
            try:
                writer.write(json.dumps({"method": method, "device": self.dev, "role": self.role, **params}).encode()+b"\n")
                submitted = True
                await writer.drain()
                raw = await asyncio.wait_for(reader.readline(), 5)
                self.response_bytes += len(raw)
                reply = json.loads(raw)
            finally:
                writer.close()
                with suppress(OSError):
                    await writer.wait_closed()
        except (OSError, asyncio.TimeoutError, ValueError) as exc:
            outcome = "unknown" if submitted else "not_executed"
            raise Unavailable(f"Control unavailable: {exc}", outcome, operation_id) from exc
        if not reply["ok"]:
            cls = {409: Conflict, 404: NotFound, 503: Unavailable}.get(reply["status"], SimulationError)
            if cls is Unavailable:
                raise Unavailable(reply["error"], reply.get("outcome", "unknown"), reply.get("operation_id"))
            raise cls(reply["error"])
        return reply["result"]

    async def refresh(self):
        import time
        reply = await self.call("snapshot",conditional=True,
            control_epoch=self.current.get("control_epoch") if self.current else None,
            state_seq=self.current.get("state_seq") if self.current else None)
        modified = reply.get("mode") != "not_modified"
        if modified:
            snap = reply.get("snapshot",reply)
            if not self.current or snap.get("control_epoch") != self.current.get("control_epoch") or snap["revision"] >= self.current["revision"]:
                self.current = snap
            self.sync_full += 1
        else:
            if not self.current:
                raise Unavailable("Control returned not_modified without a local snapshot")
            self.current = {**self.current,**{key:reply[key] for key in (
                "revision","config_revision","topology_revision","state_seq","control_epoch","effective_ns")}}
            self.sync_not_modified += 1
        callback = getattr(self, "on_snapshot", None)
        if callback and modified:
            callback()
        self.last_sync_ns = time.monotonic_ns()
        return self.current

    async def transact(self, operations, revision=None, request_id=None):
        result = await self.call("transact", operations=operations, revision=revision if revision is not None else self.current["revision"], request_id=request_id)
        await self.refresh()
        return result
