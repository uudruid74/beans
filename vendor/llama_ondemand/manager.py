# PROVENANCE (mirror of .provenance/ledger.jsonl; ledger wins)
# written-by: unknown (pre-provenance) | 2026-10-09 | 85e8dc6
# modified-by: codex/gpt-5.6-sol | 2026-10-26 | 4f14980 | provenance block added
# reviewed-by: claude-code/claude-opus-5-5 | 2026-10-26 | APPROVE_WITH_NITS
# reviewed-by: grok-build/grok-4.7 | 2026-10-26 | APPROVE_WITH_NITS
# END PROVENANCE
"""Client-side manager for an on-demand llama-server.

Every step runs the self-contained helper (host.py) on the model host, either
locally (``sh -c``) or over ssh as the account that owns the models. The helper is
copied into its state dir when its hash differs. Nothing is installed as a
service: the server starts on the first job that needs it and the helper's
watcher stops it after `idle_seconds` without use.
"""
import datetime
import hashlib
import json
import os
import re
import shlex
import subprocess
import time
from pathlib import Path

from .client import ClientError, ContextTooLarge, parse_response
from .host import zone

HELPER = Path(__file__).with_name("host.py")
HELPER_NAME = "llama_ondemand_host.py"


class LlamaUnavailable(Exception):
    """The model host is unreachable or the server is busy; worth retrying later."""


class LlamaRefused(LlamaUnavailable):
    """This job cannot run here right now (no RAM, port held by a foreign model); send it elsewhere."""


_HHMM = re.compile(r"\A([01]?\d|2[0-3]):([0-5]\d)\Z")
_JOB = re.compile(r"\A[A-Za-z0-9][A-Za-z0-9._:-]{0,79}\Z")


def parse_until(text, now, tz=None):
    """--until as an epoch: "HH:MM" (in `tz`, the next time that clock time comes after `now`) or an ISO
    time (no offset means `tz`). `tz` defaults to UTC."""
    tz = tz or datetime.timezone.utc
    text = str(text).strip()
    m = _HHMM.match(text)
    here = datetime.datetime.fromtimestamp(now, tz)
    if m:
        when = here.replace(hour=int(m.group(1)), minute=int(m.group(2)), second=0, microsecond=0)
        if when <= here:
            when += datetime.timedelta(days=1)
        return when.timestamp()
    try:
        when = datetime.datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        raise ValueError("--until %r: use HH:MM or an ISO time such as 2026-10-09T08:00" % text)
    if when.tzinfo is None:
        when = when.replace(tzinfo=tz)
    return when.timestamp()


def _clock(value, what):
    m = _HHMM.match(str(value or ""))
    if not m:
        raise ValueError("config job_hold_window.%s must be HH:MM" % what)
    return int(m.group(1)), int(m.group(2))


def hold_window(cfg, now):
    """Optional sharing rule: a job hold may run only off-hours (weekdays from `weekday_from` to
    `weekday_until`, and weekends, in the configured timezone) and must end by the next weekday
    `weekday_until`. Returns that latest end (epoch), or raises ValueError during weekday hours.
    No job_hold_window in the config: no limit (None)."""
    win = cfg.get("job_hold_window")
    if not win:
        return None
    start, end = _clock(win.get("weekday_from"), "weekday_from"), _clock(win.get("weekday_until"), "weekday_until")
    tz = zone(cfg.get("timezone"))
    here = datetime.datetime.fromtimestamp(now, tz)
    hm = (here.hour, here.minute)
    if here.weekday() < 5 and end <= hm < start:
        raise ValueError("weekday hours (%02d:%02d-%02d:%02d %s) are reserved for shared use; a job hold may "
                         "start at %02d:%02d on weekdays, or any time on weekends"
                         % (end + start + (here.strftime("%Z"),) + start))
    day = here.replace(hour=end[0], minute=end[1], second=0, microsecond=0)
    for _ in range(8):
        if day > here and day.weekday() < 5:
            return day.timestamp()
        day += datetime.timedelta(days=1)
    raise ValueError("no weekday in the next week")  # unreachable


