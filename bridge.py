#!/usr/bin/env python3
"""beans on-demand bridge — the glue between Olla (the seam) and llama-server.

Event-driven, no polling for the wake: a real request connection triggers
llama-ondemand's ensure() (spawn the model if it died on idle), then this
process splices the connection to the freshly-ready backend. Trivial probe
paths (/health, /v1/models, /props) are answered locally while the model is
asleep so Olla's health/discovery checks DON'T wake it.

VRAM guard (Evan 11-01): if the model does not fit in free VRAM the call
FAILS immediately (503, no hold, no retry) — a loud, reportable error, never
a resource fight. Idle-stop is llama-ondemand's responsibility.
"""
import argparse
import json
import os
import select
import signal
import socket
import subprocess
import sys
import threading
import time

sys.path.insert(0, "/opt/llama_ondemand")
from llama_ondemand import LlamaManager  # noqa: E402

BUFSZ = 65536
SPLICE_TIMEOUT = 300.0   # idle-timeout for a spliced connection
WAKE_TIMEOUT = 260.0     # ensure() start budget (llama-ondemand default 240 + slack)
BACKEND = "127.0.0.1"
MIB = 1048576

PROBE_PATHS = {b"/health", b"/v1/models", b"/models", b"/props", b"/slots", b"/v1/health"}
# Olla's discovery/compat sweep also probes these metadata paths (ollama-api and
# llama.cpp extras). Answer them statically so discovery NEVER wakes a sleeping
# model; inference paths (/v1/chat/completions, /v1/embeddings, ...) are untouched.
PROBE_PREFIXES = (b"/api/", b"/v1/models/", b"/version", b"/v1/props", b"/api/v1/models")


def _model_id(cfg, name):
    m = cfg["models"].get(name) or next(iter(cfg["models"].values()))
    return os.path.join(cfg.get("weights_dir", "/models"), m["file"])


def static_answer(cfg, name, method, path):
    """Minimal answers for Olla probes while the model is asleep (no wake)."""
    pid = _model_id(cfg, name)
    if path in (b"/v1/models", b"/models"):
        body = json.dumps({"object": "list", "data": [{"id": pid, "object": "model"}]}).encode()
    elif path == b"/health":
        body = b'{"status":"ok"}'
    else:  # /props, /slots and friends: enough to satisfy discovery
        body = json.dumps({"status": "sleeping", "model": name}).encode()
    resp = (b"HTTP/1.1 200 OK\r\nContent-Type: application/json\r\n"
            b"Content-Length: " + str(len(body)).encode() +
            b"\r\nConnection: close\r\n\r\n" + body)
    return resp


def error_response(status, text):
    body = json.dumps({"error": {"message": text, "type": "beans_vram"}}).encode()
    return (b"HTTP/1.1 %d " % status + b"%s\r\nContent-Type: application/json\r\n" % b"Service Unavailable" +
            b"Content-Length: " + str(len(body)).encode() + b"\r\nConnection: close\r\n\r\n" + body)


