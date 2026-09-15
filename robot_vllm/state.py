"""Single-coordinator durable state. Commit intent before any external action.

SQLite WAL/FULL protects local crash recovery, not distributed leader election.
The advisory lock prevents two processes sharing this state directory on Linux.
"""
from __future__ import annotations

import fcntl
import json
from pathlib import Path
import sqlite3


def encode(value):
    return json.dumps(value, sort_keys=True, allow_nan=False)


class StateStore:
    def __init__(self, directory):
        self.directory = Path(directory)
        self.directory.mkdir(parents=True, exist_ok=True)
        self.lock = (self.directory / "coordinator.lock").open("a+")
        try:
            fcntl.flock(self.lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            self.lock.close()
            raise RuntimeError("state_directory_in_use") from exc
        self.db = sqlite3.connect(self.directory / "state.sqlite3")
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("PRAGMA synchronous=FULL")
        self.db.executescript("""
            CREATE TABLE IF NOT EXISTS metadata (key TEXT PRIMARY KEY, value TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS executions (
                id TEXT PRIMARY KEY, owner TEXT NOT NULL, request_id TEXT NOT NULL,
                identity TEXT NOT NULL, record TEXT NOT NULL, UNIQUE(owner, request_id));
            CREATE TABLE IF NOT EXISTS tasks (
                id TEXT PRIMARY KEY, request_id TEXT UNIQUE, identity TEXT NOT NULL, record TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS checkpoints (
                task_id TEXT NOT NULL, node_id TEXT NOT NULL, record TEXT NOT NULL,
                PRIMARY KEY(task_id, node_id));
        """)
        version = self.db.execute("SELECT value FROM metadata WHERE key='schema'").fetchone()
        if version and version[0] != "1":
            self.close()
            raise RuntimeError("unsupported_state_schema")
        with self.db:
            self.db.execute("INSERT OR IGNORE INTO metadata VALUES ('schema', '1')")
        for task in self.tasks():
            if task["status"] in ("planning", "executing"):
                task.update(status="interrupted", reason="coordinator_restarted", task_verdict=None)
                self.update_task(task["task_id"], task)

    def bind_catalog(self, catalog):
        value = encode(catalog)
        prior = self.db.execute("SELECT value FROM metadata WHERE key='catalog'").fetchone()
        if prior and prior[0] != value and any(not e["settled"] for e in self.executions()):
            raise ValueError("unresolved_execution_topology_changed")
        with self.db:
            self.db.execute("INSERT OR REPLACE INTO metadata VALUES ('catalog', ?)", (value,))

    def executions(self):
        return [json.loads(r[0]) for r in self.db.execute("SELECT record FROM executions")]

    def execution(self, execution_id):
        row = self.db.execute("SELECT record FROM executions WHERE id=?", (execution_id,)).fetchone()
        return json.loads(row[0]) if row else None

    def request(self, owner, request_id):
        row = self.db.execute("SELECT identity,record FROM executions WHERE owner=? AND request_id=?",
                              (owner, request_id)).fetchone()
        return (row[0], json.loads(row[1])) if row else None

    def accept_execution(self, record, request_id, identity):
        with self.db:
            self.db.execute("INSERT INTO executions VALUES (?,?,?,?,?)",
                (record["execution_id"], record["owner"], request_id, identity, encode(record)))

    def update_execution(self, execution_id, **changes):
        record = self.execution(execution_id)
        if record is None:
            raise ValueError("unknown_persistent_execution")
        record.update(changes)
        with self.db:
            self.db.execute("UPDATE executions SET record=? WHERE id=?", (encode(record), execution_id))

    def tasks(self):
        return [json.loads(r[0]) for r in self.db.execute("SELECT record FROM tasks")]

    def task(self, task_id):
        row = self.db.execute("SELECT record FROM tasks WHERE id=?", (task_id,)).fetchone()
        return json.loads(row[0]) if row else None

    def task_request(self, request_id):
        row = self.db.execute("SELECT identity,record FROM tasks WHERE request_id=?", (request_id,)).fetchone()
        return (row[0], json.loads(row[1])) if row else None

    def accept_task(self, task_id, request_id, identity, record):
        with self.db:
            self.db.execute("INSERT INTO tasks VALUES (?,?,?,?)", (task_id, request_id, identity, encode(record)))

    def update_task(self, task_id, record):
        with self.db:
            self.db.execute("UPDATE tasks SET record=? WHERE id=?", (encode(record), task_id))

    def checkpoint(self, task_id, record):
        if record["status"] == "completed":
            value = encode(record)
            prior = self.completed(task_id).get(record["node"]["id"])
            if prior is not None and encode(prior) != value:
                raise ValueError("completed_checkpoint_is_immutable")
            with self.db:
                self.db.execute("INSERT OR IGNORE INTO checkpoints VALUES (?,?,?)",
                                (task_id, record["node"]["id"], value))

    def completed(self, task_id):
        return {row[0]: json.loads(row[1]) for row in self.db.execute(
            "SELECT node_id,record FROM checkpoints WHERE task_id=?", (task_id,))}

    def close(self):
        if getattr(self, "db", None) is not None:
            self.db.close()
            self.db = None
        if not self.lock.closed:
            fcntl.flock(self.lock, fcntl.LOCK_UN)
            self.lock.close()