def hold_until(cfg, until_text, now):
    """The hold's end (epoch): --until if given, else the latest the window allows; refused past the window."""
    tz = zone(cfg.get("timezone"))
    latest = hold_window(cfg, now)
    until = parse_until(until_text, now, tz) if until_text else latest
    if until is not None and until <= now:
        raise ValueError("--until %s is not in the future" % datetime.datetime.fromtimestamp(until, tz)
                         .strftime("%Y-%m-%d %H:%M %Z"))
    if latest is not None and until > latest:
        raise ValueError("--until %s runs past the sharing window: release by %s"
                         % tuple(datetime.datetime.fromtimestamp(t, tz).strftime("%a %Y-%m-%d %H:%M %Z")
                                 for t in (until, latest)))
    return until


class SshRunner:
    """Runs one shell command on the model host: ``argv + [command]``.

    For ssh, argv is e.g. ``["ssh", "-o", "BatchMode=yes", "user@host"]``; for the local machine it is
    ``["sh", "-c"]``.
    """

    def __init__(self, argv):
        self.argv = [os.path.expanduser(a) for a in argv]

    def __call__(self, command, stdin="", timeout=60):
        try:
            done = subprocess.run(self.argv + [command], input=stdin, capture_output=True, text=True,
                                  timeout=timeout, check=False)
        except (OSError, subprocess.SubprocessError) as exc:
            return 255, "", "%s: %s" % (type(exc).__name__, exc)
        return done.returncode, done.stdout, done.stderr


def remote_path(path):
    """Shell-quote a remote path, letting a leading ~/ expand to the remote $HOME."""
    if path.startswith("~/"):
        return '"$HOME"/' + shlex.quote(path[2:])
    return shlex.quote(path)


