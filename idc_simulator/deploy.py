"""Linux resource plans use argv, lab-scoped names and reversible ownership."""
from hashlib import sha256
from ipaddress import ip_network, ip_address
import asyncio
from contextlib import suppress
import logging
import os
from pathlib import Path
import sys
import json
import signal
import shlex
from idc_simulator.errors import SimulationError, Conflict


def write_ready(path, payload):
    temporary = path.with_name(path.name+".tmp")
    temporary.write_text(json.dumps(payload))
    os.replace(temporary, path)


def read_ready(path):
    try:
        return json.loads(Path(path).read_text())
    except (FileNotFoundError, NotADirectoryError, json.JSONDecodeError, OSError):
        return {}


def read_owner(path):
    if not Path(path).exists():
        return {}
    payload = read_ready(path)
    devices = payload.get("devices") if isinstance(payload, dict) else None
    if not isinstance(devices, list) or not devices or not all(isinstance(dev, str) and dev for dev in devices):
        raise Conflict(f"Network ownership record is unreadable; inspect it before retrying: {path}")
    return payload


def names(lab_id, dev=None, naming_version=1):
    prefix = "ids"+sha256(lab_id.encode()).hexdigest()[:7]
    suffix = sha256((dev or "").encode()).hexdigest()[:8]
    legacy_suffix = suffix[:5]
    return {"bridge": prefix, "namespace": prefix+"-"+(legacy_suffix if naming_version == 1 else suffix), "host_veth": "id"+prefix[3:8]+suffix, "peer": "mgmt0"}


def naming_version(lab):
    legacy = [names(lab["name"], dev)["namespace"] for dev in lab["devices"]]
    return 1 if len(legacy) == len(set(legacy)) else 2


def network_plan(lab, remove=False, version=None):
    version = naming_version(lab) if version is None else version
    for field in ("namespace","host_veth"):
        values = [names(lab["name"],dev,version)[field] for dev in lab["devices"]]
        if len(values) != len(set(values)):
            raise SimulationError(f"Deployment {field} name collision; rename a device before starting")
    net = ip_network(lab["management"]["subnet"])
    bridge = names(lab["name"])["bridge"]
    if remove:
        cmds = [["ip", "netns", "delete", names(lab["name"], dev, version)["namespace"]] for dev in lab["devices"]]
        return cmds+[["ip", "link", "delete", bridge]]
    commands = [["ip", "link", "add", bridge, "type", "bridge"],
                ["ip", "addr", "add", f"{lab['management']['gateway']}/{net.prefixlen}", "dev", bridge],
                ["ip", "link", "set", bridge, "up"]]
    for dev, d in lab["devices"].items():
        n = names(lab["name"], dev, version)
        commands.extend([
            ["ip", "netns", "add", n["namespace"]],
            ["ip", "link", "add", n["host_veth"], "type", "veth", "peer", "name", n["peer"], "netns", n["namespace"]],
            ["ip", "link", "set", n["host_veth"], "master", bridge],
            ["ip", "link", "set", n["host_veth"], "up"],
            ["ip", "-n", n["namespace"], "link", "set", "lo", "up"],
            ["ip", "-n", n["namespace"], "addr", "add", f"{d['management_ip']}/{net.prefixlen}", "dev", n["peer"]],
            ["ip", "-n", n["namespace"], "link", "set", n["peer"], "up"],
            ["ip", "-n", n["namespace"], "route", "add", "default", "via", lab["management"]["gateway"]]])
    return commands


