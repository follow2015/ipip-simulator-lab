"""Control API, worker orchestration and owner-only internal RPC."""
import asyncio
import hmac
import json
import os
from hashlib import sha256
from ipaddress import ip_address, ip_network
from pathlib import Path
import signal
import time
import logging
from concurrent.futures import ThreadPoolExecutor
from functools import partial
from uuid import uuid4
from tempfile import mkdtemp
import shutil
from urllib.parse import urlsplit
from aiohttp import web
from idc_simulator import __version__
from idc_simulator.control import Controller
from idc_simulator.deploy import Supervisor
from idc_simulator.errors import SimulationError, NotFound, Unavailable
from idc_simulator.ipc import RpcServer
from idc_simulator.metrics import Metrics, worker_sample
from idc_simulator.scenarios import ScenarioRunner

try:
    import fcntl
except ImportError:
    fcntl = None


DEFAULT_ORIGIN_PORTS = {"http": 80, "https": 443}


def normalize_origin(value):
    parsed = urlsplit(value)
    if parsed.scheme.lower() not in DEFAULT_ORIGIN_PORTS or parsed.username or parsed.password:
        return value
    if parsed.path not in ("", "/") or parsed.query or parsed.fragment:
        return value
    host = (parsed.hostname or "").lower()
    if not host:
        return value
    try:
        port = parsed.port
    except ValueError:
        return value
    if port == DEFAULT_ORIGIN_PORTS[parsed.scheme.lower()]:
        port = None
    authority = f"[{host}]" if ":" in host else host
    if port is not None:
        authority = f"{authority}:{port}"
    return f"{parsed.scheme.lower()}://{authority}"


def is_loopback(bind):
    try:
        return ip_address(str(bind)).is_loopback
    except ValueError:
        return False


WILDCARD_BINDS = {"0.0.0.0", "::", "[::]"}

GENERATED_SUBNET_RESERVATION_NS = 600 * 1_000_000_000


def generated_host_mac(identity, host_index):
    digest = sha256(f"{identity}:{host_index}".encode()).hexdigest()
    return "02:" + ":".join(digest[i:i+2] for i in range(0, 10, 2))


def configured_hosts(bind, raw):
    hosts = [h.strip().lower() for h in (raw or "").split(",") if h.strip()]
    if bind and str(bind).strip().lower() not in WILDCARD_BINDS:
        hosts.append(str(bind).strip().lower())
    return list(dict.fromkeys(hosts))


def prevent_unauthenticated_bind(bind, token, allow_unauthenticated=False):
    if token or allow_unauthenticated or is_loopback(bind):
        return
    raise SystemExit(f"Refusing to serve the control API on {bind} without IDC_API_TOKEN. "
                     "Set a token, bind to loopback, or pass --allow-unauthenticated for an isolated lab.")


def credentials_from_env():
    return {"community": os.getenv("IDC_SNMP_COMMUNITY", "sim-read"),
        "write_community": os.getenv("IDC_SNMP_WRITE_COMMUNITY", "sim-write"),
        "v3_user": os.getenv("IDC_SNMP_V3_USER", "sim-user"),
        "v3_auth": os.getenv("IDC_SNMP_V3_AUTH", "sim-auth-password"),
        "v3_priv": os.getenv("IDC_SNMP_V3_PRIV", "sim-priv-password"), "v3_write": True,
        "ssh_user": os.getenv("IDC_SSH_USER", "sim"), "ssh_password": os.getenv("IDC_SSH_PASSWORD", "sim-password"),
        "notifications": json.loads(os.getenv("IDC_NOTIFICATION_TARGETS", "[]")),
        "credential_sets": json.loads(os.getenv("IDC_CREDENTIAL_SETS", "{}"))}


