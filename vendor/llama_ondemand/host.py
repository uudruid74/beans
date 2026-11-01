#!/usr/bin/env python3
# PROVENANCE (mirror of .provenance/ledger.jsonl; ledger wins)
# written-by: unknown (pre-provenance) | 2026-10-09 | 85e8dc6
# modified-by: codex/gpt-5.6-sol | 2026-10-26 | 4f14980 | redirect SSRF fix and provenance block added
# reviewed-by: claude-code/claude-opus-5-5 | 2026-10-26 | APPROVE_WITH_NITS
# reviewed-by: grok-build/grok-4.7 | 2026-10-26 | APPROVE_WITH_NITS
# END PROVENANCE
"""Runs on the model host as the account that owns the models. Self-contained (stdlib only): the
manager copies this file into its state directory and runs it there, locally or over ssh.

One llama-server on 127.0.0.1:<port>, one model at a time, started on demand and
stopped after it has been idle. A server already answering on the port is reused
and never stopped by us unless our own state file says we started it.

usage: python3 llama_ondemand_host.py ACTION < request.json    (prints one JSON object)
ACTION: version | status | probe | start | touch | hold | release | release-job | chat | stop | watch
request: {"cfg": {...}, "model": NAME, "job": JOB, "until": EPOCH, "request": {...}, "only_if_idle": bool}

A job hold (start with "job") keeps one model loaded for that job until it is released or its
"until" passes: no stop (idle watcher, stop-idle, stop-done, a swap) touches the server meanwhile,
and a start for another model is refused, naming the holder and the expiry.
"""
import datetime
import fcntl
import hashlib
import re
import json
import os
import signal
import subprocess
import sys
import time
import uuid
import urllib.error
import urllib.request

MIB = 1_048_576
STATE = "state.json"
STAMP = "last_used"
HOLDS = "holds"
LOCK = "lock"
JOB_HOLD = "job_hold"
OWNER = "llama-ondemand"
_JOB = re.compile(r"\A[A-Za-z0-9][A-Za-z0-9._:-]{0,79}\Z")


class _RejectRedirects(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def _expand(path):
    return os.path.expanduser(os.path.expandvars(path))


def _mem_available_mib():
    with open("/proc/meminfo", encoding="ascii") as fh:
        for line in fh:
            if line.startswith("MemAvailable:"):
                return int(line.split()[1]) // 1024
    return 0


def _parse_free(text):
    """The "available" column of `free -m` (MiB); 0 when it cannot be read."""
    for line in text.splitlines():
        parts = line.split()
        if parts and parts[0] == "Mem:":
            try:
                return int(parts[6] if len(parts) > 6 else parts[3])
            except (ValueError, IndexError):
                return 0
    return 0


def _free_available_mib():
    try:
        out = subprocess.run(["free", "-m"], capture_output=True, text=True, timeout=10, check=False).stdout
    except (OSError, subprocess.SubprocessError):
        return 0
    return _parse_free(out)


def zone(name=None):
    """The configured time zone (an IANA name such as "Europe/Berlin"); UTC when unset or unknown."""
    if name:
        try:
            from zoneinfo import ZoneInfo
            return ZoneInfo(name)
        except Exception:  # noqa: BLE001 - no tzdata, or an unknown name
            pass
    return datetime.timezone.utc


def _when(epoch, tz=None):
    if epoch is None:
        return None
    return datetime.datetime.fromtimestamp(epoch, tz or datetime.timezone.utc).strftime("%Y-%m-%d %H:%M %Z")


def _pid_alive(pid):
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def _http(method, url, body=None, timeout=5.0):
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), _RejectRedirects())
    data = None if body is None else json.dumps(body).encode("utf-8")
    req = urllib.request.Request(url, data=data, method=method, headers={"Content-Type": "application/json"})
    try:
        with opener.open(req, timeout=timeout) as resp:
            return resp.status, resp.read(4 * MIB + 1).decode("utf-8", "replace")
    except urllib.error.HTTPError as exc:
        if 300 <= exc.code < 400:
            return 0, "redirect refused (HTTP %d)" % exc.code
        return exc.code, exc.read(65536).decode("utf-8", "replace")
    except Exception:
        return 0, ""


