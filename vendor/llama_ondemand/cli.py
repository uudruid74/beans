# PROVENANCE (mirror of .provenance/ledger.jsonl; ledger wins)
# written-by: unknown (pre-provenance) | 2026-10-09 | 85e8dc6
# modified-by: codex/gpt-5.6-sol | 2026-10-26 | 4f14980 | provenance block added
# reviewed-by: claude-code/claude-opus-5-5 | 2026-10-26 | APPROVE
# reviewed-by: grok-build/grok-4.7 | 2026-10-26 | APPROVE
# END PROVENANCE
"""llama-ondemand: start llama-server when needed, stop it when idle, never touch a server it didn't start."""
import argparse
import json
import sys

from . import config as config_mod
from .client import ClientError, strip_reasoning
from .manager import LlamaManager, LlamaUnavailable


def _print(obj):
    print(json.dumps(obj, indent=2, sort_keys=True))


def build_parser():
    p = argparse.ArgumentParser(prog="llama-ondemand", description=__doc__)
    p.add_argument("--config", help="config file (default: $LLAMA_ONDEMAND_CONFIG or "
                                    "~/.config/llama-ondemand/config.json)")
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("status", help="is a server up on the port, which model, and did we start it")
    sub.add_parser("probe", help="status plus which configured models fit in free RAM")
    sub.add_parser("models", help="list the configured models")
    s = sub.add_parser("start", help="start (or reuse) a model; the idle watcher stops it later")
    s.add_argument("model", nargs="?", help="model name from the config (default: default_model)")
    s.add_argument("--hold", metavar="JOB", help="keep this model loaded for JOB: no idle stop, no swap")
    s.add_argument("--until", help="end of the --hold: HH:MM or an ISO time (config timezone)")
    r = sub.add_parser("release", help="drop a --hold; the server then idles out normally")
    r.add_argument("job")
    st = sub.add_parser("stop", help="stop the server if this tool started it")
    g = st.add_mutually_exclusive_group()
    g.add_argument("--if-idle", action="store_true", help="only after idle_seconds without use")
    g.add_argument("--if-not-busy", action="store_true", help="only when no request is in flight")
    c = sub.add_parser("chat", help="send one prompt (argument or stdin), starting a server if needed")
    c.add_argument("prompt", nargs="?", help="the prompt; read from stdin when omitted or '-'")
    who = c.add_mutually_exclusive_group()
    who.add_argument("--model", help="model name from the config")
    who.add_argument("--kind", help="task kind, routed by the config's kind_model")
    c.add_argument("--job", help="the job holding the server, when it is held")
    c.add_argument("--system", help="system message")
    c.add_argument("--max-tokens", type=int)
    c.add_argument("--temperature", type=float)
    c.add_argument("--show-reasoning", action="store_true", help="keep <think> blocks and reasoning text")
    c.add_argument("--stop-after", action="store_true", help="stop the server afterwards unless still in use")
    c.add_argument("--json", action="store_true", help="print the full result as JSON")
    return p


def main(argv=None, manager_factory=LlamaManager, stdin=None):
    parser = build_parser()
    a = parser.parse_args(argv)
    if a.cmd == "start" and a.until and not a.hold:
        parser.error("--until needs --hold JOB")
    try:
        cfg = config_mod.load(a.config)
    except config_mod.ConfigError as exc:
        print("llama-ondemand: %s" % exc, file=sys.stderr)
        return 2
    if a.cmd == "models":
        _print({name: spec for name, spec in cfg["models"].items()})
        return 0
    model = getattr(a, "model", None)
    if model is not None and model not in cfg["models"]:
        print("llama-ondemand: unknown model %s (configured: %s)" % (model, ", ".join(sorted(cfg["models"]))),
              file=sys.stderr)
        return 2
    m = manager_factory(cfg)
    try:
        if a.cmd == "status":
            _print(m.status())
        elif a.cmd == "probe":
            _print(m.probe())
        elif a.cmd == "start":
            name = model or config_mod.default_model(cfg)
            if a.hold:
                _print(m.start_job(name, a.hold, a.until))
            else:
                out = m.ensure(None, model=name)
                m.release()
                _print(out)
        elif a.cmd == "release":
            _print(m.release_job(a.job))
        elif a.cmd == "stop":
            if a.if_idle:
                _print(m.stop_if_idle())
            elif a.if_not_busy:
                _print(m.stop_when_done())
            else:
                _print(m.stop())
        elif a.cmd == "chat":
            return _chat(a, cfg, m, stdin)
    except LlamaUnavailable as exc:
        print("llama-ondemand: %s" % exc, file=sys.stderr)
        return 1
    return 0


def _chat(a, cfg, m, stdin):
    prompt = a.prompt
    if prompt in (None, "-"):
        prompt = (stdin or sys.stdin).read()
    if not prompt.strip():
        print("llama-ondemand: empty prompt", file=sys.stderr)
        return 2
    if a.kind and a.kind not in (cfg.get("kind_model") or {}):
        print("llama-ondemand: no model for kind %s in kind_model" % a.kind, file=sys.stderr)
        return 2
    model = a.model or (None if a.kind else config_mod.default_model(cfg))
    messages = ([{"role": "system", "content": a.system}] if a.system else []) + [
        {"role": "user", "content": prompt}]
    options = {}
    if a.max_tokens is not None:
        options["max_tokens"] = a.max_tokens
    if a.temperature is not None:
        options["temperature"] = a.temperature
    m.ensure(a.kind, model=model, job=a.job)
    try:
        result = m.chat(messages, **options)
    except ClientError as exc:
        print("llama-ondemand: %s" % exc, file=sys.stderr)
        return 1
    finally:
        m.release()
        if a.stop_after:
            try:
                m.stop_when_done()
            except LlamaUnavailable:
                pass
    if a.json:
        _print(dict(result.__dict__))
    else:
        print(result.text if a.show_reasoning else strip_reasoning(result.text).strip())
    return 0
