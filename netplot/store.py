"""SQLite persistence for agents, endpoints, latency samples and hop data.

Schema notes
------------
* ``agents`` — each row is either the built-in ``local`` agent (probed in-process
  by the server) or a remote installable agent identified by a shared token.
* ``endpoints.agent_id`` — which agent is responsible for probing this target.
  The ``local`` agent is probed by the server's own monitor; remote agents pull
  their assignment from the server and push samples back via the ingest API.
"""

from __future__ import annotations

import secrets
import sqlite3
import threading
import time
from typing import List, Optional

LOCAL_AGENT_NAME = "local"


class Store:
    def __init__(self, path: str):
        self._path = path
        self._lock = threading.Lock()
        self._conn = sqlite3.connect(path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        with self._lock:
            self._conn.execute("PRAGMA journal_mode=WAL")
            self._conn.execute("PRAGMA synchronous=NORMAL")
            self._init_tables()
            self._migrate()

    def _init_tables(self):
        self._conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS agents (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL UNIQUE,
                token TEXT,
                created REAL NOT NULL,
                last_seen REAL
            );
            CREATE TABLE IF NOT EXISTS endpoints (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                agent_id INTEGER,
                host TEXT NOT NULL,
                name TEXT,
                port INTEGER,
                probe_type TEXT NOT NULL DEFAULT 'icmp',
                interval REAL NOT NULL DEFAULT 1.0,
                enabled INTEGER NOT NULL DEFAULT 1,
                created REAL NOT NULL
            );
            CREATE TABLE IF NOT EXISTS samples (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                endpoint_id INTEGER NOT NULL,
                ts REAL NOT NULL,
                seq INTEGER NOT NULL,
                rtt_ms REAL,
                lost INTEGER NOT NULL DEFAULT 0
            );
            CREATE INDEX IF NOT EXISTS idx_samples_ep_ts
                ON samples (endpoint_id, ts);
            CREATE TABLE IF NOT EXISTS hop_samples (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                endpoint_id INTEGER NOT NULL,
                ts REAL NOT NULL,
                hop INTEGER NOT NULL,
                address TEXT,
                host TEXT,
                rtt_ms REAL
            );
            CREATE INDEX IF NOT EXISTS idx_hop_ep_ts
                ON hop_samples (endpoint_id, ts);
            """
        )
        self._conn.commit()

    def _migrate(self):
        """Alter legacy tables created before the agent dimension existed."""
        cols = [r[1] for r in self._conn.execute(
                "PRAGMA table_info(endpoints)").fetchall()]
        if "agent_id" not in cols:
            self._conn.execute(
                "ALTER TABLE endpoints ADD COLUMN agent_id INTEGER")
        # ensure the built-in local agent exists
        cur = self._conn.execute(
            "INSERT OR IGNORE INTO agents (name, created) VALUES (?,?)",
            (LOCAL_AGENT_NAME, time.time()))
        # backfill any endpoints without an agent to the local agent
        local = self.get_local_agent_id_nolock()
        self._conn.execute(
            "UPDATE endpoints SET agent_id=? WHERE agent_id IS NULL", (local,))
        self._conn.commit()

    # ------------------------------------------------------------------- agents
    def get_local_agent_id_nolock(self) -> int:
        row = self._conn.execute(
            "SELECT id FROM agents WHERE name=?", (LOCAL_AGENT_NAME,)).fetchone()
        return row["id"]

    def get_local_agent_id(self) -> int:
        with self._lock:
            return self.get_local_agent_id_nolock()

    def list_agents(self) -> List[dict]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM agents ORDER BY id").fetchall()
        return [dict(r) for r in rows]

    def add_agent(self, name: str) -> dict:
        token = secrets.token_urlsafe(24)
        with self._lock:
            # ensure name is unique-ish; let UNIQUE constraint enforce
            self._conn.execute(
                "INSERT INTO agents (name, token, created) VALUES (?,?,?)",
                (name, token, time.time()))
            self._conn.commit()
            row = self._conn.execute(
                "SELECT * FROM agents WHERE name=?", (name,)).fetchone()
        return dict(row)

    def get_agent_by_token(self, token: str) -> Optional[dict]:
        if not token:
            return None
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM agents WHERE token=?", (token,)).fetchone()
        return dict(row) if row else None

    def touch_agent(self, agent_id: int):
        with self._lock:
            self._conn.execute(
                "UPDATE agents SET last_seen=? WHERE id=?",
                (time.time(), agent_id))
            self._conn.commit()

    def delete_agent(self, agent_id: int):
        local_id = self.get_local_agent_id()
        if agent_id == local_id:
            return False
        with self._lock:
            ids = [r["id"] for r in self._conn.execute(
                "SELECT id FROM endpoints WHERE agent_id=?", (agent_id,)).fetchall()]
            for eid in ids:
                self._conn.execute("DELETE FROM samples WHERE endpoint_id=?", (eid,))
                self._conn.execute("DELETE FROM hop_samples WHERE endpoint_id=?", (eid,))
                self._conn.execute("DELETE FROM endpoints WHERE id=?", (eid,))
            self._conn.execute("DELETE FROM agents WHERE id=?", (agent_id,))
            self._conn.commit()
        return True

    # ------------------------------------------------------------------ endpoints
    def add_endpoint(self, host, name, port, probe_type, interval,
                     agent_id: Optional[int] = None) -> int:
        with self._lock:
            if agent_id is None:
                agent_id = self.get_local_agent_id_nolock()
            cur = self._conn.execute(
                "INSERT INTO endpoints "
                "(agent_id, host, name, port, probe_type, interval, created) "
                "VALUES (?,?,?,?,?,?,?)",
                (agent_id, host, name, port, probe_type, interval, time.time()))
            self._conn.commit()
            return cur.lastrowid

    def list_endpoints(self, agent_id: Optional[int] = None) -> List[dict]:
        with self._lock:
            if agent_id is None:
                rows = self._conn.execute(
                    "SELECT * FROM endpoints ORDER BY id").fetchall()
            else:
                rows = self._conn.execute(
                    "SELECT * FROM endpoints WHERE agent_id=? ORDER BY id",
                    (agent_id,)).fetchall()
        return [dict(r) for r in rows]

    def get_endpoint(self, endpoint_id) -> Optional[dict]:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM endpoints WHERE id=?",
                (endpoint_id,)).fetchone()
        return dict(row) if row else None

    def set_endpoint_agent(self, endpoint_id: int, agent_id: int):
        with self._lock:
            self._conn.execute(
                "UPDATE endpoints SET agent_id=? WHERE id=?",
                (agent_id, endpoint_id))
            self._conn.commit()

    def set_enabled(self, endpoint_id, enabled: bool):
        with self._lock:
            self._conn.execute(
                "UPDATE endpoints SET enabled=? WHERE id=?",
                (1 if enabled else 0, endpoint_id))
            self._conn.commit()

    def delete_endpoint(self, endpoint_id):
        with self._lock:
            self._conn.execute("DELETE FROM samples WHERE endpoint_id=?",
                               (endpoint_id,))
            self._conn.execute("DELETE FROM hop_samples WHERE endpoint_id=?",
                               (endpoint_id,))
            self._conn.execute("DELETE FROM endpoints WHERE id=?",
                               (endpoint_id,))
            self._conn.commit()

    # ------------------------------------------------------------------- samples
    def insert_sample(self, endpoint_id: int, ts: float, seq: int,
                      rtt_ms: Optional[float], lost: bool):
        with self._lock:
            self._conn.execute(
                "INSERT INTO samples (endpoint_id, ts, seq, rtt_ms, lost) "
                "VALUES (?,?,?,?,?)",
                (endpoint_id, ts, seq, rtt_ms, 1 if lost else 0))
            self._conn.commit()

    def insert_samples_batch(self, rows: List[tuple]):
        """rows: (endpoint_id, ts, seq, rtt_ms, lost)"""
        with self._lock:
            self._conn.executemany(
                "INSERT INTO samples (endpoint_id, ts, seq, rtt_ms, lost) "
                "VALUES (?,?,?,?,?)", rows)
            self._conn.commit()

    def fetch_samples(self, endpoint_id: int, since_ts: float,
                      limit: int = 5000) -> List[dict]:
        if limit <= 0:
            limit = 5000
        with self._lock:
            rows = self._conn.execute(
                "SELECT ts, seq, rtt_ms, lost FROM samples "
                "WHERE endpoint_id=? AND ts>=? ORDER BY ts DESC LIMIT ?",
                (endpoint_id, since_ts, limit)).fetchall()
        return [dict(r) for r in rows]

    # ----------------------------------------------------------------- hop data
    def insert_hop_sample(self, endpoint_id: int, ts: float, hop: int,
                          address, host, rtt_ms):
        with self._lock:
            self._conn.execute(
                "INSERT INTO hop_samples "
                "(endpoint_id, ts, hop, address, host, rtt_ms) "
                "VALUES (?,?,?,?,?,?)",
                (endpoint_id, ts, hop, address, host, rtt_ms))
            self._conn.commit()

    def insert_hops_batch(self, rows: List[tuple]):
        """rows: (endpoint_id, ts, hop, address, host, rtt_ms)"""
        with self._lock:
            self._conn.executemany(
                "INSERT INTO hop_samples "
                "(endpoint_id, ts, hop, address, host, rtt_ms) "
                "VALUES (?,?,?,?,?,?)", rows)
            self._conn.commit()

    def fetch_hop_samples(self, endpoint_id: int,
                          since_ts: float) -> List[dict]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT ts, hop, address, host, rtt_ms FROM hop_samples "
                "WHERE endpoint_id=? AND ts>=? ORDER BY ts, id",
                (endpoint_id, since_ts)).fetchall()
        return [dict(r) for r in rows]

    # ------------------------------------------------------------------- pruning
    def prune(self, endpoint_id: int, keep_newer_than_ts: float):
        with self._lock:
            self._conn.execute(
                "DELETE FROM samples WHERE endpoint_id=? AND ts<?",
                (endpoint_id, keep_newer_than_ts))
            self._conn.execute(
                "DELETE FROM hop_samples WHERE endpoint_id=? AND ts<?",
                (endpoint_id, keep_newer_than_ts))
            self._conn.commit()

    def close(self):
        with self._lock:
            self._conn.close()
