#!/usr/bin/env python3
"""beansd — beans registry supervisor (v0.3).

The registry is /beans/<model>/ — one directory per model holding the weights
GGUF plus a config.json. beansd watches the tree with inotify and reconciles
the running system against it on every change:

  * active dir           -> write ondemand-<dir>.json, wire one bridge
  * config.json edited   -> restart only that model's bridge
  * <dir>-disabled       -> ignored (bridge killed if it was running)
  * <dir>-pending        -> download in progress (curl under .download.pid)
  * no gguf but url      -> rename to -pending, download, verify, wire itself
  * no gguf and no url   -> loud error, ignored

Nothing loads models at boot. Bridges wake llama-server on demand and
llama-ondemand kills them on idle. beansd owns: Olla (the seam), the bridges,
and the downloads. SIGTERM -> everything dies, no zombies.

No personal paths: the tree, config and state are all env/argv driven.
"""
import argparse
import ctypes
import errno
import hashlib
import io
import json
import os
import select
import signal
import socket
import struct
import subprocess
import sys
import threading
import time

TREE_DEF = "/beans"
CFG_OUT_DEF = "/config"
STATE_DEF = "/var/lib/beans/state.json"
BEGIN_PORT = 8081
ROUTER_DEF = "0.0.0.0:11434"
ROUTER_BACKEND = "127.0.0.1"
BUFSZ = 65536
SPLICE_TIMEOUT = 300.0
DEBOUNCE_S = 1.5
RECONCILE_MAX = 12
SUF_DISABLED = "-disabled"
SUF_PENDING = "-pending"
PID_FILE = ".download.pid"
FAIL_FILE = ".download.failed"

ALLOWED = {"file", "url", "sha256", "ctx", "threads", "misc_args",
           "reasoning_budget", "embeddings", "pooling", "idle_seconds",
           "vram_headroom_mib"}

IN_NONBLOCK = 0x00000800
IN_CLOEXEC = 0x00080000
WATCH_MASK = (0x00000001 | 0x00000002 | 0x00000004 | 0x00000008 |  # ACCESS MODIFY ATTRIB CLOSE_WRITE
              0x00000100 | 0x00000200 | 0x00000400 |                # CREATE DELETE DELETE_SELF
              0x00000040 | 0x00000080 | 0x00000800 |                # MOVED_FROM MOVED_TO MOVED_SELF
              0x00008000)                                          # IGNORED


def log(msg):
    print("[beansd %s] %s" % (time.strftime("%H:%M:%S"), msg), flush=True)


class Inotify:
    def __init__(self):
        self.libc = ctypes.CDLL(None, use_errno=True)
        self.fd = self.libc.inotify_init1(IN_NONBLOCK | IN_CLOEXEC)
        if self.fd < 0:
            raise OSError(ctypes.get_errno(), "inotify_init1")
        self.wds = {}  # wd -> path

    def add(self, path):
        if path in self.wds.values():
            return
        b = path.encode()
        wd = self.libc.inotify_add_watch(self.fd, b, WATCH_MASK)
        if wd < 0:
            log("WARN: cannot watch %s (%s)" % (path, os.strerror(ctypes.get_errno())))
            return
        self.wds[wd] = path

    def reset(self, root):
        for wd in list(self.wds):
            self.libc.inotify_rm_watch(self.fd, wd)
        self.wds.clear()
        self.add(root)
        try:
            for entry in sorted(os.listdir(root)):
                p = os.path.join(root, entry)
                if os.path.isdir(p) and not entry.startswith("."):
                    self.add(p)
        except OSError:
            pass

    def drain(self):
        """Drain pending events. Returns True if any event was seen."""
        saw = False
        while True:
            try:
                raw = os.read(self.fd, 65536)
            except BlockingIOError:
                return saw
            except OSError as e:
                if e.errno in (errno.EAGAIN, errno.EWOULDBLOCK):
                    return saw
                return saw
            if not raw:
                return saw
            saw = True
            off = 0
            while off + 16 <= len(raw):
                wd, mask, _cookie, ln = struct.unpack_from("iIII", raw, off)
                off += 16
                name = ""
                if ln:
                    name = raw[off:off + ln].rstrip(b"\0").decode(errors="replace")
                    off += ln


