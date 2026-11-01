# PROVENANCE (mirror of .provenance/ledger.jsonl; ledger wins)
# written-by: unknown (pre-provenance) | 2026-10-09 | 85e8dc6
# modified-by: codex/gpt-5.6-sol | 2026-10-26 | 4f14980 | provenance block added
# reviewed-by: claude-code/claude-opus-5-5 | 2026-10-26 | APPROVE
# reviewed-by: grok-build/grok-4.7 | 2026-10-26 | APPROVE
# END PROVENANCE
"""Configuration: one JSON file plus a few environment overrides.

Lookup order for the file: an explicit path, then ``$LLAMA_ONDEMAND_CONFIG``, then
``~/.config/llama-ondemand/config.json``.

Environment overrides (applied after the file):

- ``LLAMA_ONDEMAND_SSH``: ``user@host`` to manage a remote host over ssh, or ``local`` to force local mode.
- ``LLAMA_ONDEMAND_PORT``: the llama-server port on the model host.
- ``LLAMA_ONDEMAND_STATE_DIR``: the helper's state directory on the model host.
"""
import json
import math
import os
import re
from pathlib import Path

ENV_CONFIG = "LLAMA_ONDEMAND_CONFIG"
DEFAULT_PATH = "~/.config/llama-ondemand/config.json"
SSH_OPTIONS = ["-o", "BatchMode=yes", "-o", "ConnectTimeout=15"]

DEFAULTS = {
    "port": 8091,
    "state_dir": "~/.local/state/llama-ondemand",
    "server_bin": "llama-server",
    "ctx": 4096,
    "threads": 4,
    "headroom_mib": 2048,
    "min_free_mib": 2048,
    "idle_seconds": 300,
    "watch_poll_seconds": 15,
    "start_timeout_seconds": 240,
    "chat_timeout_seconds": 1200,
    "lease_ahead_seconds": 120,
}


KNOWN_KEYS = frozenset(DEFAULTS) | {
    "ssh", "server_bin", "lib_dir", "launcher", "server_args", "weights_dir", "models", "kind_model",
    "default_model", "reasoning_budget", "timezone", "job_hold_window", "python", "runner",
}


# Flags the tool sets itself. Overriding them from server_args could expose the
# server beyond loopback, load a different model than the one named, or break
# the ownership check that keeps us from stopping someone else's server.
RESERVED_SERVER_FLAGS = frozenset({
    "-m", "--model", "-mu", "--model-url", "-hf", "-hfr", "--hf-repo", "-hff", "--hf-file", "-dr",
    "--docker-repo", "--host", "--port", "-a", "--alias", "--no-slots", "--path", "--api-prefix",
})
NUMBERS = {  # key: (type, minimum)
    "ctx": (int, 1), "threads": (int, 1), "headroom_mib": (int, 0), "min_free_mib": (int, 0),
    "idle_seconds": (float, 1), "watch_poll_seconds": (float, 1), "start_timeout_seconds": (float, 1),
    "chat_timeout_seconds": (float, 1), "lease_ahead_seconds": (float, 1), "reasoning_budget": (int, -1),
}
MODEL_NAME = re.compile(r"\A[A-Za-z0-9][A-Za-z0-9._:-]{0,79}\Z")


def _number_ok(value, kind, minimum):
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return False
    if kind is int and not isinstance(value, int):
        return False
    return math.isfinite(value) and value >= minimum


class ConfigError(ValueError):
    """The configuration file is missing, unreadable or invalid."""


def config_path(path=None, environ=None):
    env = os.environ if environ is None else environ
    return Path(os.path.expanduser(path or env.get(ENV_CONFIG) or DEFAULT_PATH))


def runner_argv(ssh):
    """The command prefix that runs one shell command on the model host.

    ``None``, ``""`` or ``"local"``: this machine (``sh -c``). A string: ``ssh <options> <target>``.
    A list: used as given (for custom ssh options or another transport).
    """
    if ssh in (None, "", "local"):
        return ["sh", "-c"]
    if isinstance(ssh, str):
        return ["ssh", *SSH_OPTIONS, ssh]
    if isinstance(ssh, list) and ssh and all(isinstance(a, str) and a for a in ssh):
        return list(ssh)
    raise ConfigError("ssh must be null, \"local\", \"user@host\" or a list of strings")