def _proc_cmdline(pid):
    try:
        with open("/proc/%d/cmdline" % pid, "rb") as fh:
            return fh.read().replace(b"\0", b" ").decode("utf-8", "replace")
    except OSError:
        return ""


def _proc_starttime(pid):
    """Field 22 of /proc/<pid>/stat (start time in clock ticks since boot): tells a reused PID apart."""
    try:
        with open("/proc/%d/stat" % pid, encoding="ascii", errors="replace") as fh:
            return int(fh.read().rsplit(")", 1)[1].split()[19])
    except (OSError, ValueError, IndexError):
        return None


def _spawn(argv, env, log_path):
    log = open(log_path, "ab")
    try:
        return subprocess.Popen(argv, env=env, stdin=subprocess.DEVNULL, stdout=log, stderr=log,
                                start_new_session=True, close_fds=True).pid
    finally:
        log.close()


class Host:
    def __init__(self, cfg, clock=time.time, sleep=time.sleep, http=_http, spawn=_spawn,
                 pid_alive=_pid_alive, kill=os.kill, mem_available_mib=_mem_available_mib, self_path=None,
                 proc_cmdline=_proc_cmdline, free_mib=_free_available_mib, proc_starttime=_proc_starttime):
        self.cfg = cfg
        self.port = int(cfg["port"])
        self.base = "http://127.0.0.1:%d" % self.port
        self.dir = _expand(cfg["state_dir"])
        self.tz = zone(cfg.get("timezone"))
        self.clock, self.sleep, self.http, self.spawn = clock, sleep, http, spawn
        self.pid_alive, self.kill, self.mem = pid_alive, kill, mem_available_mib
        self.proc_cmdline = proc_cmdline
        self.free_mib = free_mib
        self.proc_starttime = proc_starttime
        self.self_path = self_path or os.path.abspath(__file__)
        os.makedirs(self.dir, mode=0o700, exist_ok=True)

    # ---- state ---------------------------------------------------------
    def _path(self, name):
        return os.path.join(self.dir, name)

    def read_state(self):
        try:
            with open(self._path(STATE), encoding="utf-8") as fh:
                state = json.load(fh)
            return state if isinstance(state, dict) else {}
        except (OSError, ValueError):
            return {}

    def write_state(self, state):
        tmp = self._path(STATE + ".tmp")
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(state, fh)
        os.replace(tmp, self._path(STATE))

    def touch(self, ahead=0.0):
        """Mark the server used now; `ahead` marks a request in flight until that many seconds from now."""
        now = self.clock() + float(ahead)
        with open(self._path(STAMP), "a", encoding="ascii"):
            pass
        os.utime(self._path(STAMP), (now, now))
        return {"touched": now}

    def _hold_list(self):
        """Open holds as {id, exp}; a holder that crashed simply lets its entry expire."""
        try:
            with open(self._path(HOLDS), encoding="ascii") as fh:
                items = json.load(fh)
        except (OSError, ValueError):
            return []
        if not isinstance(items, list):
            return []
        now = self.clock()
        out = []
        for item in items:
            if not isinstance(item, dict):
                continue
            hid, exp = item.get("id"), item.get("exp")
            if isinstance(hid, str) and isinstance(exp, (int, float)) and exp > now:
                out.append({"id": hid, "exp": float(exp)})
        return out

    def _holds(self):
        return len(self._hold_list())

    def _write_holds(self, items):
        tmp = self._path(HOLDS + ".tmp")
        with open(tmp, "w", encoding="ascii") as fh:
            json.dump(items, fh)
        os.replace(tmp, self._path(HOLDS))

    def _set_holds(self, n):
        if n == 0:
            self._write_holds([])

    def hold(self, model=None, ahead=120.0, job=None):
        """Atomically: server must be healthy (and preferably matching `model`); take a hold. While a job holds
        the server, only that job's requests may take one."""
        lock = self._lock()
        try:
            held = self.job_hold()
            if held and job != held["job"]:
                return {"held": False, "reason": "%s; pass its job to use it" % self._held_by(held)}
            if not self.healthy():
                return {"held": False, "reason": "not_healthy"}
            loaded = self.loaded_model()
            if model is not None and loaded not in (None, model) and not (
                    self.cfg["models"].get(loaded, {}).get("role") == "workhorse"
                    and self.cfg["models"].get(model, {}).get("role") != "workhorse"):
                return {"held": False, "reason": "wrong_model", "model": loaded}
            hid = uuid.uuid4().hex
            items = self._hold_list() + [{"id": hid, "exp": self.clock() + float(ahead)}]
            self._write_holds(items)
            self.touch()
            return dict(self.status(), held=True, hold_id=hid, holds=len(items), reused=True)
        finally:
            lock.close()

    def release(self, hold_id=None):
        """Drop the caller's hold by id. An unknown or expired id is a no-op."""
        lock = self._lock()
        try:
            items = self._hold_list()
            if hold_id is not None:
                items = [h for h in items if h["id"] != hold_id]
            self._write_holds(items)
            self.touch()
            return {"holds": len(items), "released": hold_id}
        finally:
            lock.close()

    # ---- job holds -----------------------------------------------------
    def _raw_job_hold(self):
        try:
            with open(self._path(JOB_HOLD), encoding="utf-8") as fh:
                h = json.load(fh)
        except (OSError, ValueError):
            return None
        if not isinstance(h, dict) or not isinstance(h.get("job"), str) or not isinstance(h.get("model"), str):
            return None
        until = h.get("until")
        if until is not None and (isinstance(until, bool) or not isinstance(until, (int, float))):
            return None
        return h

    def job_hold(self):
        """The active job hold {job, model, until, since}, or None (none, malformed, or past its until)."""
        h = self._raw_job_hold()
        if h is None or (h.get("until") is not None and h["until"] <= self.clock()):
            return None
        return self._show(h)

    def _show(self, h):
        return dict(h, until_local=_when(h.get("until"), self.tz) or "no expiry")

    def _held_by(self, h):
        return "held by job %s for %s until %s" % (h["job"], h["model"], h["until_local"])

    def release_job(self, job):
        """Drop job's hold. The server stays up; the idle watcher, stop-idle or stop-done stop it later."""
        lock = self._lock()
        try:
            h = self._raw_job_hold()
            if h is None or h["job"] != job:
                active = self.job_hold()
                return {"released": None, "reason": "job %s holds nothing" % job,
                        "hold": active}
            os.remove(self._path(JOB_HOLD))
            return {"released": job, "model": h["model"], "server": self.status()}
        finally:
            lock.close()

    def last_used(self):
        try:
            return os.stat(self._path(STAMP)).st_mtime
        except OSError:
            return 0.0

    def _lock(self):
        fh = open(self._path(LOCK), "a")
        fcntl.flock(fh, fcntl.LOCK_EX)
        return fh

    # ---- server facts --------------------------------------------------
    def healthy(self):
        code, body = self.http("GET", self.base + "/health")
        if code != 200:
            return False
        try:
            return json.loads(body).get("status", "ok") == "ok"
        except (ValueError, AttributeError):
            return True

    def loaded_model(self):
        code, body = self.http("GET", self.base + "/v1/models")
        if code != 200:
            return None
        try:
            ident = str(json.loads(body)["data"][0]["id"])
        except (ValueError, KeyError, IndexError, TypeError):
            return None
        for name, spec in self.cfg["models"].items():
            if ident == name or os.path.basename(ident) == spec["file"]:
                return name
        return "unknown:" + os.path.basename(ident)[:80]

    def busy(self):
        """True when a slot is processing. An answer we cannot read counts as busy, so no stop or swap
        acts on a guess; a server that does not answer at all is not busy (nothing is in flight)."""
        code, body = self.http("GET", self.base + "/slots")
        if code == 0:
            return False
        if code != 200:
            return True
        try:
            slots = json.loads(body)
            if not isinstance(slots, list):
                return True
            return any(not isinstance(slot, dict) or bool(slot.get("is_processing")) for slot in slots)
        except (ValueError, AttributeError, TypeError):
            return True

    def _pid_is_ours(self, pid, model=None, started=None):
        """True when /proc/<pid>/cmdline is our llama-server on this port (guards PID reuse). With `model`, the
        command line must also carry that model's --alias and weights path, as our start wrote them; with
        `started`, the process start time must be the one recorded at spawn."""
        if not isinstance(pid, int) or not self.pid_alive(pid):
            return False
        if started is not None and self.proc_starttime(pid) != started:
            return False
        cmd = self.proc_cmdline(pid) + " "
        if "llama-server" not in cmd or ("--port %d " % self.port) not in cmd:
            return False
        if model is None:
            return True
        spec = self.cfg["models"].get(model)
        if not isinstance(spec, dict):
            return False
        path = os.path.join(_expand(self.cfg["weights_dir"]), spec["file"])
        return ("--alias %s " % model) in cmd and (" -m %s " % path) in cmd

    def owned(self, state=None):
        state = self.read_state() if state is None else state
        pid = state.get("pid")
        return (state.get("owner") == OWNER and state.get("status") == "running"
                and self._pid_is_ours(pid, state.get("model"), state.get("pid_start")))

    def status(self):
        state = self.read_state()
        up = self.healthy()
        owned = self.owned(state)
        last = self.last_used()
        raw = self._raw_job_hold()
        held = self.job_hold()
        return {"up": up, "port": self.port, "owned": owned, "pid": state.get("pid") if owned else None,
                "model": self.loaded_model() if up else (state.get("model") if owned else None),
                "idle_s": round(self.clock() - last, 1) if last else None,
                "hold": held, "hold_expired": self._show(raw) if raw and not held else None}

    def _fits(self, name):
        path = os.path.join(_expand(self.cfg["weights_dir"]), self.cfg["models"][name]["file"])
        try:
            size = os.stat(path).st_size
        except OSError:
            return False, path, 0, 0
        # beans patch (2026-11-01): with full GPU offload (--n-gpu-layers) the weights
        # live in VRAM; host RAM only carries load-time streaming + KV slack, so the
        # weights-file size would falsely refuse on RAM-lean hosts. Slack is configurable
        # via gpu_ram_slack_mib (default 1024).
        if any(a == "--n-gpu-layers" for a in self.cfg.get("server_args", [])):
            need = int(self.cfg.get("gpu_ram_slack_mib", 1024))
        else:
            need = size // MIB + int(self.cfg.get("headroom_mib", 3072))
        return self.mem() >= need, path, size, need

    def probe(self):
        out = self.status()
        out["fits"] = {name: self._fits(name)[0] for name in self.cfg["models"]}
        return out

    # ---- lifecycle -----------------------------------------------------
    def _wait_healthy(self, pid, timeout):
        deadline = self.clock() + timeout
        while self.clock() < deadline:
            if self.healthy():
                return True
            if pid is not None and not self.pid_alive(pid):
                return False
            self.sleep(2)
        return self.healthy()

    def start(self, name, job=None, until=None):
        """Start (or reuse) `name`. With `job`, also hold it for that job until `until` (epoch; None = until
        released): a held start swaps out a different model only when this tool started that server, and
        refuses when `free -m` shows less than min_free_mib available."""
        if name not in self.cfg["models"]:
            return {"up": False, "error": "unknown model %s" % name}
        if job is not None and (not isinstance(job, str) or not _JOB.match(job)):
            return {"up": False, "error": "bad job name %r" % (job,)}
        if until is not None and (isinstance(until, bool) or not isinstance(until, (int, float))
                                  or until <= self.clock()):
            return {"up": False, "error": "until must be a time in the future"}
        lock = self._lock()
        try:
            held = self.job_hold()
            # The holding job may move its own hold to another model (e.g. a model to its
            # no-reasoning profile); anyone else waits for the hold to end or be released.
            if held and (job != held["job"] if job is not None else held["model"] != name):
                return {"up": False, "hold": held,
                        "error": "held: the server is %s; %s for %s waits until then or until "
                                 "`llama-ondemand release %s`" % (self._held_by(held), job or "this start", name,
                                                                  held["job"])}
            if job is not None:  # checked first: a refused start must not have stopped anything
                free = self.free_mib()
                floor = int(self.cfg.get("min_free_mib", 4096))
                if free < floor:
                    return {"up": False, "error": "ram: free -m shows %d MiB available; a held start needs %d"
                                                  % (free, floor)}
            state = self.read_state()
            if self.healthy():
                loaded = self.loaded_model()
                if job is None or loaded == name:
                    if self.owned(state):
                        self.touch()
                    if job is not None:
                        self._take_job_hold(job, name, until)
                    return dict(self.status(), reused=True)
                if not self.owned(state):
                    return {"up": False, "error": "swap refused: port %d serves %s from a server llama-ondemand did "
                                                  "not start; it is never stopped by us" % (self.port, loaded)}
                if self.busy() or self._holds() > 0 or self.last_used() > self.clock():
                    return {"up": False, "error": "swap refused: %s is busy with a request; try again" % loaded}
                if held:  # the holder moving its hold: check the target before stopping the held server
                    why = self._swap_preflight(name, state.get("model"))
                    if why:
                        return {"up": False, "hold": held, "error": "swap refused: %s; %s keeps running" % (why, loaded)}
                self._terminate(state["pid"], state.get("model"), state.get("pid_start"))
                state.update(status="stopped", ended=self.clock())
                self.write_state(state)
                self._set_holds(0)
                if held:
                    out = self._launch(name)
                    if not out["up"]:
                        return self._restore_after_failed_swap(held, out["error"])
                    self._take_job_hold(job, name, until)
                    return dict(self.status(), reused=False)
            elif self.owned(state):  # ours, still loading
                ok = self._wait_healthy(state["pid"], float(self.cfg.get("start_timeout_seconds", 240)))
                if ok and job is not None and self.loaded_model() == name:
                    self._take_job_hold(job, name, until)
                return dict(self.status(), reused=True, error=None if ok else "server did not become healthy")
            out = self._launch(name)
            if not out["up"]:
                return out
            if job is not None:
                self._take_job_hold(job, name, until)
            return dict(self.status(), reused=False)
        finally:
            lock.close()

    def _swap_preflight(self, name, old):
        """Why a held swap from `old` to `name` would fail before it stops anything, else None. RAM counts the
        memory the old server frees (its weights file size)."""
        fits, path, size, need = self._fits(name)
        if not size:
            return "weights missing for %s" % name
        if fits:
            return None
        freed = 0
        if old in self.cfg["models"]:
            old_size = self._fits(old)[2]
            freed = old_size // MIB
        if self.mem() + freed < need:
            return "ram: need %d MiB, available %d MiB plus %d MiB from %s" % (need, self.mem(), freed, old)
        return None

    def _restore_after_failed_swap(self, held, error):
        """The holder's swap stopped its server but the new model failed: bring the held model back. The hold
        is left as it was (it still names the held model), so no other job takes the server meanwhile."""
        back = self._launch(held["model"])
        if back["up"]:
            return dict(self.status(), up=False, hold=held,
                        error="swap failed: %s; restored %s (hold unchanged)" % (error, held["model"]))
        return {"up": False, "hold": self.job_hold(),
                "error": "swap failed: %s; restoring %s also failed: %s. The hold for %s is kept; retry with "
                         "`llama-ondemand start MODEL --hold %s`" % (error, held["model"], back["error"],
                                                                     held["job"], held["job"])}

    def _launch(self, name):
        """Start `name` on the port (nothing may be running there); {"up": True} or {"up": False, "error"}.
        Never raises: an exception part way (spawn, state or config I/O, the watcher) stops whatever server it
        started, so a holder's swap can always fall back to restoring the held model."""
        started = []
        try:
            return self._launch_steps(name, started)
        except Exception as exc:  # noqa: BLE001 - reported to the caller, which restores or reports
            for pid, pid_start in started:
                try:
                    if pid_start is not None:  # the start time read right after spawn (PID reuse)
                        self._terminate(pid, name, pid_start)
                except Exception:  # noqa: BLE001
                    pass
                try:
                    self.write_state({"owner": OWNER, "status": "failed", "pid": pid, "model": name,
                                      "port": self.port, "ended": self.clock()})
                except Exception:  # noqa: BLE001
                    pass
            return {"up": False, "error": "start of %s failed: %s: %s" % (name, type(exc).__name__, exc)}

    def _launch_steps(self, name, started):
        fits, path, size, need = self._fits(name)
        if not size:
            return {"up": False, "error": "weights missing for %s" % name}
        if not fits:
            return {"up": False, "error": "ram: need %d MiB, available %d MiB" % (need, self.mem())}
        cfg = self.cfg
        threads = str(int(cfg.get("threads", 6)))
        launcher = [_expand(str(a)).replace("{weights_bytes}", str(size)) for a in cfg.get("launcher") or []]
        server = [_expand(cfg.get("server_bin") or "llama-server"),
                  "-m", path, "--host", "127.0.0.1", "--port", str(self.port), "-t", threads, "-tb", threads,
                  "-c", str(int(cfg.get("ctx", 4096))), "-np", "1"]
        budget = self._reasoning_budget(name)
        if budget is not None:
            server += ["--reasoning-budget", str(budget)]
        server += [str(a) for a in cfg.get("server_args") or []] + ["--alias", name]
        argv = launcher + server
        env = dict(os.environ)
        if cfg.get("lib_dir"):
            env["LD_LIBRARY_PATH"] = _expand(cfg["lib_dir"])
        pid = self.spawn(argv, env, self._path("server.log"))
        pid_start = self.proc_starttime(pid)
        started.append((pid, pid_start))
        # A launcher must exec into llama-server (as systemd-run --scope, choom, nice and taskset
        # do), so the server's /proc/<pid>/cmdline is argv minus the launcher. Recorded so a
        # client can cross-check the running server's flags against what we started.
        self.write_state({"owner": OWNER, "status": "running", "pid": pid, "model": name,
                          "pid_start": pid_start, "port": self.port, "started": self.clock(),
                          "server_argv": server})
        self._set_holds(0)
        with open(self._path("config.json"), "w", encoding="utf-8") as fh:
            json.dump(cfg, fh)
        self.touch()
        self.spawn([sys.executable, self.self_path, "watch", "--config", self._path("config.json")],
                   dict(os.environ), self._path("watch.log"))
        ok = self._wait_healthy(pid, float(cfg.get("start_timeout_seconds", 240)))
        if not ok:
            self._terminate(pid, name, self.read_state().get("pid_start"))
            self.write_state({"owner": OWNER, "status": "failed", "pid": pid, "model": name,
                              "port": self.port, "ended": self.clock()})
            return {"up": False, "error": "server did not become healthy (see server.log)"}
        return {"up": True}

    def _reasoning_budget(self, name):
        """A model profile's own reasoning_budget (e.g. 0 for a -noreason profile), else the global one."""
        spec = self.cfg["models"][name]
        value = spec.get("reasoning_budget", self.cfg.get("reasoning_budget"))
        return None if value is None else int(value)

    def _take_job_hold(self, job, model, until):
        tmp = self._path(JOB_HOLD + ".tmp")
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump({"job": job, "model": model, "until": until, "since": self.clock()}, fh)
        os.replace(tmp, self._path(JOB_HOLD))

    def _terminate(self, pid, model=None, started=None):
        """SIGTERM, then SIGKILL, our server; every signal re-checks the full identity (PID reuse)."""
        def ours():
            return self._pid_is_ours(pid, model, started)
        if not ours():
            return
        try:
            self.kill(pid, signal.SIGTERM)
        except (ProcessLookupError, PermissionError):
            return
        for _ in range(20):
            if not self.pid_alive(pid) or not ours():
                return
            self.sleep(1)
        if not ours():
            return
        try:
            self.kill(pid, signal.SIGKILL)
        except (ProcessLookupError, PermissionError):
            pass

    def stop(self, only_if_idle=False, only_if_not_busy=False):
        lock = self._lock()
        try:
            state = self.read_state()
            if not self.owned(state):
                return {"stopped": False, "reason": "not_running" if not self.healthy() else "not_owned"}
            held = self.job_hold()
            if held:  # a job hold outranks every stop; release it, or let its until pass
                return {"stopped": False, "reason": "held", "hold": held, "detail": self._held_by(held)}
            if only_if_not_busy and (self.busy() or self._holds() > 0 or self.last_used() > self.clock()):
                return {"stopped": False, "reason": "busy", "holds": self._holds()}
            if only_if_idle:
                idle = self.clock() - self.last_used()
                if self.busy() or self._holds() > 0:
                    self.touch()
                    return {"stopped": False, "reason": "busy", "holds": self._holds()}
                if idle < float(self.cfg.get("idle_seconds", 300)):
                    return {"stopped": False, "reason": "active", "idle_s": round(idle, 1)}
            self._terminate(state["pid"], state.get("model"), state.get("pid_start"))
            state.update(status="stopped", ended=self.clock())
            self.write_state(state)
            self._set_holds(0)
            return {"stopped": True, "model": state.get("model")}
        finally:
            lock.close()

    def watch(self, max_loops=None):
        """Stay alive next to our server; stop it once idle. Exits when the server is gone."""
        loops = 0
        poll = float(self.cfg.get("watch_poll_seconds", 15))
        mine = self.read_state().get("pid")
        while max_loops is None or loops < max_loops:
            loops += 1
            state = self.read_state()
            if state.get("pid") != mine:
                return {"watch": "replaced", "loops": loops}
            if not self.owned(state):
                return {"watch": "server_gone", "loops": loops}
            if self.stop(only_if_idle=True)["stopped"]:
                return {"watch": "stopped_idle", "loops": loops}
            self.sleep(poll)
        return {"watch": "max_loops", "loops": loops}

    def chat(self, request):
        if not isinstance(request, dict):
            return {"status": 0, "body": "", "error": "request must be an object"}
        request = dict(request, stream=False)
        timeout = float(self.cfg.get("chat_timeout_seconds", 1200))
        lock = self._lock()
        try:
            if not self.healthy():
                return {"status": 0, "body": "", "error": "server not healthy"}
            self.touch(ahead=timeout)  # under the lock so idle-stop cannot race the stamp
        finally:
            lock.close()
        try:
            code, body = self.http("POST", self.base + "/v1/chat/completions", request, timeout=timeout)
        finally:
            lock = self._lock()
            try:
                self.touch()
            finally:
                lock.close()
        return {"status": code, "body": body}