class Bridge:
    def __init__(self, args):
        self.name = args.model
        self.listen_port = args.listen_port
        with open(args.config, encoding="utf-8") as fh:
            self.cfg = json.load(fh)
        self.backend_port = int(self.cfg["port"])
        self.manager = LlamaManager(self.cfg)
        m = self.cfg["models"][self.name]
        self.weights_mib = os.path.getsize(os.path.join(self.cfg["weights_dir"], m["file"])) / MIB
        self.vram_headroom_mib = float(self.cfg.get("vram_headroom_mib", 800))

    def _vram_free_mib(self):
        try:
            out = subprocess.run(
                ["nvidia-smi", "--query-gpu=memory.free", "--format=csv,noheader,nounits"],
                capture_output=True, text=True, timeout=5).stdout.strip()
            return float(out.splitlines()[0])
        except Exception:
            return None

    def backend_up(self):
        try:
            s = socket.create_connection((BACKEND, self.backend_port), timeout=1.0)
            s.close()
            return True
        except OSError:
            return False

    def _wait_backend(self):
        deadline = time.time() + WAKE_TIMEOUT
        while True:
            if self.backend_up():
                return True
            if time.time() > deadline:
                return False
            time.sleep(0.5)

    def read_request(self, conn):
        data = b""
        while b"\r\n\r\n" not in data and len(data) < (1 << 20):
            chunk = conn.recv(8192)
            if not chunk:
                break
            data += chunk
        head, sep, rest = data.partition(b"\r\n\r\n")
        if not sep:
            return data
        cl = 0
        for line in head.split(b"\r\n"):
            if line.lower().startswith(b"content-length:"):
                try:
                    cl = int(line.split(b":", 1)[1].strip())
                except ValueError:
                    cl = 0
        while len(rest) < cl:
            chunk = conn.recv(8192)
            if not chunk:
                break
            rest += chunk
        return head + b"\r\n\r\n" + rest

    def splice(self, conn, backend):
        last = time.time()
        socks = [conn, backend]
        for s in socks:
            s.settimeout(1.0)
        while socks:
            try:
                r, _, _ = select.select(socks, [], [], 2.0)
            except OSError:
                return
            if not r:
                if time.time() - last > SPLICE_TIMEOUT:
                    return
                continue
            for s in list(r):
                try:
                    data = s.recv(BUFSZ)
                except OSError:
                    data = b""
                if not data:
                    socks.remove(s)
                    other = socks[0] if socks else None
                    if other is not None:
                        try:
                            other.shutdown(socket.SHUT_WR)
                        except OSError:
                            return
                    if not socks:
                        return
                else:
                    last = time.time()
                    peer = backend if s is conn else conn
                    try:
                        peer.sendall(data)
                    except OSError:
                        return

    def handle(self, conn):
        try:
            req = self.read_request(conn)
            if not req:
                return
            first_line = req.split(b"\r\n", 1)[0].split(b" ")
            method = first_line[0] if first_line else b""
            path = first_line[1].split(b"?")[0] if len(first_line) > 1 else b""
            if path in PROBE_PATHS or path.startswith(PROBE_PREFIXES) or path == b"/api/show":
                if self.backend_up():
                    self._forward(conn, req)
                else:
                    conn.sendall(static_answer(self.cfg, self.name, method, path))
                return
            # Already up? Reuse — nothing new to allocate, so NO VRAM check. The guard
            # only gates the WAKE (spawning a new server that might not fit).
            if self.backend_up():
                self._forward(conn, req)
                return
            # Wake path: VRAM guard FIRST (fail loud, no hold, no retry)
            free = self._vram_free_mib()
            need = self.weights_mib + self.vram_headroom_mib
            if free is None:
                conn.sendall(error_response(503, "beans: VRAM check unavailable for %s" % self.name))
                return
            if free < need:
                conn.sendall(error_response(
                    503, "beans: not enough VRAM for %s (need ~%d MiB, free %d MiB) — "
                         "unload another model or adjust the workflow" % (self.name, int(need), int(free))))
                return
            # wake the model NOW (event-driven, no poll loop)
            self.manager.ensure(None, model=self.name)
            self._forward(conn, req)
        except Exception:
            return  # drop connection; llama-ondemand's identity/leases guard the rest
        finally:
            try:
                conn.close()
            except OSError:
                pass

    def _forward(self, conn, req):
        if not self._wait_backend():
            try:
                conn.sendall(error_response(503, "beans: model %s failed to start" % self.name))
            except OSError:
                pass
            return
        try:
            backend = socket.create_connection((BACKEND, self.backend_port), timeout=5)
        except OSError:
            return
        try:
            backend.sendall(req)
            self.splice(conn, backend)
        except OSError:
            pass
        finally:
            try:
                backend.close()
            except OSError:
                pass


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--listen-port", type=int, required=True)
    ap.add_argument("--model", required=True)
    ap.add_argument("--config", required=True)
    args = ap.parse_args()

    signal.signal(signal.SIGTERM, lambda *_: sys.exit(0))
    signal.signal(signal.SIGINT, lambda *_: sys.exit(0))

    bridge = Bridge(args)
    server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    server.bind(("127.0.0.1", args.listen_port))
    server.listen(16)
    sys.stdout.write("bridge %s on :%d -> :%d (vram limit %d MiB)\n" %
                     (args.model, args.listen_port, bridge.backend_port,
                      int(bridge.weights_mib + bridge.vram_headroom_mib)))
    sys.stdout.flush()
    while True:
        try:
            conn, _ = server.accept()
        except OSError:
            break
        threading.Thread(target=bridge.handle, args=(conn,), daemon=True).start()


if __name__ == "__main__":
    main()