class Runtime:
    def __init__(self, directory, mode="local", snmp_port=1161, ssh_port=8022, credentials=None, worker_uid=None, model_only=False):
        self.directory = Path(directory).resolve()
        self.directory.mkdir(parents=True, exist_ok=True)
        self.owner_lock = None
        self.started = False
        if worker_uid is not None:
            os.chmod(self.directory, 0o711)
        self.controller = Controller(self.directory/"state.db")
        os.chmod(self.directory/"state.db", 0o600)
        self._generated_reservations = {}
        self.metrics = Metrics()
        self.socket = self.directory/"control.sock"
        self.socket_directory = self.directory/"sock"
        self.temporary_socket_directory = None
        if len(os.fsencode(self.socket)) > 100:
            self.socket_directory = Path(mkdtemp(prefix="idc-sock-"))
            self.temporary_socket_directory = self.socket_directory
            self.socket = self.socket_directory/"control.sock"
        elif len(os.fsencode(self.socket_directory/"w-00000000-00000000.sock")) > 100:
            self.socket_directory = Path(mkdtemp(prefix="idc-sock-"))
            self.temporary_socket_directory = self.socket_directory
        if not self.socket_directory.exists():
            self.socket_directory.mkdir(mode=0o700, parents=True)
        os.chmod(self.socket_directory, 0o711 if worker_uid is not None else 0o700)
        self.rpc = RpcServer(self.socket, self.handle_rpc)
        self.rpc.peer_pid = os.getpid()
        self.executor = ThreadPoolExecutor(max_workers=1,thread_name_prefix="idc-state")
        self.state_slots = asyncio.Semaphore(256)
        self.state_timeout = float(os.getenv("IDC_STATE_TIMEOUT", "30"))
        self.lifecycle_timeout = float(os.getenv("IDC_LIFECYCLE_TIMEOUT", "300"))
        self.shutdown_timeout = float(os.getenv("IDC_SHUTDOWN_TIMEOUT",
            str(max(2*self.state_timeout, 60))))
        self.state_timeout_at = 0.0
        self.state_timeout_function = None
        self.channels = {}
        self.supervisor = Supervisor(self.controller, self.directory, self.socket, credentials or credentials_from_env(), mode, snmp_port, ssh_port, worker_uid,
            state_call=self.state_call,channel_factory=self.worker_channel)
        self.scenarios = ScenarioRunner(self.controller,self.state_call)
        self.worker_uid = worker_uid
        self.locks = {}
        self.lifecycle_tasks = set()
        self.mode, self.model_only = mode, model_only
        self.auth_enabled = False
        self.retention_rows = int(os.getenv("IDC_EVENT_RETENTION_ROWS", "100000"))
        self.operation_hours = float(os.getenv("IDC_OPERATION_RETENTION_HOURS", "24"))
        self.checkpoint_seconds = float(os.getenv("IDC_CHECKPOINT_SECONDS", "30"))
        self.maintenance = None

    async def start(self):
        if self.started:
            return
        descriptor = None
        if fcntl is None:
            logging.warning("State directory locking is unavailable on this platform; "
                            "ensure a single control process owns %s", self.directory)
        else:
            descriptor = os.open(self.directory/"control.lock", os.O_CREAT | os.O_RDWR, 0o600)
            try:
                fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError:
                os.close(descriptor)
                raise SimulationError("Control state directory already in use") from None
            self.owner_lock = descriptor
        try:
            if not hasattr(asyncio, "start_unix_server"):
                if not self.model_only:
                    raise SimulationError("Control RPC requires Unix domain sockets; "
                                          "use --model-only on this platform")
                logging.warning("Unix domain sockets are unavailable; "
                                "model-only mode runs without the control RPC socket")
            else:
                await self.rpc.start()
        except BaseException:
            if self.owner_lock is not None:
                os.close(self.owner_lock)
                self.owner_lock = None
            raise
        self.started = True
        for lab, state in tuple(self.controller.labs.items()):
            if not self.model_only and state["status"] in {"running", "degraded"}:
                try:
                    await self.supervisor.start(lab)
                except Exception:
                    await self.state_call(self.controller.lab_status,lab,"degraded")
                    self.metrics.increment("lab_resume_errors_total")
                    logging.exception("Failed to resume lab %s; control API remains available",lab)
        self.maintenance = asyncio.create_task(self.maintain())

    async def maintain(self):
        last_prune = time.monotonic()
        while True:
            await asyncio.sleep(self.checkpoint_seconds)
            try:
                await self.state_call(self.controller.checkpoint)
                if time.monotonic()-last_prune >= 60:
                    last_prune = time.monotonic()
                    removed = await self.state_call(self.controller.prune,event_rows=self.retention_rows, operation_hours=self.operation_hours)
                    if removed:
                        self.metrics.increment("events_pruned_total", removed)
            except asyncio.CancelledError:
                raise
            except Exception:
                self.metrics.increment("maintenance_errors_total")

    async def handle_rpc(self, request):
        if request.get("method") == "transact":
            request = {**request, "request_id": request.get("request_id") or str(uuid4())}
        try:
            return await self.state_call(self._handle_rpc,request)
        except Unavailable as exc:
            exc.operation_id = request.get("request_id")
            raise

    async def state_call(self,function,*args,_state_wait=False,_state_budget=None,**kwargs):
        bypass = _state_wait or _state_budget is not None or asyncio.current_task() in self.lifecycle_tasks
        if not bypass and self.state_timeout_function and time.monotonic()-self.state_timeout_at < self.state_timeout:
            raise Unavailable(f"Control state is recovering from a timed-out call ({self.state_timeout_function}); retry later")
        try:
            await asyncio.wait_for(self.state_slots.acquire(),2)
        except asyncio.TimeoutError:
            raise Unavailable("Control state queue is busy; retry later") from None
        started = time.monotonic()
        try:
            future = self.executor.submit(partial(function,*args,**kwargs))
        except BaseException:
            self.state_slots.release()
            raise
        pending = asyncio.wrap_future(future)
        def completed(done):
            self.state_slots.release()
            if not done.cancelled():
                done.exception()
        pending.add_done_callback(completed)
        try:
            timeout = _state_budget if _state_budget is not None else (None if bypass else self.state_timeout)
            done, _ = await asyncio.wait({pending}, timeout=timeout)
            if not done:
                self.state_timeout_at = time.monotonic()
                self.state_timeout_function = getattr(function,"__name__",repr(function))
                self.metrics.increment("state_call_timeouts_total")
                logging.error("Control state call %s exceeded %.1fs; admitting probes only",
                    self.state_timeout_function,timeout)
                outcome = "not_executed" if future.cancel() else "unknown"
                raise Unavailable(f"Control state operation timed out after {timeout:g}s", outcome)
            result = pending.result()
        except asyncio.CancelledError:
            future.cancel()
            raise
        finally:
            self.metrics.observe("state_call_seconds",time.monotonic()-started)
        if self.state_timeout_function:
            logging.warning("Control state executor recovered after %s",self.state_timeout_function)
            self.state_timeout_function = None
        return result

    async def worker_channel(self,dev,role):
        key = (dev,role)
        if key in self.channels:
            channel = self.channels[key]
            channel.peer_pid = -1
            return channel
        path = self.socket_directory/f"w-{len(self.channels):08x}-{uuid4().hex[:8]}.sock"
        async def bound(request):
            if request.get("device") != dev or request.get("role","query") != role:
                raise SimulationError("RPC device or role does not match this worker channel")
            if role == "notification" and request.get("method") not in {"snapshot","events","delivery","notification_cursor"}:
                raise SimulationError("Notification workers cannot modify configuration")
            if request.get("method") in {"transact","preview"}:
                if any(op.get("kind") not in {"interface","lag","system","vlan","l3","route","firewall","cross_connect","save","reboot"} for op in request.get("operations",[])):
                    raise SimulationError("Worker cannot execute control-only operations")
            if request.get("method") == "delivery":
                def owns_event():
                    return self.controller.store.db.execute("SELECT 1 FROM events WHERE id=? AND device_id=?",(request["event_id"],dev)).fetchone()
                if not await self.state_call(owns_event):
                    raise SimulationError("Delivery event belongs to another device")
            return await self.handle_rpc(request)
        channel = RpcServer(path,bound)
        channel.peer_pid = -1
        await channel.start()
        if self.worker_uid is not None:
            os.chown(path,self.worker_uid,-1)
        self.channels[key] = channel
        return channel

    def _handle_rpc(self, request):
        method, dev = request["method"], request.get("device")
        controller = self.controller
        if method == "snapshot":
            result = controller.snapshot(dev,request.get("control_epoch"),request.get("state_seq"),request.get("conditional",False))
            self.metrics.increment("state_sync_"+(result.get("mode","full"))+"_total")
            return result
        if method == "transact":
            return controller.transact(dev, request["operations"], request["revision"], request.get("request_id"),scope="device")
        if method == "preview":
            return controller.transact(dev, request["operations"], request["revision"], preview=True,scope="device")
        if method == "startup":
            _, state = controller.locate(dev)
            return state["startup"][dev]
        if method == "ping":
            lab_id, _ = controller.locate(dev)
            return controller.diagnose_ping(lab_id, request.get("source") or dev, request["destination"],
                request.get("vrf"), request.get("count", 5), source_device=dev)
        if method == "events":
            return controller.store.pending_events(dev, request.get("role", "query"), request.get("after", 0), request.get("limit", 1000))
        if method == "delivery":
            controller.store.delivery(request["event_id"], request["status"], {**request["payload"],"device": dev, "role": request.get("role", "query")})
            return {"status": "recorded"}
        if method == "notification_cursor":
            return controller.store.cursor(dev+":"+request.get("role", "query"), request.get("value"))
        raise SimulationError("Unknown RPC method")

    def app(self, token=None, allowed_hosts=None, allowed_origins=None):
        self.auth_enabled = bool(token)
        trusted_origins = set(allowed_origins or os.getenv("IDC_ALLOWED_ORIGINS", "").split(","))
        trusted_origins = {origin.strip() for origin in trusted_origins if origin.strip()}
        for origin in trusted_origins:
            parsed = urlsplit(origin)
            if parsed.scheme not in {"http", "https"} or not parsed.hostname or parsed.username or parsed.password or parsed.path or parsed.query or parsed.fragment:
                raise ValueError("IDC_ALLOWED_ORIGINS requires exact http(s) origins without paths")
            parsed.port
        trusted_origins = {normalize_origin(origin) for origin in trusted_origins}
        @web.middleware
        async def errors(request, handler):
            try:
                host = urlsplit("//"+request.host).hostname
                allowed = {"localhost","127.0.0.1","::1",*(h.lower() for h in allowed_hosts or [])}
                if host not in allowed:
                    return web.json_response({"error":"Unrecognized Host"},status=403)
                origin = request.headers.get("Origin")
                if origin:
                    supplied = normalize_origin(origin)
                    if supplied != normalize_origin(f"{request.scheme}://{request.host}") and supplied not in trusted_origins:
                        return web.json_response({"error":"Cross-origin requests are forbidden"},status=403)
                if request.method in {"POST","PUT","PATCH","DELETE"} and request.can_read_body and request.content_type != "application/json":
                    return web.json_response({"error":"Content-Type must be application/json"},status=415)
                if token and request.path not in {"/", "/ui/app.js", "/ui/styles.css"}:
                    supplied = request.headers.get("Authorization", "")
                    if not hmac.compare_digest(supplied.encode("utf-8"), ("Bearer "+token).encode("utf-8")):
                        return web.json_response({"error": "Unauthorized"}, status=401, headers={"WWW-Authenticate": "Bearer"})
                start = time.monotonic()
                result = await handler(request)
                self.metrics.observe("api_response_seconds", time.monotonic()-start)
                return result
            except SimulationError as exc:
                self.metrics.increment("api_errors_total")
                if isinstance(exc, Unavailable):
                    body = {"error": str(exc), "outcome": exc.outcome}
                    if exc.operation_id:
                        body["operation_id"] = exc.operation_id
                    return web.json_response(body, status=503, headers={"Retry-After": "1"})
                return web.json_response({"error": str(exc)}, status=exc.status)
            except (KeyError, TypeError, ValueError) as exc:
                return web.json_response({"error": f"Invalid request: {exc}"}, status=400)

        app = web.Application(middlewares=[errors], client_max_size=16*1024*1024)
        assets = Path(__file__).parent/"web"
        async def index(request):
            return web.FileResponse(assets/"index.html", headers={"Cache-Control":"no-cache"})
        async def asset(request):
            return web.FileResponse(assets/request.match_info["filename"], headers={"Cache-Control":"no-cache"})
        app.router.add_get("/", index)
        app.router.add_get("/ui/{filename:app.js|styles.css}", asset)
        app.router.add_get("/v1/status", self.get_status)
        app.router.add_get("/v1/templates/generated", self.generate_template)
        app.router.add_get("/v1/templates/{template}", self.get_template)
        app.router.add_post("/v1/labs", self.create_lab)
        app.router.add_post("/v1/labs/import", self.import_lab)
        app.router.add_get("/v1/labs", self.list_labs)
        app.router.add_get("/v1/labs/{lab}/topology", self.get_topology)
        app.router.add_get("/v1/labs/{lab}/config", self.get_config)
        app.router.add_post("/v1/labs/{lab}/config/preview",self.preview_config)
        app.router.add_put("/v1/labs/{lab}/config",self.replace_config)
        app.router.add_delete("/v1/labs/{lab}",self.delete_lab)
        app.router.add_get("/v1/labs/{lab}/locate", self.locate_address)
        app.router.add_post("/v1/labs/{lab}/ping", self.diagnose_ping)
        app.router.add_post("/v1/labs/{lab}/start", self.start_lab)
        app.router.add_post("/v1/labs/{lab}/stop", self.stop_lab)
        app.router.add_post("/v1/labs/{lab}/notifications", self.inject_notifications)
        app.router.add_get("/v1/devices/{device}/state", self.get_state)
        app.router.add_patch("/v1/devices/{device}/interfaces/{port}", self.patch_interface)
        app.router.add_post("/v1/devices/{device}/{operation:save|reboot|transactions}", self.operation)
        app.router.add_post("/v1/scenarios/{scenario}/run", self.run_scenario)
        app.router.add_get("/v1/operations/{operation}", self.get_operation)
        app.router.add_get("/v1/events", self.events)
        app.router.add_get("/metrics", self.get_metrics)
        return app

    async def create_lab(self, request):
        return web.json_response(await self.state_call(self.controller.create,await request.json()), status=201)

    async def import_lab(self, request):
        from idc_simulator.model import safe_load
        body = await request.json()
        raw = safe_load(body.get("content"))
        if not isinstance(raw, dict):
            raise SimulationError("Topology must be a mapping")
        return web.json_response(await self.state_call(self.controller.create,raw),status=201)

    async def get_status(self, request):
        return web.json_response({"mode":self.mode,"model_only":self.model_only,"version":__version__,
            "auth":self.auth_enabled,"retention":{"events":self.retention_rows,"operations_hours":self.operation_hours}})

    async def get_template(self, request):
        name = request.match_info["template"]
        if name == "gpu-idc":
            from idc_simulator.idc_lab import generate_gpu_idc
            return web.json_response(generate_gpu_idc())
        if name == "local":
            from idc_simulator.tools import generate_lab
            lab = generate_lab(1,4)
            lab.update(name="local-web")
            lab["management"].update(subnet="127.0.0.0/8",gateway="127.0.0.254",attachment="loopback")
            lab["devices"][0]["management_ip"] = "127.0.0.1"
            return web.json_response(lab)
        raise NotFound("Unknown topology template")

    async def generate_template(self, request):
        from idc_simulator.tools import generate_lab
        try:
            devices = int(request.query.get("devices", "10"))
            ports = int(request.query.get("ports", "24"))
        except ValueError as exc:
            raise SimulationError("devices and ports must be integers") from exc
        layout = request.query.get("layout", "chain")
        local = request.query.get("local") == "true"
        if local and devices != 1:
            raise SimulationError("Local mode runs one device per lab")
        name = request.query.get("name", "generated-lab")
        if not name or len(name) > 48 or any(char not in "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_-" for char in name):
            raise SimulationError("name must contain 1-48 letters, digits, '_' or '-'")
        try:
            lab = await asyncio.to_thread(generate_lab, devices, ports, local, layout)
        except SimulationError:
            raise
        lab["name"] = name
        identity = sha256(name.encode()).hexdigest()
        subnet_number = await self.state_call(self.available_generated_subnet, name, local)
        old_prefix = "127.20.0" if local else "10.200.0"
        new_prefix = (f"127.{16 + subnet_number // 256}.{subnet_number % 256}" if local
                      else f"10.{200 + subnet_number // 256}.{subnet_number % 256}")
        lab["management"]["subnet"] = lab["management"]["subnet"].replace(old_prefix, new_prefix, 1)
        lab["management"]["gateway"] = lab["management"]["gateway"].replace(old_prefix, new_prefix, 1)
        device_names = {device["id"]:f"g{identity[:10]}-{device['id']}" for device in lab["devices"]}
        for device in lab["devices"]:
            device["id"] = device_names[device["id"]]
            device["hostname"] = device["id"]
            device["management_ip"] = device["management_ip"].replace(old_prefix, new_prefix, 1)
        for link in lab["links"]:
            link["endpoints"] = [f"{device_names[endpoint.rsplit(':',1)[0]]}:{endpoint.rsplit(':',1)[1]}" for endpoint in link["endpoints"]]
        for host_index, host in enumerate(lab["hosts"], 1):
            old_id = host["id"]
            host["id"] = f"h{identity[:10]}-{old_id}"
            host["mac"] = generated_host_mac(identity, host_index)
            host["device"] = device_names[host["device"]]
            for flow in lab["flows"]:
                if flow["source"] == old_id:
                    flow["source"] = host["id"]
                if flow["destination"] == old_id:
                    flow["destination"] = host["id"]
        for scenario in lab.get("scenarios", {}).values():
            for step in scenario.get("steps", []):
                if step.get("device") in device_names:
                    step["device"] = device_names[step["device"]]
        return web.json_response(lab)

    @staticmethod
    def _subnet_prefix(number, local):
        return (f"127.{16 + number // 256}.{number % 256}" if local
                else f"10.{200 + number // 256}.{number % 256}")

    def available_generated_subnet(self, name, local=False, now=None):
        now = time.monotonic_ns() if now is None else now
        reservations = self._generated_reservations
        for key, (_, expires) in list(reservations.items()):
            if expires <= now:
                del reservations[key]
        key = (name, local)
        own = reservations.get(key)
        if own is not None:
            return own[0]
        start = int(sha256(name.encode()).hexdigest()[:4], 16) % 10240
        used = []
        for state in self.controller.labs.values():
            try:
                used.append(ip_network(state["lab"]["management"]["subnet"], strict=False))
            except (KeyError, ValueError):
                continue
        for (_, reserved_local), (number, _) in reservations.items():
            used.append(ip_network(self._subnet_prefix(number, reserved_local)+".0/24"))
        for offset in range(10240):
            number = (start + offset) % 10240
            candidate = ip_network(self._subnet_prefix(number, local)+".0/24")
            if not any(candidate.overlaps(network) for network in used):
                reservations[key] = (number, now + GENERATED_SUBNET_RESERVATION_NS)
                return number
        raise SimulationError("Generated topology management address pool is full")

    async def get_config(self, request):
        return web.json_response(await self.state_call(self.export_config,request.match_info["lab"]))

    async def preview_config(self,request):
        body = await request.json()
        return web.json_response(await self.state_call(self.controller.preview_lab,request.match_info["lab"],
            body["topology"],self.revision(request),body.get("save_startup",False)))

    async def close_channels(self,devices):
        for key,channel in tuple(self.channels.items()):
            if key[0] in devices:
                await channel.close()
                self.channels.pop(key,None)

    async def replace_config(self,request):
        lab = request.match_info["lab"]
        body = await request.json()
        revision = self.revision(request)
        return await self.lifecycle(self._replace_config(lab, body, revision))

    async def lifecycle(self, operation, operation_id=None):
        async def execute():
            try:
                return await operation
            except Unavailable as exc:
                exc.outcome = "unknown"
                exc.operation_id = operation_id
                raise
        task = asyncio.create_task(execute())
        self.lifecycle_tasks.add(task)
        def finished(done):
            self.lifecycle_tasks.discard(done)
            if done.cancelled():
                return
            error = done.exception()
            if error is not None:
                if isinstance(error, SimulationError):
                    logging.warning("Background lifecycle task rejected: %s", error)
                    return
                self.metrics.increment("lifecycle_task_errors_total")
                logging.error("Background lifecycle task failed: %s", error,
                              exc_info=(type(error), error, error.__traceback__))
        task.add_done_callback(finished)
        done, _ = await asyncio.wait({task}, timeout=self.lifecycle_timeout)
        if not done:
            self.metrics.increment("lifecycle_timeouts_total")
            raise Unavailable("Operation is still running; query its result or labs/config before retrying", "unknown", operation_id)
        return task.result()

    async def _replace_config(self,lab,body,revision):
        async with self.locks.setdefault(lab,asyncio.Lock()):
            def begin():
                from idc_simulator.errors import Conflict
                self.controller.preview_lab(lab, body["topology"], revision, body.get("save_startup", False))
                state = self.controller.labs[lab]
                if state["status"] != "stopped":
                    raise Conflict("Stop the lab before replacing its configuration")
                self.controller.busy_labs.add(lab)
                return set(state["lab"]["devices"])
            try:
                devices = await self.state_call(begin)
                await self.scenarios.cancel_devices(devices)
                await self.close_channels(devices)
                def commit():
                    self.controller.busy_labs.discard(lab)
                    return self.controller.replace_lab(lab, body["topology"], revision, body.get("save_startup", False))
                result = await self.state_call(commit)
                return web.json_response(result)
            finally:
                self.controller.busy_labs.discard(lab)

    async def delete_lab(self,request):
        return await self.lifecycle(self._delete_lab(request))

    async def _delete_lab(self,request):
        lab = request.match_info["lab"]
        revision = self.revision(request)
        async with self.locks.setdefault(lab,asyncio.Lock()):
            begun = False
            def begin():
                state = self.controller.labs.get(lab)
                if state is None:
                    raise NotFound("Unknown lab")
                if state["revision"] != revision:
                    from idc_simulator.errors import Conflict
                    raise Conflict("Lab revision changed")
                self.controller.busy_labs.add(lab)
                return set(state["lab"]["devices"])
            try:
                devices = await self.state_call(begin)
                begun = True
                await self.scenarios.cancel_devices(devices)
                await self.supervisor.stop(lab)
                await self.close_channels(devices)
                return web.json_response(await self.state_call(self.controller.delete_lab,lab,revision))
            except Exception:
                def degraded():
                    if begun and lab in self.controller.labs:
                        self.controller.lab_status(lab,"degraded")
                await self.state_call(degraded)
                raise
            finally:
                self.controller.busy_labs.discard(lab)

    def export_config(self,lab):
        from idc_simulator.lifecycle import export_lab
        if lab not in self.controller.labs:
            raise NotFound("Unknown lab")
        return export_lab(self.controller.labs[lab]["lab"])

    async def list_labs(self, request):
        return web.json_response(await self.state_call(lambda:[{ "id": lab, "status": state["status"], "revision": state["revision"], "devices": list(state["lab"]["devices"])} for lab, state in self.controller.labs.items()]))

    async def get_topology(self, request):
        return web.json_response(await self.state_call(self.controller.topology,request.match_info["lab"]))

    async def locate_address(self, request):
        return web.json_response(await self.state_call(self.controller.locate_address,request.match_info["lab"],
            **{key: request.query[key] for key in ("ip", "mac", "vrf", "vlan", "device", "port") if key in request.query}))

    async def diagnose_ping(self, request):
        body = await request.json()
        return web.json_response(await self.state_call(self.controller.diagnose_ping,request.match_info["lab"], body["source"], body["destination"],
            body.get("vrf"), body.get("count", 5)))

    async def start_lab(self, request):
        if self.model_only:
            raise SimulationError("Model-only mode: SNMP/SSH workers are disabled")
        return await self.lifecycle(self._start_lab(request))

    async def _start_lab(self, request):
        lab = request.match_info["lab"]
        async with self.locks.setdefault(lab, asyncio.Lock()):
            return web.json_response(await self.supervisor.start(lab))

    async def stop_lab(self, request):
        return await self.lifecycle(self._stop_lab(request))

    async def _stop_lab(self, request):
        lab = request.match_info["lab"]
        async with self.locks.setdefault(lab, asyncio.Lock()):
            return web.json_response(await self.supervisor.stop(lab))

    async def inject_notifications(self, request):
        body = await request.json()
        return web.json_response(await self.state_call(self.controller.inject_notifications,request.match_info["lab"], body["devices"], body.get("count", 1), body.get("type", "coldStart"), body.get("payload")))

    async def get_state(self, request):
        state = await self.state_call(self.controller.snapshot,request.match_info["device"])
        return web.json_response(state, headers={"ETag": f'"{state["revision"]}"'})

    @staticmethod
    def revision(request):
        if "If-Match" not in request.headers:
            raise SimulationError("If-Match revision required")
        return int(request.headers["If-Match"].strip('"'))

    async def patch_interface(self, request):
        result = await self.device_transaction(request, [{"kind": "interface", "port": request.match_info["port"], "fields": await request.json()}])
        return web.json_response(result)

    async def operation(self, request):
        kind = request.match_info["operation"]
        operations = (await request.json())["operations"] if kind == "transactions" else [{"kind": kind}]
        result = await self.device_transaction(request, operations)
        return web.json_response(result)

    async def device_transaction(self, request, operations):
        operation_id = request.headers.get("Idempotency-Key") or str(uuid4())
        try:
            return await self.state_call(self.controller.transact,request.match_info["device"],operations,self.revision(request),operation_id)
        except Unavailable as exc:
            exc.operation_id = operation_id
            raise

    async def run_scenario(self, request):
        body = await request.json()
        spec = body.get("spec")
        name = request.match_info["scenario"]
        if spec is None:
            def read_spec():
                state = self.controller.labs.get(body["lab"])
                if state is None or name not in state["lab"]["scenarios"]:
                    raise NotFound("Unknown scenario")
                from copy import deepcopy
                return deepcopy(state["lab"]["scenarios"][name])
            spec = await self.state_call(read_spec)
        operation_id = request.headers.get("Idempotency-Key") or str(uuid4())
        return web.json_response(await self.lifecycle(self.scenarios.start_async(name,spec,operation_id), operation_id),status=202)

    async def get_operation(self, request):
        key = request.match_info["operation"]
        run = self.scenarios.runs.get(key)
        if run is None:
            run = await self.state_call(self.controller.store.operation_by_id,key)
        if run is None:
            raise NotFound("Unknown operation")
        return web.json_response(run)

    async def events(self, request):
        def read():
            return {"events":self.controller.store.events(int(request.query.get("after",0)),int(request.query.get("limit",1000))),"deliveries":self.controller.store.deliveries()}
        return web.json_response(await self.state_call(read))

    async def get_metrics(self, request):
        samples = []
        for file in self.directory.glob("*/*/metrics-*.prom"):
            dev = file.parent.name
            role = file.stem.removeprefix("metrics-")
            try:
                with file.open("r", encoding="utf-8", errors="replace") as source:
                    content = source.read(1024*1024+1)
                age = max(0,time.time()-file.stat().st_mtime)
            except OSError:
                continue
            if role not in {"query", "notification"}:
                self.metrics.increment("worker_metrics_invalid_lines_total", device=dev, role=role)
                continue
            samples.append(f'worker_metrics_age_seconds{{device={json.dumps(dev)},role={json.dumps(role)}}} {age}\n')
            if len(content) > 1024*1024:
                self.metrics.increment("worker_metrics_invalid_lines_total", device=dev, role=role)
                continue
            for line in content.splitlines():
                sample = worker_sample(line, dev, role)
                if sample is None:
                    self.metrics.increment("worker_metrics_invalid_lines_total", device=dev, role=role)
                else:
                    samples.append(sample)
        text = self.metrics.render()+''.join(samples)
        return web.Response(text=text, content_type="text/plain")

    async def close(self):
        deadline = time.monotonic()+self.shutdown_timeout
        def remaining():
            return max(1.0, deadline-time.monotonic())
        if self.lifecycle_tasks:
            pending_tasks = set(self.lifecycle_tasks)
            _, pending = await asyncio.wait(pending_tasks, timeout=remaining())
            if pending:
                self.metrics.increment("shutdown_timeout_total")
                logging.warning("Shutdown budget reached with %d lifecycle task(s) running; cancelling them", len(pending))
                for task in pending:
                    task.cancel()
                await asyncio.wait(pending, timeout=5)
        if self.maintenance:
            self.maintenance.cancel()
            await asyncio.gather(self.maintenance, return_exceptions=True)
            self.maintenance = None
        try:
            await asyncio.wait_for(self.scenarios.close(), remaining())
        except asyncio.TimeoutError:
            self.metrics.increment("shutdown_timeout_total")
            logging.warning("Scenario cleanup exceeded the shutdown budget")
        await self.supervisor.close(budget=remaining())
        for channel in self.channels.values():
            await channel.close()
        await self.rpc.close()
        if self.temporary_socket_directory:
            shutil.rmtree(self.temporary_socket_directory)
        self.state_timeout_function = None
        await self.state_call(self.controller.checkpoint, _state_budget=remaining())
        await self.state_call(self.controller.store.close, _state_budget=remaining())
        try:
            await asyncio.wait_for(asyncio.to_thread(self.executor.shutdown, True), remaining())
        except asyncio.TimeoutError:
            self.metrics.increment("shutdown_timeout_total")
            logging.warning("State executor did not stop within the shutdown budget")
        if self.owner_lock is not None:
            os.close(self.owner_lock)
            self.owner_lock = None
        self.started = False


