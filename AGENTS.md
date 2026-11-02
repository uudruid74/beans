# AGENTS.md — beans operations for agents

beans is a **self-configuring, multi-model, OpenAI-compatible inference server** in one
container. This file is the operating manual for AI agents that run, debug, or extend it.

## The mental model

- **The registry is the truth.** `BEANS_ROOT/<model>/` = one directory per model:
  weights GGUF + `config.json`. Everything the container runs is *derived from this tree*.
- **One supervisor, no magic.** `beansd.py` (PID 1) watches the tree with inotify and
  reconciles state on every change: spawn/kill bridges, run downloads.
- **Nothing loads at boot.** Each model has a *bridge* process that wakes `llama-server`
  on the first real request (event-driven) and llama-ondemand kills it after `idle_seconds`.
- **Each bridge IS the server; beansd routes.** ONE published port — the beansd router on
  `127.0.0.1:11434` — serves a normal OpenAI API. The `model` field in the request body
  dispatches to the owning bridge (by directory name — the directory name IS the
  model name; the model id IS the directory). Meta routes
  (`/v1/models`, `/v1/props`, `/health`) are answered by the router from the live registry,
  so clients auto-detect each model's real context window (llamacpp + 65535). Bridges bind
  loopback (`127.0.0.1`)
  inside the container and are never published. Worker containers reach beans by name on
  the `beans_net` external network: `http://beans:11434/v1`.

## Setup

```sh
export BEANS_ROOT=/path/to/models        # REQUIRED — compose fails loud without it
docker compose up -d --build            # optional: BEANS_PORT=11434 for a custom port
```

**Hermes client wiring (one base URL, one port):**

- Base URL everywhere: `http://127.0.0.1:11434/v1` (or `http://beans:11434/v1` from
  a worker container on `beans_net`).
- **Model names are the DIRECTORY names** — there is no `name` key in `config.json`,
  so `mkdir $BEANS_ROOT/anything` makes `"anything"` the model id. On this machine:
  `ornith` (chat) and `nomic-embed` (embeddings).
- Env (shared): `EMBEDDING_API_BASE=http://127.0.0.1:11434/v1`,
  `EMBEDDING_MODEL=nomic-embed`, `EMBEDDING_FALLBACK_MODEL=nomic-embed`.
- Profile config.yaml (unless already set): `auxiliary.vision.model=ornith` with
  `provider: custom` and `base_url: http://127.0.0.1:11434/v1`. Set via
  `hermes config set auxiliary.vision.model ornith --profile <p>` — agents cannot
  patch profiles/*/config.yaml directly (write-guard).
- Detection: `/v1/props` returns llama.cpp shape with `n_ctx` from the live chat
  model's ctx — Hermes auto-detects `llamacpp` + 65535 without config.

## How to add / change / remove a model

| Action | What to do |
|---|---|
| Add | `mkdir $BEANS_ROOT/<name>` + drop `config.json` (with `url` and/or the GGUF). beansd provisions, downloads if needed, wires it up. No restart. |
| Change | Edit that model's `config.json`. Only its bridge is restarted. |
| Disable | `mv $BEANS_ROOT/<name> $BEANS_ROOT/<name>-disabled`. Ignored; server stopped if running. |
| Remove | `rm -rf $BEANS_ROOT/<name>`. |

Watch it happen: `docker logs -f beans`.

## Diagnostics cheatsheet

- **Model up?** `curl -s http://127.0.0.1:11434/v1/models` → the model with its context_length.
- **Server type (detection)?** `curl -s http://127.0.0.1:11434/v1/props` →
  `default_generation_settings.n_ctx` present = hermes classifies `llamacpp`.
- **Unknown model?** POST with a bogus `model` → 404 `{"error":{"message":"model_not_found"}}`.
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
- **HTTP 404/broken requests on a model** — usually the bridge died: `docker logs beans`
  for its traceback (bare-except paths: see `bridge.py`). beansd respawns it on a tree
  event; restart the container (`docker compose up -d --force-recreate`) to revive
  immediately.
- **Python imports in exec fail but the bridge works** — the bridge imports `llama_ondemand`
  because running `/opt/bridge.py` puts `/opt` on `sys.path`. In `docker exec`, use
  `cd /opt && python3 -c "import sys; sys.path.insert(0,'/opt'); ..."`.
- **Idle-death verified** by the llama-ondemand state log
  (`{"watch": "stopped_idle", ...}`) and VRAM dropping to ~0.

## Extending / the build

- Files: `beansd.py` (supervisor), `bridge.py` (per-model wake + splice + VRAM guard),
  `entrypoint.sh` (execs beansd), `Dockerfile` (llama.cpp server-cuda),
  `docker-compose.yml` (BEANS_ROOT required), `vendor/llama_ondemand` (patched — see
  README "Notes").
- Config source of truth: the `ALLOWED` set + `write_ondemand()` in `beansd.py`; the
  `config.json` table in README. Add a key to both, or the registry rejects it.
- Registry key `misc_args` maps to llama-ondemand's internal `server_args` (see
  `write_ondemand`) — the public schema is `ctx` (first-class) + `misc_args`
  (pass-through).
- Rebuild after any change: `docker build -t beans:1ctr . && BEANS_ROOT=... docker compose
  up -d --force-recreate`. Verify boot with the diagnostics cheatsheet before declaring done.