def version():
    with open(os.path.abspath(__file__), "rb") as fh:
        return {"sha256": hashlib.sha256(fh.read()).hexdigest()}


def main(argv=None, stdin=None, host_factory=Host):
    argv = sys.argv[1:] if argv is None else argv
    if not argv:
        print(json.dumps({"error": "action required"}))
        return 2
    action = argv[0]
    if action == "version":
        print(json.dumps(version()))
        return 0
    if action == "watch" and len(argv) == 3 and argv[1] == "--config":
        with open(argv[2], encoding="utf-8") as fh:
            payload = {"cfg": json.load(fh)}
    else:
        raw = (stdin or sys.stdin).read(8 * MIB)
        try:
            payload = json.loads(raw or "{}")
        except ValueError:
            print(json.dumps({"error": "request is not JSON"}))
            return 2
    host = host_factory(payload["cfg"])
    if action == "status":
        out = host.status()
    elif action == "probe":
        out = host.probe()
    elif action == "start":
        out = host.start(payload.get("model"), job=payload.get("job"), until=payload.get("until"))
    elif action == "release-job":
        out = host.release_job(payload.get("job"))
    elif action == "touch":
        ahead = payload.get("ahead", 0)
        ahead = min(max(float(ahead), 0.0), float(host.cfg.get("chat_timeout_seconds", 1200))) \
            if isinstance(ahead, (int, float)) else 0.0
        lock = host._lock()
        try:
            out = host.touch(ahead=ahead) if host.owned() or host.healthy() else {"touched": None}
        finally:
            lock.close()
    elif action == "hold":
        ahead = payload.get("ahead", 120)
        ahead = min(max(float(ahead), 0.0), float(host.cfg.get("chat_timeout_seconds", 1200))) \
            if isinstance(ahead, (int, float)) else 120.0
        out = host.hold(model=payload.get("model"), ahead=ahead, job=payload.get("job"))
    elif action == "release":
        out = host.release(hold_id=payload.get("hold_id"))
    elif action == "chat":
        out = host.chat(payload.get("request"))
    elif action == "stop":
        out = host.stop(only_if_idle=bool(payload.get("only_if_idle")),
                        only_if_not_busy=bool(payload.get("only_if_not_busy")))
    elif action == "watch":
        out = host.watch()
    else:
        out = {"error": "unknown action %s" % action}
    print(json.dumps(out))
    return 0 if "error" not in out or out.get("error") is None else 1


if __name__ == "__main__":
    sys.exit(main())