async def run_command(argv, tolerate=False, timeout=30):
    process = await asyncio.create_subprocess_exec(*argv, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
    try:
        output, error = await asyncio.wait_for(process.communicate(), timeout)
    except asyncio.TimeoutError:
        with suppress(ProcessLookupError):
            process.kill()
        with suppress(Exception):
            await process.wait()
        if not tolerate:
            raise SimulationError(f"Deployment command timed out after {timeout:g}s: {' '.join(argv)}") from None
        return 124, ""
    if process.returncode and not tolerate:
        raise SimulationError(f"Deployment command failed: {' '.join(argv)}: {error.decode()}")
    return process.returncode, output.decode()


class Supervisor:
    def __init__(self, controller, directory, socket, credentials, mode="local", snmp_port=1161, ssh_port=8022, worker_uid=None, state_call=None, channel_factory=None):
        self.controller, self.directory, self.socket = controller, Path(directory), socket
        self.credentials, self.mode = credentials, mode
        self.snmp_port, self.ssh_port = snmp_port, ssh_port
        self.worker_uid = worker_uid
        self.processes = {}
        self.monitors = {}
        self.stopping = set()
        self.recovering = set()
        self.state_call, self.channel_factory = state_call, channel_factory

    async def call(self, function, *args, **kwargs):
        if self.state_call:
            return await self.state_call(function,*args,**kwargs)
        return function(*args,**kwargs)

    async def start(self, lab_id):
        if lab_id in self.processes:
            return await self.call(self.controller.lab_status,lab_id, self.controller.labs[lab_id]["status"])
        state = self.controller.labs.get(lab_id)
        if not state:
            raise SimulationError("Unknown lab")
        lab = state["lab"]
        network_plan(lab)
        if self.mode == "namespace" and (sys.platform != "linux" or os.geteuid() != 0):
            raise SimulationError("Namespace deployment requires Linux root deployment service")
        if self.mode == "local":
            if any(not ip_address(d["management_ip"]).is_loopback for d in lab["devices"].values()):
                raise SimulationError("Local mode requires assigned loopback IPs; on macOS use one 127.0.0.1 device per lab")
            if len(lab["devices"]) != 1:
                raise SimulationError(f"Local mode runs one device per lab: {lab_id} has {len(lab['devices'])}. Use --mode namespace on Linux, or split the topology into one-device labs.")
        self.processes[lab_id] = {}
        self.stopping.discard(lab_id)
        owner = self.directory/lab_id/"network-owner.json"
        owner.parent.mkdir(parents=True, exist_ok=True)
        if self.worker_uid is not None:
            os.chmod(owner.parent, 0o711)
        try:
            await self.recover_orphans(lab_id)
            if self.mode == "namespace":
                recorded = read_owner(owner)
                if recorded:
                    owned = {**lab, "devices": dict.fromkeys(recorded["devices"])}
                    for argv in network_plan(owned, remove=True, version=recorded.get("naming_version", 1)):
                        await run_command(argv, tolerate=True)
                else:
                    code, _ = await run_command(["ip", "link", "show", names(lab_id)["bridge"]], tolerate=True)
                    _, namespaces = await run_command(["ip", "netns", "list"])
                    if code == 0 or any(names(lab_id, dev, naming_version(lab))["namespace"] in namespaces.split() for dev in lab["devices"]):
                        raise SimulationError("Network resource name exists without this lab's ownership record")
                write_ready(owner, {"lab_id": lab_id, "devices": list(lab["devices"]), "naming_version": naming_version(lab)})
                for argv in network_plan(lab):
                    await run_command(argv)
            for dev in lab["devices"]:
                await self.spawn(lab_id, dev)
                if any(t.get("version", "v2c") == "v2c" for t in self.device_credentials(lab, dev).get("notifications", [])):
                    await self.spawn(lab_id, dev, "notification")
            return await self.call(self.controller.lab_status,lab_id, "running")
        except Exception:
            try:
                await self.stop(lab_id)
            except Exception:
                logging.exception("Cleanup after failed start of lab %s also failed", lab_id)
            raise

    async def spawn(self, lab_id, dev, role="query"):
        state = self.controller.labs[lab_id]
        if role == "query":
            await self.call(self.controller.transact,dev, [{"kind": "worker_started"}], state["revision"])
        device_dir = self.directory/lab_id/dev
        device_dir.mkdir(parents=True, exist_ok=True)
        ready = device_dir/("ready.json" if role == "query" else "ready-notification.json")
        ready.unlink(missing_ok=True)
        channel = await self.channel_factory(dev,role) if self.channel_factory else None
        worker_socket = channel.path if channel else self.socket
        argv = [sys.executable, "-m", "idc_simulator", "worker", "--device", dev, "--socket", str(worker_socket),
                "--state-dir", str(device_dir), "--snmp-port", str(161 if self.mode == "namespace" else self.snmp_port),
                "--ssh-port", str(22 if self.mode == "namespace" else self.ssh_port), "--role", role]
        if self.worker_uid is not None:
            argv.extend(("--uid", str(self.worker_uid)))
        if self.mode == "namespace":
            owner = read_owner(self.directory/lab_id/"network-owner.json")
            argv = ["ip", "netns", "exec", names(lab_id, dev, owner.get("naming_version", 1))["namespace"]]+argv
        env = {key:os.environ[key] for key in ("PATH","LANG","LC_ALL","LC_CTYPE","PYTHONPATH","VIRTUAL_ENV","TMPDIR") if key in os.environ}
        env["IDC_SIM_CREDENTIALS"] = json.dumps(self.device_credentials(state["lab"], dev))
        logfile = device_dir/f"{role}.log"
        if logfile.exists() and logfile.stat().st_size > 10*1024*1024:
            logfile.replace(device_dir/f"{role}.log.1")
        log = open(logfile, "ab")
        process = await asyncio.create_subprocess_exec(*argv, env=env, stdout=log, stderr=log)
        if channel:
            channel.peer_pid = process.pid
        log.close()
        key = dev if role == "query" else dev+":notification"
        self.processes[lab_id][key] = process
        deadline = asyncio.get_running_loop().time()+30
        while asyncio.get_running_loop().time() < deadline:
            if process.returncode is not None:
                raise SimulationError(f"Worker {dev} exited; see {device_dir/(role+'.log')}")
            if read_ready(ready).get("pid") == process.pid:
                self.monitors[(lab_id, key)] = asyncio.create_task(self.monitor(lab_id, dev, process, role))
                return
            await asyncio.sleep(.05)
        raise SimulationError(f"Worker startup timeout: {dev}")

    async def monitor(self, lab_id, dev, process, role):
        await process.wait()
        key = dev if role == "query" else dev+":notification"
        recovery = (lab_id,key)
        if lab_id in self.stopping:
            return
        self.recovering.add(recovery)
        await self.call(self.controller.lab_status,lab_id,"degraded")
        delay = 1
        while lab_id not in self.stopping:
            await asyncio.sleep(delay)
            if lab_id in self.stopping:
                return
            try:
                await self.spawn(lab_id, dev, role)
                self.recovering.discard(recovery)
                if lab_id not in self.stopping:
                    await self.call(self.controller.lab_status,lab_id,"degraded" if any(lab==lab_id for lab,_ in self.recovering) else "running")
                return
            except Exception:
                await self.call(self.controller.lab_status,lab_id, "degraded")
                failed = self.processes.get(lab_id,{}).get(key)
                if failed and failed.returncode is None:
                    failed.kill(); await failed.wait()
                delay = min(delay*2,30)

    async def stop(self, lab_id):
        self.stopping.add(lab_id)
        self.recovering = {item for item in self.recovering if item[0] != lab_id}
        for (lab, dev), task in tuple(self.monitors.items()):
            if lab == lab_id:
                task.cancel(); self.monitors.pop((lab, dev))
        for process in self.processes.get(lab_id, {}).values():
            if process.returncode is None:
                process.terminate()
                try:
                    await asyncio.wait_for(process.wait(), 5)
                except asyncio.TimeoutError:
                    process.kill(); await process.wait()
        self.processes.pop(lab_id, None)
        for ready in (self.directory/lab_id).glob("*/ready*.json"):
            ready.unlink(missing_ok=True)
        for metrics in (self.directory/lab_id).glob("*/metrics-*.*"):
            metrics.unlink(missing_ok=True)
        owner = self.directory/lab_id/"network-owner.json"
        if self.mode == "namespace":
            recorded = read_owner(owner)
            if recorded:
                try:
                    lab = {**self.controller.labs[lab_id]["lab"], "devices": dict.fromkeys(recorded["devices"])}
                    version = recorded.get("naming_version", 1)
                    for argv in network_plan(lab, remove=True, version=version):
                        await run_command(argv, tolerate=True)
                    code, _ = await run_command(["ip", "link", "show", names(lab_id)["bridge"]], tolerate=True)
                    _, namespaces = await run_command(["ip", "netns", "list"])
                    if code == 0 or any(names(lab_id, dev, version)["namespace"] in namespaces.split() for dev in lab["devices"]):
                        raise SimulationError("Network resources remain; ownership retained for cleanup retry")
                    owner.unlink()
                except Exception:
                    await self.call(self.controller.lab_status, lab_id, "degraded")
                    raise
        return await self.call(self.controller.lab_status,lab_id, "stopped")

    async def close(self, budget=None):
        started = asyncio.get_running_loop().time()
        for lab in tuple(self.processes):
            try:
                if budget is None:
                    await self.stop(lab)
                    continue
                remaining = budget-(asyncio.get_running_loop().time()-started)
                if remaining <= 0:
                    logging.warning("Shutdown budget exhausted before lab %s stopped; killing its workers", lab)
                    await self.kill(lab)
                    continue
                await asyncio.wait_for(self.stop(lab), remaining)
            except asyncio.TimeoutError:
                logging.warning("Stopping lab %s exceeded the shutdown budget; killing its workers", lab)
                await self.kill(lab)
            except Exception:
                logging.exception("Stopping lab %s failed during shutdown", lab)
                await self.kill(lab)

    async def kill(self, lab_id):
        for process in self.processes.get(lab_id, {}).values():
            if process.returncode is None:
                with suppress(ProcessLookupError):
                    process.kill()
                with suppress(Exception):
                    await process.wait()
        self.processes.pop(lab_id, None)

    def device_credentials(self, lab, dev):
        credentials = {k: v for k, v in self.credentials.items() if k != "credential_sets"}
        for protocol in ("snmp", "ssh"):
            settings = {**lab.get("defaults", {}).get(protocol, {}), **lab["devices"][dev].get(protocol, {})}
            reference = settings.get("credentials_ref")
            if reference:
                if reference not in self.credentials.get("credential_sets", {}):
                    raise SimulationError(f"Credential reference not found: {reference}")
                credentials.update(self.credentials["credential_sets"][reference])
            if protocol == "snmp" and "versions" in settings:
                credentials["versions"] = settings["versions"]
        return credentials

    async def recover_orphans(self, lab_id):
        for ready in (self.directory/lab_id).glob("*/ready*.json"):
            payload = read_ready(ready)
            try:
                pid = int(payload["pid"])
                if pid <= 1:
                    continue
                if sys.platform == "linux":
                    argv = Path(f"/proc/{pid}/cmdline").read_bytes().decode().split("\0")
                else:
                    _, output = await run_command(["ps", "-p", str(pid), "-o", "command="], tolerate=True)
                    argv = shlex.split(output)
                if "idc_simulator" in argv and "worker" in argv and str(ready.parent) in argv and "--socket" in argv:
                    os.kill(pid, signal.SIGTERM)
                    for _ in range(100):
                        try:
                            os.kill(pid, 0)
                        except ProcessLookupError:
                            break
                        await asyncio.sleep(.05)
                    else:
                        raise SimulationError(f"Owned orphan worker {pid} did not stop")
            except (ProcessLookupError, FileNotFoundError, KeyError, ValueError):
                pass
            ready.unlink(missing_ok=True)
