"""Single writer SQLite: state, revisions, event outbox committed together."""
import json
from contextlib import contextmanager
import sqlite3
import time
import os
from pathlib import Path

SCHEMA_VERSION = 2

SCHEMA = """
  CREATE TABLE IF NOT EXISTS lab_meta (id TEXT PRIMARY KEY, state TEXT NOT NULL);
  CREATE TABLE IF NOT EXISTS lab_state (id TEXT PRIMARY KEY, revision INTEGER NOT NULL,
    status TEXT NOT NULL, boot_epoch INTEGER NOT NULL DEFAULT 0, derived_ns INTEGER NOT NULL DEFAULT 0);
  CREATE TABLE IF NOT EXISTS lab_device (lab_id TEXT NOT NULL, dev TEXT NOT NULL,
    config TEXT NOT NULL, startup TEXT NOT NULL, PRIMARY KEY(lab_id, dev));
  CREATE TABLE IF NOT EXISTS lab_runtime (lab_id TEXT NOT NULL, dev TEXT NOT NULL,
    state TEXT NOT NULL, PRIMARY KEY(lab_id, dev));
  CREATE TABLE IF NOT EXISTS events (id INTEGER PRIMARY KEY AUTOINCREMENT,
    lab_id TEXT NOT NULL, device_id TEXT, type TEXT NOT NULL,
    revision INTEGER NOT NULL, time_ns INTEGER NOT NULL, payload TEXT NOT NULL);
  CREATE TABLE IF NOT EXISTS deliveries (id INTEGER PRIMARY KEY AUTOINCREMENT,
    event_id INTEGER NOT NULL, status TEXT NOT NULL, payload TEXT NOT NULL,
    device TEXT, role TEXT, time_ns INTEGER NOT NULL DEFAULT 0);
  CREATE TABLE IF NOT EXISTS operations (id TEXT PRIMARY KEY, result TEXT NOT NULL,
    operation_id TEXT, created_ns INTEGER NOT NULL DEFAULT 0);
  CREATE TABLE IF NOT EXISTS cursors (id TEXT PRIMARY KEY, value INTEGER NOT NULL);
  CREATE TABLE IF NOT EXISTS runs (id TEXT PRIMARY KEY, result TEXT NOT NULL);
"""

INDEXES = (
    "CREATE INDEX IF NOT EXISTS deliveries_lookup ON deliveries(device, role, status, event_id)",
    "CREATE INDEX IF NOT EXISTS deliveries_event ON deliveries(event_id)",
    "CREATE INDEX IF NOT EXISTS operations_by_operation ON operations(operation_id)",
    "CREATE INDEX IF NOT EXISTS events_device ON events(device_id, id)",
)

STATE_KEYS = ("revision", "status", "host_boot_epoch", "derived_ns")

DERIVED_RUNTIME = ("lldp_cache", "fdb_cache", "arp_cache", "oper", "last_change", "lldp_since")


