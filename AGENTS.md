# AGENTS.md — beans operations for agents

beans is a **self-configuring, multi-model, OpenAI-compatible inference server** in one
container. This file is the operating manual for AI agents that run, debug, or extend it.

## The mental model

- **The registry is the truth.** `BEANS_ROOT/<model>/` = one directory per model:
  weights GGUF + `config.json`. Everything the container runs is *derived from this tree*.
- **One supervisor, no magic.** `beansd.py` (PID 1) watches the tree with inotify and
  reconciles state on every change: spawn/kill bridges, write Olla config, run downloads.
- **Nothing loads at boot.** Each model has a *bridge* process that wakes `llama-server`
  on the first real request (event-driven) and llama-ondemand kills it after `idle_seconds`.
- **The seam** (`127.0.0.1:11434/olla/openai/v1/*`) is Olla, a stateless Go proxy. It
  routes by model alias and restarts only on topology changes.

## Setup

```sh
export BEANS_ROOT=/path/to/models        # REQUIRED — compose fails loud without it
docker compose up -d --build
```

## How to add / change / remove a model

| Action | What to do |
|---|---|
| Add | `mkdir $BEANS_ROOT/<name>` + drop `config.json` (with `url` and/or the GGUF). beansd provisions, downloads if needed, wires it up. No restart. |
| Change | Edit that model's `config.json`. Only its bridge is restarted. |
| Disable | `mv $BEANS_ROOT/<name> $BEANS_ROOT/<name>-disabled`. Ignored; server stopped if running. |
| Remove | `rm -rf $BEANS_ROOT/<name>`. |

Watch it happen: `docker logs -f beans`.

## Diagnostics cheatsheet

- **Seam up?** `curl -s http://127.0.0.1:11434/internal/health` → 200.
- **Models visible?** `curl -s http://127.0.0.1:11434/olla/openai/v1/models`.
- **What's loaded?** `docker exec beans sh -c 'ps -e | grep -c llama-server'` and
  `nvidia-smi --query-gpu=memory.used --format=csv,noheader`.
- **Config drift / errors:** `docker logs beans | grep -E "ERROR|beansd"` — unknown config
  keys, missing urls, checksum mismatches and duplicate aliases all log loudly.
- **Bridge health:** `docker exec beans sh -c 'ps -e -o pid,args | grep bridge.py'`. A dead
  bridge + alive beansd = the bridge crashed (check its stderr in `docker logs beans`).

## Failure semantics (know these before debugging)

- **503 with `beans: not enough VRAM for <model> (need ~N MiB, free M MiB)`** — by design.
  Fail-loud VRAM guard: another model is resident; unload it (idle-death or disable), raise
  VRAM, or resize. **Do not** "fix" by holding/retrying.
- **Olla 502 `all endpoints unavailable (circuit breaker open)`** — the breaker opens after
  repeated backend failures. It resets on Olla restart (topology change or container
  restart). A *sustained* breaker usually means a bridge is crashing on every request —
  grab `docker logs beans` for its traceback (bare-except paths: see `bridge.py`).
- **Python imports in exec fail but the bridge works** — the bridge imports `llama_ondemand`
  because running `/opt/bridge.py` puts `/opt` on `sys.path`. In `docker exec`, use
  `cd /opt && python3 -c "import sys; sys.path.insert(0,'/opt'); ..."`.
- **Idle-death verified** by the llama-ondemand state log
  (`{"watch": "stopped_idle", ...}`) and VRAM dropping to ~0.

## Extending / the build

- Files: `beansd.py` (supervisor), `bridge.py` (per-model wake + splice + VRAM guard),
  `entrypoint.sh` (execs beansd), `Dockerfile` (llama.cpp server-cuda + Olla binary),
  `docker-compose.yml` (BEANS_ROOT required), `vendor/llama_ondemand` (patched — see
  README "Notes").
- Config source of truth: the `ALLOWED` set + `write_ondemand()` in `beansd.py`; the
  `config.json` table in README. Add a key to both, or the registry rejects it.
- Rebuild after any change: `docker build -t beans:1ctr . && BEANS_ROOT=... docker compose
  up -d --force-recreate`. Verify boot with the diagnostics cheatsheet before declaring done.