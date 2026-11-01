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
import struct
import subprocess
import sys
import time

TREE_DEF = "/beans"
CFG_OUT_DEF = "/config"
STATE_DEF = "/var/lib/beans/state.json"
OLLA_BIN_DEF = "/usr/local/bin/olla"
BEGIN_PORT = 8081
DEBOUNCE_S = 1.5
RECONCILE_MAX = 12
SUF_DISABLED = "-disabled"
SUF_PENDING = "-pending"
PID_FILE = ".download.pid"
FAIL_FILE = ".download.failed"

ALLOWED = {"name", "file", "url", "sha256", "ctx", "threads", "server_args",
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
    def __init__(self, tree, cfg_out, state_file, olla_bin):
        self.tree = tree
        self.cfg_out = cfg_out
        self.state_file = state_file
        self.olla_bin = olla_bin
        self.state = self.load_state()
        self.olla_pid = None
        self.olla_cfg_sha = None
        self.olla_cfg_path = os.path.join(cfg_out, "config.yaml")
        self.bridges = {}   # dir -> (pid, port, name)
        self.downloads = {}  # pending dir path -> pid
        self.inot = Inotify()
        self.stop = False

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
            "server_args": list(cfg.get("server_args", [])),
            "headroom_mib": 64,
            "min_free_mib": 64,
            "idle_seconds": int(cfg.get("idle_seconds", 300)),
            "watch_poll_seconds": 15,
            "start_timeout_seconds": 240,
            "lease_ahead_seconds": 120,
            "default_model": cfg.get("name") or os.path.basename(m["dir"]),
            "vram_headroom_mib": int(cfg.get("vram_headroom_mib", 800)),
            "models": {cfg.get("name") or os.path.basename(m["dir"]):
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
             "--model", m["cfg"].get("name") or name, "--config", cfg_path])
        self.bridges[name] = (proc.pid, port, m["cfg"].get("name") or name)
        log("bridge %s on :%d (pid %d)" % (name, port, proc.pid))

    def kill_model(self, name_path):
        """Kill a model's bridge + its llama-server (by unique gguf path)."""
        base = os.path.basename(name_path.rstrip("/"))
        pid, port, _ = self.bridges.pop(base, (None, None, None))
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

    # ------------------------------------------------------------- Olla config
    def olla_config(self, actives):
        endpoints = []
        aliases = {}
        for m in actives:
            port = self.port_for(m["dir"])
            name = os.path.basename(m["dir"])
            endpoints.append({"url": "http://127.0.0.1:%d" % port,
                              "name": "local-%s" % name, "type": "llamacpp", "priority": 95})
            model_name = m["cfg"].get("name") or name
            aliases[model_name] = [os.path.join(m["dir"], m["cfg"]["file"])]
        return {
            "server": {"host": "0.0.0.0", "port": 11434},
            "proxy": {"engine": "olla", "load_balancer": "least-connections"},
            "discovery": {"static": {"endpoints": endpoints}},
            "logging": {"level": "info", "format": "text"},
            "model_aliases": aliases,
        }

    def write_olla_cfg(self, actives):
        cfg = self.olla_config(actives)
        text = json.dumps(cfg, indent=2)
        sha = hashlib.sha256(text.encode()).hexdigest()
        if sha == self.olla_cfg_sha:
            return False
        os.makedirs(self.cfg_out, exist_ok=True)
        tmp = self.olla_cfg_path + ".tmp"
        with io.open(tmp, "w", encoding="utf-8") as f:
            f.write(text + "\n")
        os.replace(tmp, self.olla_cfg_path)
        self.olla_cfg_sha = sha
        return True

    def ensure_olla(self, restart=False):
        if restart and self.olla_pid:
            try:
                os.kill(self.olla_pid, signal.SIGTERM)
                time.sleep(1.0)
            except OSError:
                pass
            self.olla_pid = None
        if self.olla_pid is None:
            env = dict(os.environ)
            env["OLLA_CONFIG_FILE"] = self.olla_cfg_path
            proc = subprocess.Popen([self.olla_bin], env=env)
            self.olla_pid = proc.pid
            log("olla up (pid %d)" % proc.pid)

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
            # 5. Olla config — re-scan: renames above may have changed the active set
            actives = [m for m in self.scan() if m["status"] == "active"]
            if self.write_olla_cfg(actives):
                self.ensure_olla(restart=True)
                log("olla config updated")
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
        # Ensure olla comes up with whatever's active at boot
        self.reconcile()
        self.ensure_olla()
        self.inot.reset(self.tree)
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
            if self.olla_pid and not self._proc_alive(self.olla_pid):
                log("olla died — restarting")
                self.olla_pid = None
                self.reconcile()
        self.shutdown()

    def shutdown(self):
        if self.stop:
            return
        self.stop = True
        log("shutting down")
        for base in list(self.bridges):
            self.kill_model(os.path.join(self.tree, base))
        if self.olla_pid:
            try:
                os.kill(self.olla_pid, signal.SIGTERM)
            except OSError:
                pass
        for dpath, pid in list(self.downloads.items()):
            try:
                os.kill(pid, signal.SIGTERM)
            except OSError:
                pass
        sys.exit(0)


def main():
    ap = argparse.ArgumentParser(description="beans registry supervisor")
    ap.add_argument("--tree", default=os.environ.get("BEANS_TREE", TREE_DEF))
    ap.add_argument("--config-out", default=os.environ.get("BEANS_CFG_OUT", CFG_OUT_DEF))
    ap.add_argument("--state", default=os.environ.get("BEANS_STATE", STATE_DEF))
    ap.add_argument("--olla-bin", default=os.environ.get("BEANS_OLLA", OLLA_BIN_DEF))
    args = ap.parse_args()
    daemon = BeansD(args.tree, args.config_out, args.state, args.olla_bin)
    daemon.run()


if __name__ == "__main__":
    main()