"""Threaded HTTP server exposing the JSON API and the web dashboard."""

from __future__ import annotations

import json
import os
import secrets
import subprocess
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

from . import __version__
from .monitor import Monitor
from .stats import hop_stats, summarize
from .store import Store

WEB_DIR = os.path.join(os.path.dirname(__file__), "web")
MIME = {
    ".html": "text/html; charset=utf-8",
    ".js": "text/javascript; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".png": "image/png",
    ".svg": "image/svg+xml",
}


class NetPlotServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    SESSION_TTL = 30 * 24 * 3600  # 30 days

    def __init__(self, addr, handler, store: Store, monitor: Monitor,
                 retention_days: float, auth_user: str = "",
                 auth_pass: str = ""):
        super().__init__(addr, handler)
        self.store = store
        self.monitor = monitor
        self.retention_days = retention_days
        self.hop_interval = monitor.hop_interval
        self.auth_user = auth_user
        self.auth_pass = auth_pass
        self.sessions: dict[str, float] = {}
        self.sessions_lock = threading.Lock()

    @property
    def auth_enabled(self) -> bool:
        return bool(self.auth_user)

    def _session_valid(self, sid: str) -> bool:
        if not sid:
            return False
        with self.sessions_lock:
            exp = self.sessions.get(sid)
            if exp is None:
                return False
            if time.time() > exp:
                self.sessions.pop(sid, None)
                return False
            return True

    def _issue_session(self) -> str:
        sid = secrets.token_urlsafe(32)
        with self.sessions_lock:
            self.sessions[sid] = time.time() + self.SESSION_TTL
        return sid

    def _drop_session(self, sid: str):
        with self.sessions_lock:
            self.sessions.pop(sid, None)


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    # ------------------------------------------------------------------ helpers
    @property
    def svr(self) -> NetPlotServer:
        return self.server  # type: ignore[return-value]

    def _json(self, obj, status=200):
        body = json.dumps(obj).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _read_json(self):
        length = int(self.headers.get("Content-Length") or 0)
        if length <= 0:
            return {}
        return json.loads(self.rfile.read(length).decode())

    def _serve_static(self, path: str):
        # prevent path traversal
        rel = os.path.normpath(path).lstrip("/")
        full = os.path.join(WEB_DIR, rel)
        if not full.startswith(WEB_DIR) or not os.path.isfile(full):
            self._json({"error": "not found"}, 404)
            return
        ext = os.path.splitext(full)[1].lower()
        with open(full, "rb") as f:
            body = f.read()
        self.send_response(200)
        self.send_header("Content-Type", MIME.get(ext, "application/octet-stream"))
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _window(self):
        q = parse_qs(urlparse(self.path).query)
        return float(q.get("window", ["300"])[0])

    def _agent_token(self):
        return self.headers.get("X-Agent-Token")

    # ----------------------------------------------------------- auth helpers
    def _sid(self):
        cookie = self.headers.get("Cookie") or ""
        for part in cookie.split(";"):
            part = part.strip()
            if part.lower().startswith("netplot_session="):
                return part[len("netplot_session="):]
        return ""

    def _authed(self) -> bool:
        if not self.svr.auth_enabled:
            return True
        return self.svr._session_valid(self._sid())

    def _set_session_cookie(self, sid: str):
        # Must send a valid JSON body so the SPA's response.json() succeeds.
        body = json.dumps({"ok": True, "session": sid}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Set-Cookie",
                         f"netplot_session={sid}; HttpOnly; SameSite=Strict; "
                         f"Path=/; Max-Age={int(self.svr.SESSION_TTL)}")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _auth_gate(self) -> bool:
        """Return True if the request may proceed; otherwise send 401."""
        if self._authed():
            return True
        self._json({"error": "unauthorized"}, 401)
        return False

    def _auth_agent(self):
        token = self._agent_token()
        return self.svr.store.get_agent_by_token(token) if token else None

    # ------------------------------------------------------------------- routes
    def do_GET(self):
        parsed = urlparse(self.path)
        path = parsed.path
        try:
            if path in ("/", "/index.html"):
                self._serve_static("index.html")
            elif path.startswith("/static/"):
                self._serve_static(path[len("/static/"):])
            elif path.startswith("/api/") and not self._auth_gate():
                pass
            elif path == "/api/info":
                self._json({
                    "version": __version__,
                    "retention_days": self.svr.retention_days,
                    "hop_interval": self.svr.hop_interval,
                    "probe_types": ["icmp", "tcp"],
                })
            elif path == "/api/endpoints":
                self._json({"endpoints": self.svr.store.list_endpoints()})
            elif path == "/api/samples":
                self._samples()
            elif path == "/api/summary":
                self._summary()
            elif path == "/api/route":
                self._route()
            elif path == "/api/trace":
                self._trace(parse_qs(parsed.query).get("host", [""])[0])
            elif path == "/api/agents":
                self._agents()
            elif path == "/api/agent/config":
                self._agent_config()
            else:
                self._json({"error": "not found"}, 404)
        except Exception as exc:  # keep the server alive on any handler error
            try:
                self._json({"error": str(exc)}, 500)
            except Exception:
                pass

    def _endpoint_id(self):
        q = parse_qs(urlparse(self.path).query)
        try:
            return int(q["endpoint"][0])
        except (KeyError, ValueError):
            return None

    def _samples(self):
        eid = self._endpoint_id()
        if eid is None:
            self._json({"error": "missing endpoint"}, 400)
            return
        since = time.time() - self._window()
        samples = self.svr.store.fetch_samples(eid, since)
        samples.reverse()  # oldest -> newest for plotting
        self._json({"endpoint": eid, "window": self._window(), "samples": samples})

    def _summary(self):
        eid = self._endpoint_id()
        if eid is None:
            self._json({"error": "missing endpoint"}, 400)
            return
        since = time.time() - self._window()
        samples = self.svr.store.fetch_samples(eid, since)
        self._json({"endpoint": eid, "window": self._window(),
                    "summary": summarize(samples)})

    def _route(self):
        eid = self._endpoint_id()
        if eid is None:
            self._json({"error": "missing endpoint"}, 400)
            return
        since = time.time() - self._window()
        records = self.svr.store.fetch_hop_samples(eid, since)
        self._json({"endpoint": eid, "window": self._window(),
                    "hops": hop_stats(records)})

    def _agents(self):
        agents = self.svr.store.list_agents()
        self._json({"agents": agents,
                    "local_agent_id": self.svr.store.get_local_agent_id()})

    def _agent_config(self):
        agent = self._auth_agent()
        if not agent:
            self._json({"error": "unauthorized"}, 401)
            return
        self.svr.store.touch_agent(agent["id"])
        eps = [e for e in self.svr.store.list_endpoints(agent_id=agent["id"])
               if e["enabled"]]
        self._json({
            "ok": True,
            "agent": {"id": agent["id"], "name": agent["name"]},
            "now": time.time(),
            "hop_interval": self.svr.hop_interval,
            "config": {"endpoints": eps},
        })

    def _trace(self, host: str):
        if not host:
            self._json({"error": "missing host"}, 400)
            return
        try:
            proc = subprocess.run(
                ["traceroute", "-w", "1", "-q", "1", "-m", "20", host],
                capture_output=True, text=True, timeout=40)
            lines = (proc.stdout or proc.stderr or "").splitlines()
            self._json({"host": host, "exit": proc.returncode, "lines": lines})
        except FileNotFoundError:
            self._json({"error": "traceroute not installed"}, 501)
        except subprocess.TimeoutExpired:
            self._json({"error": "traceroute timed out"}, 504)

    def do_POST(self):
        parsed = urlparse(self.path)
        path = parsed.path
        try:
            if path == "/api/login":
                self._login(self._read_json())
            elif path == "/api/logout":
                self._logout()
            elif path == "/api/agent/config":
                self._agent_config()
            elif path == "/api/agent/ingest":
                self._agent_ingest(self._read_json())
            elif not self._auth_gate():
                pass
            elif path == "/api/endpoints":
                self._add_endpoint(self._read_json())
            elif path == "/api/endpoints/enable":
                self._enable_endpoint(self._read_json())
            elif path == "/api/agents":
                self._add_agent(self._read_json())
            else:
                self._json({"error": "not found"}, 404)
        except Exception as exc:
            try:
                self._json({"error": str(exc)}, 500)
            except Exception:
                pass

    def _add_endpoint(self, payload: dict):
        host = (payload.get("host") or "").strip()
        if not host:
            self._json({"error": "host is required"}, 400)
            return
        probe_type = payload.get("probe_type", "icmp")
        if probe_type not in ("icmp", "tcp"):
            self._json({"error": "probe_type must be icmp or tcp"}, 400)
            return
        interval = float(payload.get("interval", 1.0) or 1.0)
        interval = max(0.2, min(interval, 60.0))
        name = (payload.get("name") or host).strip()
        try:
            port = int(payload.get("port", 443))
        except (TypeError, ValueError):
            port = 443

        payload_agent = payload.get("agent_id")
        try:
            agent_id = int(payload_agent) if payload_agent not in (None, "") else None
        except (TypeError, ValueError):
            agent_id = None
        eid = self.svr.store.add_endpoint(
            host, name, port, probe_type, interval, agent_id=agent_id)
        ep = self.svr.store.get_endpoint(eid)
        if ep["agent_id"] == self.svr.store.get_local_agent_id():
            self.svr.monitor.add(ep)
        self._json({"ok": True, "id": eid}, 201)

    def _login(self, payload: dict):
        if not self.svr.auth_enabled:
            sid = self.svr._issue_session()
            self._set_session_cookie(sid)
            return
        user = (payload.get("username") or "").strip()
        pw = payload.get("password") or ""
        if secrets.compare_digest(user, self.svr.auth_user) and \
                secrets.compare_digest(pw, self.svr.auth_pass):
            sid = self.svr._issue_session()
            self._set_session_cookie(sid)
        else:
            self._json({"error": "unauthorized"}, 401)

    def _logout(self):
        sid = self._sid()
        if sid:
            self.svr._drop_session(sid)
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Set-Cookie",
                         "netplot_session=; HttpOnly; SameSite=Strict; "
                         "Path=/; Max-Age=0")
        self.send_header("Content-Length", "2")
        self.end_headers()
        self.wfile.write(b"{}")

    def _add_agent(self, payload: dict):
        name = (payload.get("name") or "").strip()
        if not name:
            self._json({"error": "name is required"}, 400)
            return
        agent = self.svr.store.add_agent(name)
        self._json({"ok": True, "agent": agent}, 201)

    def _agent_ingest(self, payload: dict):
        agent = self._auth_agent()
        if not agent:
            self._json({"error": "unauthorized"}, 401)
            return
        store = self.svr.store
        store.touch_agent(agent["id"])
        allowed = {e["id"] for e in store.list_endpoints(agent_id=agent["id"])}

        samples = payload.get("samples") or []
        sample_rows = [
            (r["endpoint"], r["ts"], int(r.get("seq", 0)),
             r.get("rtt_ms"), 1 if r.get("lost") else 0)
            for r in samples if r.get("endpoint") in allowed
        ]
        hops = payload.get("hops") or []
        hop_rows = [
            (r["endpoint"], r["ts"], int(r["hop"]), r.get("address"),
             r.get("host"), r.get("rtt_ms"))
            for r in hops if r.get("endpoint") in allowed
        ]
        if sample_rows:
            store.insert_samples_batch(sample_rows)
        if hop_rows:
            store.insert_hops_batch(hop_rows)
        self._json({"ok": True, "accepted_samples": len(sample_rows),
                    "accepted_hops": len(hop_rows)})

    def _enable_endpoint(self, payload: dict):
        eid = payload.get("id")
        enabled = bool(payload.get("enabled", True))
        ep = self.svr.store.get_endpoint(eid)
        if not ep:
            self._json({"error": "endpoint not found"}, 404)
            return
        self.svr.store.set_enabled(eid, enabled)
        ep = self.svr.store.get_endpoint(eid)
        if ep["agent_id"] == self.svr.store.get_local_agent_id():
            self.svr.monitor.set_enabled(ep, enabled)
        self._json({"ok": True})

    def do_DELETE(self):
        path = urlparse(self.path).path
        try:
            if not self._auth_gate():
                return
            if path == "/api/endpoints":
                eid = self._endpoint_id()
                if eid is None:
                    self._json({"error": "missing endpoint"}, 400)
                    return
                self.svr.monitor.remove(eid)
                self.svr.store.delete_endpoint(eid)
                self._json({"ok": True})
            elif path == "/api/agents":
                q = parse_qs(urlparse(self.path).query).get("agent", [""])[0]
                try:
                    agent_id = int(q)
                except (TypeError, ValueError):
                    self._json({"error": "missing agent"}, 400)
                    return
                ok = self.svr.store.delete_agent(agent_id)
                self._json({"ok": ok})
            else:
                self._json({"error": "not found"}, 404)
        except Exception as exc:
            try:
                self._json({"error": str(exc)}, 500)
            except Exception:
                pass

    def log_message(self, fmt, *args):  # quiet the access log
        pass