class LlamaManager:
    def __init__(self, cfg, run=None, clock=time.monotonic):
        self.cfg = cfg
        self.run = run if run is not None else SshRunner(cfg.get("runner") or ["sh", "-c"])
        self.clock = clock
        self._helper_ok = False
        self._hold_ids = []
        self.dir = remote_path(self.cfg["state_dir"])

    # ---- remote plumbing ----------------------------------------------
    def _remote_cfg(self):
        return {k: v for k, v in self.cfg.items() if k not in ("runner", "ssh", "host", "kind_model")}

    def _install_helper(self):
        src = HELPER.read_text(encoding="utf-8")
        sha = hashlib.sha256(src.encode("utf-8")).hexdigest()
        cmd = ('mkdir -p {d} && chmod 700 {d} && f={d}/{name} && '
               'if [ "$(sha256sum "$f" 2>/dev/null | cut -d" " -f1)" = {sha} ]; then cat >/dev/null; '
               'else t="$f.$$.tmp" && cat > "$t" && mv -f "$t" "$f"; fi && echo helper_ok').format(
            d=self.dir, sha=sha, name=HELPER_NAME)
        rc, out, err = self.run(cmd, stdin=src, timeout=60)
        if rc != 0 or "helper_ok" not in out:
            raise LlamaUnavailable("cannot install helper on %s: %s" % (self.cfg.get("host"), err.strip()[:200]))
        self._helper_ok = True

    def call(self, action, timeout=60, **payload):
        if not self._helper_ok:
            self._install_helper()
        cmd = "%s %s/%s %s" % (shlex.quote(self.cfg.get("python", "python3")), self.dir, HELPER_NAME,
                               shlex.quote(action))
        rc, out, err = self.run(cmd, stdin=json.dumps(dict(payload, cfg=self._remote_cfg())), timeout=timeout)
        lines = [ln for ln in out.strip().splitlines() if ln.strip()]
        try:
            result = json.loads(lines[-1])
        except (IndexError, ValueError):
            raise LlamaUnavailable("helper %s failed (rc=%s): %s" % (action, rc, err.strip()[:200]))
        if not isinstance(result, dict):
            raise LlamaUnavailable("helper %s returned a non-object" % action)
        return result

    # ---- policy -------------------------------------------------------
    def model_for(self, kind):
        try:
            return self.cfg["kind_model"][kind]
        except KeyError:
            raise LlamaUnavailable("no local model for kind %s" % kind)

    def role(self, model):
        return self.cfg["models"].get(model, {}).get("role", "unknown")

    def status(self):
        return self.call("status")

    def available(self):
        """True when a server answers or a workhorse model fits (a workhorse covers every kind)."""
        try:
            probe = self.call("probe")
        except LlamaUnavailable:
            return False
        held = probe.get("hold")
        if held and self.role(held.get("model")) != "workhorse":
            return False  # a job holds a non-workhorse model: other work waits
        if probe.get("up"):
            return True
        workhorses = [n for n, m in self.cfg["models"].items() if m.get("role") == "workhorse"]
        fits = probe.get("fits") or {}
        return any(fits.get(n) for n in workhorses) or (not workhorses and any(fits.values()))

    def ensure(self, kind, model=None, job=None):
        """Return the status of a server able to take `kind`, starting or swapping one if needed.

        The server is marked in use for `lease_ahead_seconds` so the idle watcher cannot stop it
        between here and the job's first chat.
        """
        want = model if model in self.cfg["models"] else self.model_for(kind)
        st = self.status()
        if st.get("up"):
            loaded = st.get("model")
            covers = loaded == want or (self.role(loaded) == "workhorse" and self.role(want) != "workhorse")
            if covers:
                return self._hold(dict(st, reused=True), model=want, job=job)
            if not st.get("owned"):
                raise LlamaRefused("port %s is held by a foreign %s server; %s needs %s"
                                   % (st.get("port"), loaded, kind, want))
            stopped = self.call("stop", timeout=90, only_if_not_busy=True)
            if stopped.get("reason") == "held":
                raise LlamaUnavailable("server %s; %s needs %s" % (stopped.get("detail"), kind, want))
            if not stopped.get("stopped"):
                raise LlamaUnavailable("server busy with %s; %s needs %s" % (loaded, kind, want))
        st = self.call("start", timeout=float(self.cfg.get("start_timeout_seconds", 240)) + 60, model=want)
        if not st.get("up"):
            error = st.get("error") or "server did not start"
            raise (LlamaRefused if error.startswith("ram:") else LlamaUnavailable)(error)
        return self._hold(st, model=want, job=job)

    def _hold(self, st, model=None, job=None):
        held = self.call("hold", model=model or st.get("model"), job=job,
                         ahead=float(self.cfg.get("lease_ahead_seconds", 120)))
        if not held.get("held"):
            raise LlamaUnavailable("could not hold server: %s" % held.get("reason", "unknown"))
        if held.get("hold_id"):
            self._hold_ids.append(held["hold_id"])
        return held

    def release(self):
        """Drop this manager's newest ensure() hold; other holds (ours or other drivers') stay."""
        if not self._hold_ids:
            return {"holds": None, "released": None}
        out = self.call("release", hold_id=self._hold_ids[-1])
        self._hold_ids.pop()
        return out

    def start_job(self, model, job, until_text=None, now=None):
        """Start (or swap to) `model` and hold it for `job` until --until or the end of the sharing window."""
        if model not in self.cfg["models"]:
            raise LlamaRefused("unknown model %s (the config has %s)" % (model, ", ".join(sorted(self.cfg["models"]))))
        if not isinstance(job, str) or not _JOB.match(job):
            raise LlamaRefused("--hold %r: use letters, digits and ._:- (up to 80)" % (job,))
        try:
            until = hold_until(self.cfg, until_text, time.time() if now is None else now)
        except ValueError as exc:
            raise LlamaRefused(str(exc))
        st = self.call("start", timeout=float(self.cfg.get("start_timeout_seconds", 240)) + 60, model=model, job=job,
                       until=until)
        if not st.get("up"):
            raise LlamaRefused(st.get("error") or "server did not start")
        return st

    def release_job(self, job):
        return self.call("release-job", job=job)

    def stop(self):
        return self.call("stop", timeout=90)

    def stop_if_idle(self):
        return self.call("stop", timeout=90, only_if_idle=True)

    def stop_when_done(self):
        """Stop our server now unless a request is in flight or held (the caller has no work left)."""
        return self.call("stop", timeout=90, only_if_not_busy=True)

    # ---- chat (same shape as client.LlamaClient.chat) -------------------
    def chat(self, messages, **options):
        request = dict(options, messages=messages, stream=False)
        started = self.clock()
        try:
            reply = self.call("chat", timeout=float(self.cfg.get("chat_timeout_seconds", 1200)) + 60, request=request)
        except LlamaUnavailable as exc:
            raise ClientError(str(exc)) from exc
        if reply.get("status") != 200:
            body = str(reply.get("body") or "")[:2000]
            if reply.get("status") == 400 and "context" in body.lower():
                raise ContextTooLarge("prompt exceeds the server context")
            raise ClientError("server answered %s" % reply.get("status"))
        return parse_response(reply.get("body", ""), self.clock() - started)
