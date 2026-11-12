"""Versioned, explicit profiles. Vendor private OIDs require evidence."""
from pathlib import Path
from functools import lru_cache
import re
import yaml
from idc_simulator.errors import SimulationError

ROOT = Path(__file__).parent
FAMILIES = {"ios", "nxos", "vrp", "comware", "eos", "junos", "fortios", "linux", "transport"}


@lru_cache(maxsize=256)
def load_profile(name: str) -> dict:
    if not re.fullmatch(r"[a-z0-9-]+/[a-z0-9-]+/[a-z0-9-]+", name):
        raise SimulationError(f"Invalid profile reference: {name}")
    path = ROOT / name
    if not path.is_dir():
        raise SimulationError(f"Unknown profile: {name}")
    result = {k: yaml.safe_load((path / f"{k}.yaml").read_text()) for k in
              ("metadata", "mib", "cli", "traps", "defaults", "compatibility")}
    meta = result["metadata"]
    if not all(meta.get(k) for k in ("brand", "series", "model", "version", "source", "sys_object_id")):
        raise SimulationError(f"Incomplete metadata: {name}")
    if result["cli"]["family"] not in FAMILIES:
        raise SimulationError(f"Unsupported CLI family: {name}")
    seen = set()
    for entry in result["mib"].get("private", []):
        oid = tuple(int(x) for x in entry["oid"].split("."))
        if oid in seen or entry["type"] not in {"Integer32", "Gauge32", "OctetString", "Counter64"} or not entry.get("source"):
            raise SimulationError(f"Invalid/private OID without provenance: {name}")
        seen.add(oid)
    result["id"] = name
    if result["compatibility"].get("collector_verified") and not (path/"fixtures").is_dir():
        raise SimulationError(f"Verified profile requires collector fixtures: {name}")
    return result


def list_profiles() -> list[dict]:
    return [load_profile(str(p.parent.relative_to(ROOT))) for p in sorted(ROOT.glob("*/*/*/metadata.yaml"))]