class BeansD:
    def __init__(self, tree, cfg_out, state_file, listen=ROUTER_DEF):
        self.tree = tree
        self.cfg_out = cfg_out
        self.state_file = state_file
        self.state = self.load_state()
        self.bridges = {}   # dir -> (pid, port, name, gguf_basename)
        self.downloads = {}  # pending dir path -> pid
        self.inot = Inotify()
        self.stop = False
        self.listen = listen
        self.router_sock = None

    # ------------------------------------------------------------------ state
    def load_state(self):
        try:
            with io.open(self.state_file, encoding="utf-8") as f:
                s = json.load(f)
            s.setdefault("ports", {})
            s.setdefault("config_sha", {})
            return s
        except Exception:
            return {"ports": {}, "config_sha": {}}

    def save_state(self):
        os.makedirs(os.path.dirname(self.state_file), exist_ok=True)
        tmp = self.state_file + ".tmp"
        with io.open(tmp, "w", encoding="utf-8") as f:
            json.dump(self.state, f, indent=1)
        os.replace(tmp, self.state_file)

    def port_for(self, d):
        if d in self.state["ports"]:
            return self.state["ports"][d]
        used = set(self.state["ports"].values())
        port = BEGIN_PORT
        while port in used:
            port += 1
        self.state["ports"][d] = port
        return port

    # --------------------------------------------------------------- scanning
    def scan(self):
        """Return list of dicts: dir, status(active|disabled|pending), name, cfg, gguf, url, sha."""
        out = []
        try:
            entries = sorted(os.listdir(self.tree))
        except OSError as e:
            log("FATAL: cannot read tree %s: %s" % (self.tree, e))
            sys.exit(2)
        for entry in entries:
            if entry.startswith("."):
                continue
            d = os.path.join(self.tree, entry)
            if not os.path.isdir(d):
                continue
            base = entry
            if entry.endswith(SUF_DISABLED):
                out.append({"dir": d, "name": base[:-len(SUF_DISABLED)], "status": "disabled",
                            "cfg": None, "gguf": None, "url": None, "sha": None})
                continue
            if entry.endswith(SUF_PENDING):
                out.append({"dir": d, "name": base[:-len(SUF_PENDING)], "status": "pending",
                            "cfg": None, "gguf": None, "url": None, "sha": None})
                continue
            cfg_path = os.path.join(d, "config.json")
            cfg = {}
            if os.path.exists(cfg_path):
                try:
                    with io.open(cfg_path, encoding="utf-8") as f:
                        cfg = json.load(f)
                except ValueError as e:
                    log("ERROR: %s bad config.json: %s — ignored" % (d, e))
                    continue
                unknown = sorted(set(cfg) - ALLOWED)
                if unknown:
                    log("ERROR: %s unknown config keys %s — ignored" % (d, unknown))
                    continue
            else:
                log("ERROR: %s has no config.json — ignored" % d)
                continue
            ggu = cfg.get("file") or ""
            if not ggu:
                files = [x for x in os.listdir(d) if x.endswith(".gguf")]
                if len(files) == 1:
                    ggu = files[0]
            if ggu:
                cfg = dict(cfg)
                cfg["file"] = ggu
            gguf_path = os.path.join(d, ggu) if ggu else None
            out.append({"dir": d, "name": entry, "status": "active", "cfg": cfg,
                        "gguf": gguf_path, "url": cfg.get("url"), "sha": cfg.get("sha256")})
        return out

    # ------------------------------------------------------------ model wiring
    def ondemand_path(self, name):
        return os.path.join(self.cfg_out, "ondemand-" + name + ".json")

    def write_ondemand(self, m):
        cfg = m["cfg"]
        os.makedirs(self.cfg_out, exist_ok=True)
        om = {
            "ssh": None,
            "port": 18081 + (self.port_for(m["dir"]) - BEGIN_PORT),
            "state_dir": "/var/lib/llama-ondemand-" + os.path.basename(m["dir"]),
            "server_bin": "/app/llama-server",
            "weights_dir": m["dir"],
            "ctx": int(cfg.get("ctx", 4096)),
            "threads": int(cfg.get("threads", 4)),
            "server_args": list(cfg.get("misc_args", [])),  # registry misc_args -> llama-ondemand server_args
            "headroom_mib": 64,
            "min_free_mib": 64,
            "idle_seconds": int(cfg.get("idle_seconds", 300)),
            "watch_poll_seconds": 15,
            "start_timeout_seconds": 240,
            "lease_ahead_seconds": 120,
            "default_model": os.path.basename(m["dir"]),
            "vram_headroom_mib": int(cfg.get("vram_headroom_mib", 800)),
            "models": {os.path.basename(m["dir"]):
                       {"file": cfg["file"], "role": "workhorse"}},
        }
        if cfg.get("reasoning_budget") is not None:
            om["models"][om["default_model"]]["reasoning_budget"] = int(cfg["reasoning_budget"])
        if cfg.get("embeddings"):
            om["server_args"] += ["--embeddings"]
            if cfg.get("pooling"):
                om["server_args"] += ["--pooling", cfg["pooling"]]
        with io.open(self.ondemand_path(os.path.basename(m["dir"])), "w", encoding="utf-8") as f:
            json.dump(om, f, indent=2)

    def spawn_bridge(self, m):
        name = os.path.basename(m["dir"])
        port = self.port_for(m["dir"])
        cfg_path = self.ondemand_path(name)
        proc = subprocess.Popen(
            [sys.executable, "/opt/bridge.py", "--listen-port", str(port),
             "--model", name, "--config", cfg_path])
        gguf = os.path.basename(m["cfg"].get("file") or "")
        self.bridges[name] = (proc.pid, port, name, gguf)
        log("bridge %s on :%d (pid %d)" % (name, port, proc.pid))

    def kill_model(self, name_path):
        """Kill a model's bridge + its llama-server (by unique gguf path)."""
        base = os.path.basename(name_path.rstrip("/"))
        pid, port, _, _ = self.bridges.pop(base, (None, None, None, None))
        if pid:
            try:
                os.kill(pid, signal.SIGTERM)
                log("killed bridge %s (pid %d)" % (base, pid))
            except OSError:
                pass
        # kill the llama-server for this model — unique path match
        try:
            subprocess.run(["pkill", "-TERM", "-f", os.path.join(name_path, "")],
                           capture_output=True)
        except OSError:
            pass

    # ------------------------------------------------------------- downloads
    def start_download(self, dpath, cfg):
        base = os.path.basename(dpath)
        name = base[:-len(SUF_PENDING)]
        url = cfg.get("url")
        ggu = cfg.get("file")
        if not url or not ggu:
            log("ERROR: %s pending but no url/file — remove the dir or fix config" % dpath)
            return
        dest = os.path.join(dpath, ggu)
        proc = subprocess.Popen(["curl", "-L", "-C", "-", "-sS", "--fail", "-o", dest, url])
        with io.open(os.path.join(dpath, PID_FILE), "w", encoding="utf-8") as f:
            json.dump({"pid": proc.pid, "url": url, "dest": dest,
                       "started": time.time()}, f)
        self.downloads[dpath] = proc.pid
        log("download %s: pid %d %s" % (name, proc.pid, url))

    def check_pending(self, m, cfg):
        """Handle a -pending dir. Returns True if it became active-ready (wired next pass)."""
        dpath = m["dir"]
        base = os.path.basename(dpath)
        name = base[:-len(SUF_PENDING)]
        fail_path = os.path.join(dpath, FAIL_FILE)
        pid_path = os.path.join(dpath, PID_FILE)
        if cfg is None:  # always self-load: sha verify + download need the url/file keys
            cfg = {}
            try:
                with io.open(os.path.join(dpath, "config.json"), encoding="utf-8") as fh:
                    cfg = json.load(fh)
            except Exception:
                pass
        # failed marker: only a config.json edit clears it (reconcile removes marker on sha change)
        if os.path.exists(fail_path):
            return False
        pid_info = None
        if os.path.exists(pid_path):
            try:
                with io.open(pid_path, encoding="utf-8") as f:
                    pid_info = json.load(f)
            except ValueError:
                pid_info = None
        gguf = None
        for f in os.listdir(dpath):
            if f.endswith(".gguf"):
                gguf = os.path.join(dpath, f)
                break
        if pid_info and self._proc_alive(pid_info.get("pid")):
            return False  # still downloading
        if pid_info:
            dest = pid_info.get("dest")
            if gguf and os.path.exists(gguf):
                # download finished — verify sha if configured
                if cfg is not None and cfg.get("sha256"):
                    if self._verify(gguf, cfg["sha256"]):
                        log("verified sha256 for %s" % name)
                    else:
                        log("ERROR: sha256 mismatch for %s — keeping -pending (fix config to retry)" % name)
                        with io.open(fail_path, "w", encoding="utf-8") as f:
                            f.write("sha256 mismatch\n")
                        try:
                            os.unlink(pid_path)
                        except OSError:
                            pass
                        return False
                # promote back to active
                new_path = os.path.join(self.tree, name)
                try:
                    os.rename(dpath, new_path)
                except OSError as e:
                    log("ERROR: cannot promote %s: %s" % (dpath, e))
                    return False
                try:  # pid file MOVED with the dir — unlink at the NEW path
                    os.unlink(os.path.join(new_path, PID_FILE))
                except OSError:
                    pass
                self.downloads.pop(dpath, None)
                log("download complete — %s active" % name)
                return True
            # process died, no gguf — resume (curl -C - continues partial)
            log("resuming download %s (previous pid %s gone)" % (name, pid_info.get("pid")))
        # no pid or dead without file → (re)start
        self.start_download(dpath, cfg)
        return False

    def _proc_alive(self, pid):
        if not pid:
            return False
        try:
            os.kill(pid, 0)
            return True
        except OSError:
            return False

    def _verify(self, path, want):
        h = hashlib.sha256()
        try:
            with io.open(path, "rb") as f:
                while True:
                    chunk = f.read(1 << 20)
                    if not chunk:
                        break
                    h.update(chunk)
        except OSError:
            return False
        return h.hexdigest() == want

    # ------------------------------------------------------------- reconcile
    def reconcile(self):
        changed_any = False
        for _ in range(RECONCILE_MAX):
            scan = self.scan()
            actives = [m for m in scan if m["status"] == "active"]
            # refresh watches AFTER any rename churn
            # 1. pending handling / download promotion
            for m in scan:
                if m["status"] == "pending":
                    if self.check_pending(m, None):
                        changed_any = True
                        continue
            if changed_any:
                scan = self.scan()
                actives = [m for m in scan if m["status"] == "active"]
            # 2. active models
            wanted = {}
            for m in actives:
                base = os.path.basename(m["dir"])
                cfg = m["cfg"] or {}
                has_gguf = m["gguf"] and os.path.exists(m["gguf"])
                if not has_gguf:
                    if cfg.get("url") and cfg.get("file"):
                        # rename to -pending and let the pending path handle it
                        pend = os.path.join(self.tree, base + SUF_PENDING)
                        try:
                            os.rename(m["dir"], pend)
                            log("no gguf — %s -> %s (download)" % (base, base + SUF_PENDING))
                            changed_any = True
                        except OSError as e:
                            log("ERROR: cannot rename %s: %s" % (m["dir"], e))
                        continue
                    log("ERROR: %s has no gguf and no url — ignored (fix config.json)" % base)
                    continue
                wanted[base] = m
            # 3. kill models no longer wanted
            for base in list(self.bridges):
                if base not in wanted:
                    self.kill_model(os.path.join(self.tree, base))
                    self.state["config_sha"].pop(base, None)
                    changed_any = True
            # 4. wire/rewire wanted
            for base, m in wanted.items():
                cfg = m["cfg"] or {}
                cfg_path = os.path.join(m["dir"], "config.json")
                try:
                    with io.open(cfg_path, "rb") as f:
                        sha = hashlib.sha256(f.read()).hexdigest()
                except OSError:
                    sha = None
                seen = self.state["config_sha"].get(base)
                wired = base in self.bridges
                fail_path = os.path.join(m["dir"], FAIL_FILE)
                if os.path.exists(fail_path):  # a previous failure marker
                    try:
                        os.unlink(fail_path)
                    except OSError:
                        pass
                if not wired:
                    self.write_ondemand(m)
                    self.spawn_bridge(m)
                    self.state["config_sha"][base] = sha
                    changed_any = True
                elif sha != seen:
                    log("config change: %s reconfigured" % base)
                    self.kill_model(os.path.join(self.tree, base))
                    self.write_ondemand(m)
                    self.spawn_bridge(m)
                    self.state["config_sha"][base] = sha
                    changed_any = True
            self.save_state()
            self.inot.reset(self.tree)
            if not changed_any:
                break
            changed_any = False
        self.inot.reset(self.tree)

    # ---------------------------------------------------------------- main
    def run(self):
        signal.signal(signal.SIGTERM, lambda *_: self.shutdown())
        signal.signal(signal.SIGINT, lambda *_: self.shutdown())
        signal.signal(signal.SIGCHLD, signal.SIG_IGN)  # auto-reap children
        self.reconcile()
        self.inot.reset(self.tree)
        self.start_router()
        log("ready — watching %s" % self.tree)
        last_event = 0.0
        while not self.stop:
            r, _, _ = select.select([self.inot.fd], [], [], 0.25)
            if r:
                self.inot.drain()
                last_event = time.time()
            elif last_event and (time.time() - last_event) >= DEBOUNCE_S:
                last_event = 0.0
                self.reconcile()
            if self.bridges and all(not self._proc_alive(pid) for pid, _, _, _ in self.bridges.values()):
                log("all bridges died — reconciling")
                self.reconcile()
        self.shutdown()

    def shutdown(self):
        if self.stop:
            return
        self.stop = True
        log("shutting down")
        if self.router_sock:
            try:
                self.router_sock.close()
            except OSError:
                pass
        for base in list(self.bridges):
            self.kill_model(os.path.join(self.tree, base))
        for dpath, pid in list(self.downloads.items()):
            try:
                os.kill(pid, signal.SIGTERM)
            except OSError:
                pass
        sys.exit(0)

    # ----------------------------------------------------------------- router
    # One published port (BEANS_LISTEN, default 0.0.0.0:11434). Meta routes are
    # answered by beansd from the live scan; inference requests are dispatched by
    # the body's `model` field to the owning bridge port and byte-spliced through
    # (streaming SSE passes untouched). No router process, no handoff file.

    def _read_request(self, conn):
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

    def _splice(self, conn, backend):
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

    def _http(self, status, body, ctype=b"application/json"):
        return (b"HTTP/1.1 %d %s\r\nContent-Type: %s\r\n"
                b"Content-Length: %d\r\nConnection: close\r\n\r\n%s" %
                (status, b"OK" if status == 200 else b"Not Found", ctype,
                 len(body), body))

    def _models_body(self):
        """Merged /v1/models list from the live scan: id = config name, ctx intact."""
        data = []
        for m in self.scan():
            if m["status"] != "active" or not m["gguf"]:
                continue
            cfg = m["cfg"] or {}
            name = os.path.basename(m["dir"])
            data.append({"id": name, "object": "model", "created": 0,
                         "owned_by": "beans",
                         "context_length": int(cfg.get("ctx", 4096))})
        return json.dumps({"object": "list", "data": data}).encode()

    def _props_body(self):
        """llama.cpp-shaped /v1/props with the default model's real n_ctx.

        The default is the first ACTIVE model (registry order) that is not an
        embeddings model — Hermes' llamacpp detection reads n_ctx from here, so
        it must reflect the chat model's true context (e.g. 65535), never the
        embeddings model's small window.
        """
        ctx = 4096
        name = ""
        for m in self.scan():
            if m["status"] == "active" and m["gguf"]:
                cfg = m["cfg"] or {}
                if cfg.get("embeddings"):
                    continue
                ctx = int(cfg.get("ctx", 4096))
                name = os.path.basename(m["dir"])
                break
        return json.dumps({
            "default_generation_settings": {"n_ctx": ctx},
            "model_hparams": {"n_ctx_train": ctx},
            "n_ctx": ctx,
            "status": "ok",
            "model": name}).encode()

    def _bridge_port(self, model):
        """Dispatch lookup by directory name AND gguf basename."""
        if not model:
            return None
        for pid, port, name, gguf in self.bridges.values():
            if model in (name, gguf, os.path.basename(name or "")):
                return port
        return None

    def _router_handle(self, conn):
        try:
            req = self._read_request(conn)
            if not req:
                return
            first_line = req.split(b"\r\n", 1)[0].split(b" ")
            method = first_line[0] if first_line else b""
            path = first_line[1].split(b"?")[0] if len(first_line) > 1 else b""
            if method == b"GET" and path in (b"/health",):
                conn.sendall(self._http(200, b'{"status":"ok"}'))
                return
            if method == b"GET" and path in (b"/v1/models", b"/models"):
                conn.sendall(self._http(200, self._models_body()))
                return
            if method == b"GET" and path in (b"/v1/props", b"/props"):
                conn.sendall(self._http(200, self._props_body()))
                return
            # inference: pull `model` from the JSON body, dispatch, splice
            model = None
            try:
                body_start = req.index(b"\r\n\r\n") + 4
                model = json.loads(req[body_start:].decode(
                    "utf-8", errors="replace")).get("model")
            except Exception:
                pass
            port = self._bridge_port(model)
            if port is None:
                body = json.dumps({"error": {"message": "model_not_found"}}).encode()
                conn.sendall(self._http(404, body))
                return
            try:
                backend = socket.create_connection((ROUTER_BACKEND, port), timeout=5)
            except OSError:
                return
            try:
                backend.sendall(req)
                self._splice(conn, backend)
            except OSError:
                pass
            finally:
                try:
                    backend.close()
                except OSError:
                    pass
        except Exception:
            return
        finally:
            try:
                conn.close()
            except OSError:
                pass

    def _router_loop(self):
        host, _, port = self.listen.rpartition(":")
        port = int(port)
        server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        server.bind((host or "0.0.0.0", port))
        server.listen(32)
        self.router_sock = server
        log("router on %s:%d" % (host or "0.0.0.0", port))
        while not self.stop:
            try:
                conn, _ = server.accept()
            except OSError:
                return
            threading.Thread(target=self._router_handle, args=(conn,),
                             daemon=True).start()

    def start_router(self):
        threading.Thread(target=self._router_loop, daemon=True).start()


def main():
    ap = argparse.ArgumentParser(description="beans registry supervisor")
    ap.add_argument("--tree", default=os.environ.get("BEANS_TREE", TREE_DEF))
    ap.add_argument("--config-out", default=os.environ.get("BEANS_CFG_OUT", CFG_OUT_DEF))
    ap.add_argument("--state", default=os.environ.get("BEANS_STATE", STATE_DEF))
    ap.add_argument("--beans-listen", default=os.environ.get("BEANS_LISTEN", ROUTER_DEF))
    args = ap.parse_args()
    daemon = BeansD(args.tree, args.config_out, args.state, args.beans_listen)
    daemon.run()


if __name__ == "__main__":
    main()