class Store:
    def __init__(self, path):
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        path = Path(path)
        descriptor = os.open(path,os.O_CREAT|os.O_RDWR,0o600)
        os.close(descriptor)
        os.chmod(path,0o600)
        self.db = sqlite3.connect(path,check_same_thread=False)
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("PRAGMA synchronous=FULL")
        self.db.executescript(SCHEMA)
        self._meta_cache = {}
        self._migrate()
        for suffix in ("","-wal","-shm"):
            file = Path(str(path)+suffix)
            if file.exists():
                os.chmod(file,0o600)

    def _columns(self, table):
        return {row[1] for row in self.db.execute(f"PRAGMA table_info({table})")}

    def _has_table(self, table):
        return self.db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)).fetchone() is not None

    def _migrate(self):
        legacy = self.db.execute("SELECT id,state FROM labs").fetchall() if self._has_table("labs") else []
        version = self.db.execute("PRAGMA user_version").fetchone()[0]
        with self.db:
            for lab_id, blob in legacy:
                self._write_state(lab_id, json.loads(blob), None, None)
            if legacy:
                self.db.execute("DROP TABLE labs")
            if version < 2 or legacy:
                self._upgrade_columns()
                self.db.execute(f"PRAGMA user_version={SCHEMA_VERSION}")
            for statement in INDEXES:
                self.db.execute(statement)
            if "fingerprint" not in self._columns("operations"):
                self.db.execute("ALTER TABLE operations ADD COLUMN fingerprint TEXT")

    def _upgrade_columns(self):
        deliveries = self._columns("deliveries")
        if "device" not in deliveries:
            self.db.execute("ALTER TABLE deliveries ADD COLUMN device TEXT")
            self.db.execute("ALTER TABLE deliveries ADD COLUMN role TEXT")
            self.db.execute("ALTER TABLE deliveries ADD COLUMN time_ns INTEGER NOT NULL DEFAULT 0")
            rows = self.db.execute("SELECT id,payload FROM deliveries").fetchall()
            for row_id, payload in rows:
                try:
                    body = json.loads(payload)
                except ValueError:
                    continue
                self.db.execute("UPDATE deliveries SET device=?, role=? WHERE id=?",
                    (body.get("device"), body.get("role"), row_id))
            self.db.execute("UPDATE deliveries SET time_ns=COALESCE((SELECT time_ns FROM events WHERE events.id=deliveries.event_id), 0) WHERE time_ns=0")
        operations = self._columns("operations")
        if "operation_id" not in operations:
            self.db.execute("ALTER TABLE operations ADD COLUMN operation_id TEXT")
            self.db.execute("ALTER TABLE operations ADD COLUMN created_ns INTEGER NOT NULL DEFAULT 0")
            now = time.time_ns()
            for key, blob in self.db.execute("SELECT id,result FROM operations").fetchall():
                try:
                    body = json.loads(blob)
                except ValueError:
                    continue
                self.db.execute("UPDATE operations SET operation_id=?, created_ns=? WHERE id=?",
                    (body.get("operation_id"), now, key))

    def load(self):
        labs = {}
        for lab_id, meta_text in self.db.execute("SELECT id,state FROM lab_meta").fetchall():
            meta = json.loads(meta_text)
            self._meta_cache[lab_id] = meta_text
            row = self.db.execute("SELECT revision,status,boot_epoch,derived_ns FROM lab_state WHERE id=?", (lab_id,)).fetchone()
            revision, status, boot_epoch, derived_ns = row if row else (1, "stopped", 0, 0)
            devices, startup, runtime = {}, {}, {}
            for dev, config, saved in self.db.execute("SELECT dev,config,startup FROM lab_device WHERE lab_id=?", (lab_id,)).fetchall():
                devices[dev] = json.loads(config)
                startup[dev] = json.loads(saved)
            for dev, blob in self.db.execute("SELECT dev,state FROM lab_runtime WHERE lab_id=?", (lab_id,)).fetchall():
                runtime[dev] = json.loads(blob)
            lab = dict(meta["lab"])
            order = meta.get("device_ids", list(devices))
            lab["devices"] = {dev: devices[dev] for dev in order if dev in devices}
            lab["devices"].update({dev: config for dev, config in devices.items() if dev not in lab["devices"]})
            labs[lab_id] = {"lab": lab, "revision": revision, "status": status, "runtime": runtime,
                "startup": startup, "host_boot_epoch": boot_epoch, "derived_ns": derived_ns}
            labs[lab_id]["host_boot_id"] = meta.get("host_boot_id")
            labs[lab_id]["topology_revision"] = meta.get("topology_revision",revision)
        return labs

    def _write_state(self, lab_id, state, devices, runtime):
        lab = {key: value for key, value in state["lab"].items() if key != "devices"}
        meta_text = json.dumps({"lab": lab, "device_ids": list(state["lab"]["devices"]),"host_boot_id":state.get("host_boot_id"),"topology_revision":state.get("topology_revision",1)})
        if self._meta_cache.get(lab_id) != meta_text:
            self.db.execute("INSERT INTO lab_meta VALUES (?,?) ON CONFLICT(id) DO UPDATE SET state=excluded.state", (lab_id, meta_text))
            self._meta_cache[lab_id] = meta_text
        self.db.execute("INSERT INTO lab_state VALUES (?,?,?,?,?) ON CONFLICT(id) DO UPDATE SET revision=excluded.revision, "
            "status=excluded.status, boot_epoch=excluded.boot_epoch, derived_ns=excluded.derived_ns",
            (lab_id, state["revision"], state["status"], state.get("host_boot_epoch", 0), state.get("derived_ns", 0)))
        targets = list(state["lab"]["devices"]) if devices is None else [dev for dev in devices if dev in state["lab"]["devices"]]
        for dev in targets:
            self.db.execute("INSERT INTO lab_device VALUES (?,?,?,?) ON CONFLICT(lab_id,dev) DO UPDATE SET "
                "config=excluded.config, startup=excluded.startup",
                (lab_id, dev, json.dumps(state["lab"]["devices"][dev]), json.dumps(state["startup"].get(dev, state["lab"]["devices"][dev]))))
        targets = list(state["runtime"]) if runtime is None else [dev for dev in runtime if dev in state["runtime"]]
        for dev in targets:
            body = {key: value for key, value in state["runtime"][dev].items() if key not in DERIVED_RUNTIME}
            self.db.execute("INSERT INTO lab_runtime VALUES (?,?,?) ON CONFLICT(lab_id,dev) DO UPDATE SET state=excluded.state",
                (lab_id, dev, json.dumps(body)))

    @contextmanager
    def transaction(self):
        try:
            with self.db:
                yield
        except Exception:
            self._meta_cache.clear()
            raise

    def commit(self, lab_id, state, events=(), operation=None, devices=None, runtime=None):
        with self.transaction():
            self._commit(lab_id, state, events, operation, devices, runtime)
            if any(event["type"] == "labCreated" for event in events):
                self._initialize_cursors(state["lab"]["devices"])

    def _commit(self, lab_id, state, events=(), operation=None, devices=None, runtime=None):
        self._write_state(lab_id, state, devices, runtime)
        for e in events:
            cursor = self.db.execute("INSERT INTO events(lab_id,device_id,type,revision,time_ns,payload) VALUES (?,?,?,?,?,?)",
                (lab_id, e.get("device_id"), e["type"], state["revision"], e["time_ns"], json.dumps(e.get("payload", {}))))
            e["id"] = cursor.lastrowid
        if operation:
            self.db.execute("INSERT INTO operations(id,result,operation_id,created_ns,fingerprint) VALUES (?,?,?,?,?) "
                "ON CONFLICT(id) DO UPDATE SET result=excluded.result, operation_id=excluded.operation_id, created_ns=excluded.created_ns, fingerprint=excluded.fingerprint",
                (operation[0], json.dumps(operation[1]), operation[1].get("operation_id"), time.time_ns(),operation[2] if len(operation)>2 else None))


    def checkpoint(self, labs):
        with self.transaction():
            for lab_id, state in labs.items():
                self._write_state(lab_id, state, None, None)

    def _remove_device_records(self, lab_id, devices):
        for dev in devices:
            self.db.execute("DELETE FROM lab_device WHERE lab_id=? AND dev=?",(lab_id,dev))
            self.db.execute("DELETE FROM lab_runtime WHERE lab_id=? AND dev=?",(lab_id,dev))
            self.db.execute("DELETE FROM operations WHERE substr(id,1,?)=?",(len(dev)+1,dev+":"))
            self.db.execute("DELETE FROM cursors WHERE id IN (?,?)",(dev+":query",dev+":notification"))

    def replace_lab(self,lab_id,state,removed,events):
        with self.transaction():
            self._remove_device_records(lab_id,removed)
            self._commit(lab_id,state,events)
            self._initialize_cursors(state["lab"]["devices"], only_missing=True)

    def delete_lab(self,lab_id,state,now):
        with self.db:
            self._remove_device_records(lab_id,state["lab"]["devices"])
            self.db.execute("DELETE FROM lab_meta WHERE id=?",(lab_id,))
            self.db.execute("DELETE FROM lab_state WHERE id=?",(lab_id,))
            self.db.execute("INSERT INTO events(lab_id,type,revision,time_ns,payload) VALUES (?,?,?,?,?)",
                (lab_id,"labDeleted",state["revision"],now,"{}"))
        self._meta_cache.pop(lab_id,None)

    def _initialize_cursors(self, devices, only_missing=False):
        for dev in devices:
            floor = self.latest_event_id(dev)
            for role in ("query", "notification"):
                sql = "INSERT INTO cursors VALUES (?,?) ON CONFLICT(id) DO NOTHING" if only_missing else "INSERT INTO cursors VALUES (?,?) ON CONFLICT(id) DO UPDATE SET value=MAX(value,excluded.value)"
                self.db.execute(sql, (dev+":"+role, floor))

    def latest_event_id(self,dev):
        return self.db.execute("SELECT COALESCE(MAX(id),0) FROM events WHERE device_id=?",(dev,)).fetchone()[0]

    def append_events(self, lab_id, revision, events):
        with self.db:
            for event in events:
                cursor = self.db.execute("INSERT INTO events(lab_id,device_id,type,revision,time_ns,payload) VALUES (?,?,?,?,?,?)",
                    (lab_id, event["device_id"], event["type"], revision, event["time_ns"], json.dumps(event.get("payload", {}))))
                event["id"] = cursor.lastrowid

    def pending_events(self, dev, role, after=0, limit=1000):
        events = self.events(after, limit, dev)
        complete = {i for i, in self.db.execute("SELECT event_id FROM deliveries WHERE device=? AND role=? AND status IN ('processed','dropped') AND event_id>?", (dev, role, after))}
        for event in events:
            event["completed"] = event["id"] in complete
        return events

    def operation(self, key):
        row = self.db.execute("SELECT result FROM operations WHERE id=?", (key,)).fetchone()
        return json.loads(row[0]) if row else None

    def operation_fingerprint(self,key):
        row = self.db.execute("SELECT fingerprint FROM operations WHERE id=?",(key,)).fetchone()
        return row[0] if row else None

    def operation_by_id(self, operation_id):
        row = self.db.execute("SELECT result FROM operations WHERE operation_id=? LIMIT 1", (operation_id,)).fetchone()
        return json.loads(row[0]) if row else None

    def save_run(self, run):
        with self.db:
            self.db.execute("INSERT INTO runs VALUES (?,?) ON CONFLICT(id) DO UPDATE SET result=excluded.result", (run["operation_id"], json.dumps(run)))

    def load_runs(self):
        return {i: json.loads(value) for i, value in self.db.execute("SELECT * FROM runs")}

    def events(self, after=0, limit=1000, device=None):
        if after < 0 or not 1 <= limit <= 10000:
            from idc_simulator.errors import SimulationError
            raise SimulationError("Event cursor must be nonnegative and limit must be 1..10000")
        rows = self.db.execute("SELECT id,lab_id,device_id,type,revision,time_ns,payload FROM events WHERE id>? AND (? IS NULL OR device_id=?) ORDER BY id LIMIT ?", (after, device, device, min(limit, 10000)))
        return [dict(zip(("id", "lab_id", "device_id", "type", "revision", "time_ns", "payload"), row[:6]+(json.loads(row[6]),))) for row in rows]

    def delivery(self, event_id, status, payload):
        with self.db:
            self.db.execute("INSERT INTO deliveries(event_id,status,payload,device,role,time_ns) VALUES (?,?,?,?,?,?)",
                (event_id, status, json.dumps(payload), payload.get("device"), payload.get("role"), time.time_ns()))

    def deliveries(self, limit=1000):
        return [{"id": i, "event_id": e, "status": s, "payload": json.loads(p)} for i, e, s, p in self.db.execute("SELECT id,event_id,status,payload FROM deliveries ORDER BY id DESC LIMIT ?", (limit,))]

    def prune(self, event_rows=100_000, operation_hours=24):
        if event_rows < 0 or operation_hours < 0:
            raise ValueError("Retention parameters must be nonnegative")
        with self.db:
            cutoff_id = 0
            if event_rows:
                row = self.db.execute("SELECT id FROM events ORDER BY id DESC LIMIT 1 OFFSET ?", (event_rows,)).fetchone()
                cutoff_id = row[0] if row else 0
                floor = self.db.execute("SELECT MIN(value) FROM cursors").fetchone()[0]
                if floor is not None:
                    cutoff_id = min(cutoff_id, floor)
                if cutoff_id > 0:
                    self.db.execute("DELETE FROM deliveries WHERE event_id<=?", (cutoff_id,))
                    self.db.execute("DELETE FROM events WHERE id<=?", (cutoff_id,))
            if operation_hours:
                self.db.execute("DELETE FROM operations WHERE created_ns>0 AND created_ns<?", (time.time_ns()-int(operation_hours*3600*1_000_000_000),))
        return cutoff_id

    def close(self):
        self.db.close()

    def cursor(self, key, value=None):
        if value is not None:
            with self.db:
                self.db.execute("INSERT INTO cursors VALUES (?,?) ON CONFLICT(id) DO UPDATE SET value=MAX(value,excluded.value)", (key, value))
        row = self.db.execute("SELECT value FROM cursors WHERE id=?", (key,)).fetchone()
        return row[0] if row else 0