def install_stop_handlers(stop):
    loop = asyncio.get_running_loop()
    delivered = []
    for sig in (signal.SIGTERM, signal.SIGINT):
        try:
            loop.add_signal_handler(sig, stop.set)
            delivered.append(sig)
        except (NotImplementedError, RuntimeError, AttributeError, ValueError):
            continue
    for sig in (signal.SIGTERM, signal.SIGINT):
        if sig in delivered:
            continue
        try:
            signal.signal(sig, lambda *_: loop.call_soon_threadsafe(stop.set))
        except (ValueError, OSError, RuntimeError, AttributeError):
            continue
    return delivered


async def serve(args):
    token = os.getenv("IDC_API_TOKEN")
    prevent_unauthenticated_bind(args.bind, token, getattr(args, "allow_unauthenticated", False))
    runtime = Runtime(args.state_dir, args.mode, args.snmp_port, args.ssh_port, worker_uid=args.worker_uid, model_only=getattr(args,"model_only",False))
    if args.topology:
        from idc_simulator.model import safe_load
        raw = safe_load(Path(args.topology).read_text())
        if raw["name"] not in runtime.controller.labs:
            runtime.controller.create(raw)
        else:
            from idc_simulator.lifecycle import comparable
            from idc_simulator.model import normalize_lab
            supplied = normalize_lab(raw)
            existing = runtime.controller.labs[raw["name"]]["lab"]
            if comparable(supplied) != comparable(existing):
                print("Topology file differs from persisted configuration; using persisted running/startup. "
                      "Use POST /v1/labs/{lab}/config/preview and PUT /v1/labs/{lab}/config to apply changes.",flush=True)
            else:
                print("Topology file matches persisted configuration; using stored state.",flush=True)
    hosts = configured_hosts(args.bind, os.getenv("IDC_ALLOWED_HOSTS",""))
    runner = web.AppRunner(runtime.app(token,allowed_hosts=hosts))
    allowed = ", ".join(["localhost","127.0.0.1","::1",*hosts])
    print(f"Host allowlist: {allowed}", flush=True)
    if str(args.bind).strip().lower() in WILDCARD_BINDS and not hosts:
        print("⚠️  绑定通配地址但未设置 IDC_ALLOWED_HOSTS：来自非本机 Host 的请求将全部 403。"
              "需要远程访问时显式设置（逗号分隔，不含端口），例如 IDC_ALLOWED_HOSTS=sim.example,10.0.0.5", flush=True)
    stop = asyncio.Event()
    install_stop_handlers(stop)
    try:
        await runtime.start()
        await runner.setup()
        await web.TCPSite(runner, args.bind, args.api_port).start()
        if args.topology and not runtime.model_only:
            try:
                await runtime.supervisor.start(raw["name"])
            except Exception:
                await runtime.state_call(runtime.controller.lab_status,raw["name"],"degraded")
                logging.exception("Initial lab failed to start; control API remains available")
        print(f"Web / Control API http://{args.bind}:{args.api_port}; state {runtime.directory}", flush=True)
        await stop.wait()
    finally:
        await runner.cleanup(); await runtime.close()