def validate(cfg):
    unknown = sorted(set(cfg) - KNOWN_KEYS)
    if unknown:
        raise ConfigError("unknown config key(s): %s" % ", ".join(unknown))
    models = cfg.get("models")
    if not isinstance(models, dict) or not models:
        raise ConfigError("models must map at least one model name to {\"file\": ...}")
    for name, spec in models.items():
        if not MODEL_NAME.match(name):
            raise ConfigError("model name %r: use letters, digits and ._:- (up to 80)" % name)
        if not isinstance(spec, dict) or not isinstance(spec.get("file"), str) or not spec["file"]:
            raise ConfigError("models.%s needs a \"file\" (the GGUF file name under weights_dir)" % name)
        if "/" in spec["file"]:
            raise ConfigError("models.%s.file is a file name under weights_dir, not a path" % name)
        if "role" in spec and spec["role"] not in ("workhorse", "quick", "pilot"):
            raise ConfigError("models.%s.role must be workhorse, quick or pilot" % name)
        if "reasoning_budget" in spec and not _number_ok(spec["reasoning_budget"], int, -1):
            raise ConfigError("models.%s.reasoning_budget must be an integer >= -1" % name)
    if not isinstance(cfg.get("weights_dir"), str) or not cfg["weights_dir"]:
        raise ConfigError("weights_dir is required (the directory holding the GGUF files on the model host)")
    port = cfg.get("port")
    if isinstance(port, bool) or not isinstance(port, int) or not 1 <= port <= 65535:
        raise ConfigError("port must be an integer from 1 to 65535")
    for key, (kind, minimum) in NUMBERS.items():
        if key in cfg and cfg[key] is not None and not _number_ok(cfg[key], kind, minimum):
            raise ConfigError("%s must be %s >= %s" % (key, "an integer" if kind is int else "a number", minimum))
    kind_model = cfg.get("kind_model") or {}
    if not isinstance(kind_model, dict):
        raise ConfigError("kind_model must map task kinds to model names")
    for kind, model in kind_model.items():
        if model not in models:
            raise ConfigError("kind_model.%s names unknown model %s" % (kind, model))
    default = cfg.get("default_model")
    if default is not None and default not in models:
        raise ConfigError("default_model %s is not in models" % default)
    for key in ("launcher", "server_args"):
        value = cfg.get(key, [])
        if not isinstance(value, list) or not all(isinstance(a, str) for a in value):
            raise ConfigError("%s must be a list of strings" % key)
    reserved = sorted({a.split("=", 1)[0] for a in cfg.get("server_args", [])} & RESERVED_SERVER_FLAGS)
    if reserved:
        raise ConfigError("server_args may not set %s: the tool sets model, address and alias itself"
                          % ", ".join(reserved))
    window = cfg.get("job_hold_window")
    if window is not None and not isinstance(window, dict):
        raise ConfigError("job_hold_window must be an object with weekday_from and weekday_until")
    return cfg


def from_dict(data, environ=None):
    """Defaults + data + environment overrides, validated, with the runner argv resolved."""
    env = os.environ if environ is None else environ
    if not isinstance(data, dict):
        raise ConfigError("the config must be a JSON object")
    cfg = dict(DEFAULTS, **data)
    if env.get("LLAMA_ONDEMAND_SSH") is not None:
        cfg["ssh"] = env["LLAMA_ONDEMAND_SSH"]
    if env.get("LLAMA_ONDEMAND_PORT"):
        try:
            cfg["port"] = int(env["LLAMA_ONDEMAND_PORT"])
        except ValueError:
            raise ConfigError("LLAMA_ONDEMAND_PORT must be an integer")
    if env.get("LLAMA_ONDEMAND_STATE_DIR"):
        cfg["state_dir"] = env["LLAMA_ONDEMAND_STATE_DIR"]
    cfg["runner"] = runner_argv(cfg.get("ssh"))
    return validate(cfg)


def load(path=None, environ=None):
    p = config_path(path, environ)
    try:
        with open(p, encoding="utf-8") as fh:
            data = json.load(fh)
    except FileNotFoundError:
        raise ConfigError("no config at %s (pass --config or set %s)" % (p, ENV_CONFIG))
    except (OSError, ValueError) as exc:
        raise ConfigError("cannot read %s: %s" % (p, exc))
    return from_dict(data, environ)


def default_model(cfg):
    """The model to start when none is named: default_model, else the first workhorse, else the first model."""
    if cfg.get("default_model"):
        return cfg["default_model"]
    for name, spec in cfg["models"].items():
        if spec.get("role") == "workhorse":
            return name
    return next(iter(cfg["models"]))
