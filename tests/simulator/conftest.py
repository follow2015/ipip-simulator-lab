from pathlib import Path
import pytest
import yaml
from idc_simulator.control import Controller

ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture
def raw_lab():
    return yaml.safe_load((ROOT/"examples/local.yaml").read_text())


class Clock:
    now = 1_000_000_000
    def __call__(self):
        return self.now
    def advance(self, seconds):
        self.now += int(seconds*1_000_000_000)


@pytest.fixture
def clock():
    return Clock()


@pytest.fixture
def controller(tmp_path, raw_lab, clock):
    core = Controller(tmp_path/"state.db", clock=clock)
    core.create(raw_lab)
    yield core
    core.store.close()


class LocalClient:
    def __init__(self, core, dev="sw-a"):
        self.core, self.dev = core, dev
        self.current = core.snapshot(dev)
    async def refresh(self):
        self.current = self.core.snapshot(self.dev)
        return self.current
    async def transact(self, operations, revision=None, request_id=None):
        result = self.core.transact(self.dev, operations, revision if revision is not None else self.current["revision"], request_id)
        await self.refresh()
        return result
    async def call(self, method, **params):
        if method == "startup":
            return self.core.locate(self.dev)[1]["startup"][self.dev]
        if method == "preview":
            return self.core.transact(self.dev, params["operations"], params["revision"], preview=True)
        raise ValueError(method)
