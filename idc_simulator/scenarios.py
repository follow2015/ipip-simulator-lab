"""Versioned reproducible schedules, independent of polling frequency."""
import asyncio
import random
import time
import json
from hashlib import sha256
from idc_simulator.errors import Conflict
from idc_simulator.errors import SimulationError


class ScenarioRunner:
    def __init__(self, controller, state_call=None):
        self.controller = controller
        self.tasks = {}
        self.runs = controller.store.load_runs()
        self.state_call = state_call
        for run in self.runs.values():
            if run["status"] == "running":
                run["status"] = "interrupted"
                controller.store.save_run(run)

    def start(self, name, spec, request_id=None):
        run = self.prepare(name,spec,request_id)
        self.schedule(run)
        return run

    async def start_async(self,name,spec,request_id=None):
        run = await self.state_call(self.prepare,name,spec,request_id)
        self.schedule(run)
        return run

    def schedule(self,run):
        run_id = run["operation_id"]
        if run_id not in self.tasks and run["status"] == "running":
            self.tasks[run_id] = asyncio.create_task(self.execute(run,run["spec"].get("steps",[])))

    def prepare(self, name, spec, request_id=None):
        run_id = request_id or f"{name}-{time.monotonic_ns()}"
        if run_id in self.runs:
            if self.runs[run_id].get("fingerprint") != sha256(json.dumps([name,spec],sort_keys=True).encode()).hexdigest():
                raise Conflict("Idempotency-Key reused with a different scenario")
            return self.runs[run_id]
        if spec.get("schema_version") != 1:
            raise SimulationError("Scenario schema_version must be 1")
        steps = spec.get("steps", [])
        for step in steps:
            if step.get("at", 0) < 0 or not step.get("operations"):
                raise SimulationError("Invalid scenario step")
            lab_id, _ = self.controller.locate(step["device"])
            if lab_id in self.controller.busy_labs:
                raise Conflict("Lab lifecycle operation is in progress")
        run = {"operation_id": run_id, "scenario": name, "status": "running", "seed": spec.get("seed", 1), "spec": spec}
        run["fingerprint"] = sha256(json.dumps([name,spec],sort_keys=True).encode()).hexdigest()
        self.runs[run_id] = run
        self.controller.store.save_run(run)
        return run

    async def execute(self, run, steps):
        rng = random.Random(run["seed"])
        start = time.monotonic()
        try:
            for step in sorted(steps, key=lambda s: s.get("at", 0)):
                delay = max(0, step.get("at", 0)+rng.uniform(0, step.get("jitter", 0))-(time.monotonic()-start))
                await asyncio.sleep(delay)
                for repeat in range(step.get("repeat", 1)):
                    def transact():
                        _, state = self.controller.locate(step["device"])
                        return self.controller.transact(step["device"], step["operations"], state["revision"])
                    if self.state_call:
                        await self.state_call(transact)
                    else:
                        transact()
                    if repeat+1 < step.get("repeat", 1):
                        await asyncio.sleep(step.get("interval", 0))
            run["status"] = "complete"
        except asyncio.CancelledError:
            run["status"] = "cancelled"
            raise
        except Exception as exc:
            run["status"] = "failed"; run["error"] = str(exc)
        finally:
            if self.state_call:
                await self.state_call(self.controller.store.save_run,run)
            else:
                self.controller.store.save_run(run)

    async def close(self):
        for task in self.tasks.values():
            task.cancel()
        await asyncio.gather(*self.tasks.values(), return_exceptions=True)

    async def cancel_devices(self, devices):
        tasks = [task for key,task in self.tasks.items() if not task.done() and
                 any(step["device"] in devices for step in self.runs[key]["spec"].get("steps",[]))]
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks,return_exceptions=True)
