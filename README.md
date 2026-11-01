# beans 🫘

**A multi-model, OpenAI-compatible inference server that configures itself from one file per model.**

Set one env var, `docker compose up`, drop a `config.json` into a directory with a download
URL, and beans downloads the weights, lives on the wire, and powers down models when idle.
Adding a model is literally *one shared file* — no code, no rebuild, no config surgery.

```
127.0.0.1:11434  ← beansd router — ONE OpenAI-compatible API for every model
```

One published port, one normal OpenAI API. The `model` name in the request body routes
to the owning bridge; `/v1/models` and `/v1/props` are answered by the router from the
live registry, so clients auto-detect each model's true context window (e.g. llamacpp +
65535). Bridges bind loopback inside the container and are not published.

## Quick start

```sh
export BEANS_ROOT=/path/to/models          # required
docker compose up -d --build
```

Create `$BEANS_ROOT/<name>/config.json` — **the directory name IS the model name**
(client sends `"model":"<name>"`, e.g. `my-model` below). That's the whole setup:

```json
{
  "file": "model.Q4_K_M.gguf",
  "url": "https://example.com/model.Q4_K_M.gguf",
  "sha256": "optional-checksum-verified-before-wiring"
}
```

beans downloads the weights, verifies them, and starts serving:

```sh
curl http://127.0.0.1:11434/v1/chat/completions \
  -H 'Content-Type: application/json' \
  -d '{"model":"my-model","messages":[{"role":"user","content":"hello"}]}'
```

(The `model` field names the model; the router dispatches to its bridge. All
clients use the one port.)

## How it works

- **One container.** `beansd` (a small supervisor, PID 1) watches the registry tree with
  inotify and reconciles the running system on every change.
- **Nothing loads at boot.** Each model gets a tiny *bridge*; the bridge wakes `llama-server`
  on the first real request (event-driven, no polling) and forwards the connection.
- **Idle = death.** An idle watchdog (llama-ondemand) kills the server after its budget —
  VRAM returns to zero until the next request.
- **Models load on demand.** First request after idle pays the load time (~10s), the rest
  are warm.
- **One router.** beansd's listener on :11434 answers the meta routes from the live
  registry and dispatches inference by `model` to the owning bridge — streaming SSE
  passes through byte-for-byte. Bridges bind loopback; nothing else is published.

### Lifecycle

| You do | beans does |
|---|---|
| Drop a dir with `config.json` + URL, no weights | renames it `-pending`, downloads, sha-verifies, renames back, wires it up |
| Drop a dir with weights + `config.json` | wires it up immediately |
| Edit a model's `config.json` | restarts only that model's bridge |
| Rename a dir to `*-disabled` | ignores it; stops its server if running |
| Delete the dir | tears it down |

`-pending` is visible in the tree while a download runs. A failed download (checksum
mismatch, curl error) stays `-pending` with a `.download.failed` marker until the config
changes. Renames from the host side are respected live.

### VRAM guard

If there isn't enough free VRAM to load a model, the request **fails immediately** with a
503 naming the model and the numbers — no hold, no retry, no thrashing. Fix the workflow,
don't fight the machine.

## config.json format

| Key | Required | Meaning |
|---|---|---|
| `file` | yes¹ | GGUF filename in this directory |
| `url` | no | Download URL; used when the GGUF is missing (auto-provision) |
| `sha256` | no | Checksum; verified after download before the model is wired |
| `ctx` | no | Context window in tokens (default 4096) — kept first-class so clients can
  auto-detect the real window |
| `threads` | no | CPU threads (default 4) |
| `misc_args` | no | Raw pass-through to llama-server (e.g. `["--n-gpu-layers", "999"]` for
  full GPU offload, or `["--cache-type-k", "q5_1", "--cache-type-v", "q5_1"]` for a
  quantized KV cache) |
| `reasoning_budget` | no | Reasoning budget for i1-style reasoning models |
| `embeddings` | no | `true` runs the model as an embeddings endpoint |
| `pooling` | no | Embeddings pooling (e.g. `"mean"`) |
| `idle_seconds` | no | Idle timeout before the model unloads (default 300) |
| `vram_headroom_mib` | no | Extra MiB the VRAM guard reserves above the GGUF size |

¹ `file` may be omitted if the directory has exactly one `.gguf`.

Unknown keys are rejected loudly at startup — a bad config is an error, never a silent
half-setup.

## Point Hermes at beans

Hermes agents talk to beans like any OpenAI base URL — `127.0.0.1:11434/v1`.

**Model names = directory names** (forced consistency, no `name` key exists):
the docked registry on this machine has `ornith` (chat, ctx 65535) and
`nomic-embed` (embeddings, ctx 2048). Whatever you name a registry directory
is the `model` string clients must send.

**Embeddings** (in `~/.hermes/.env`):

```sh
EMBEDDING_API_BASE=http://127.0.0.1:11434/v1
EMBEDDING_MODEL=nomic-embed
EMBEDDING_FALLBACK_MODEL=nomic-embed
```

**Vision / auxiliary chat** (profile `config.yaml`, via `hermes config set`):

```yaml
auxiliary:
  vision:
    provider: custom
    model: ornith
    base_url: http://127.0.0.1:11434/v1
    api_key: ''
```

Set it with: `hermes config set auxiliary.vision.model ornith --profile <p>`.

**Detection stays native:** beans answers `/v1/models` (context_length intact)
and `/v1/props` (llama.cpp shape, `n_ctx` = the chat model's real window), so
Hermes still auto-classifies the server as `llamacpp` with the true context —
no provider faking anywhere in the path.

**Custom port:** export `BEANS_PORT` (default 11434) before `docker compose up`
and use that same port in every URL above.

## Examples

`examples/` has two ready-to-run configs (they download their own weights and replicate a
known-good setup — one chat model with full GPU offload, one embeddings model):

- `examples/ornith.config.json` — 9B reasoning chat model, ctx 65535, GPU offload
- `examples/nomic-embed.config.json` — 768-dim embeddings, small/fast

Copy either to `$BEANS_ROOT/<name>/config.json` and it provisions itself.

## Notes

- The mount must be read-write: beans writes downloads and `-pending` renames into the tree.
- Idle-stop uses [llama-ondemand](https://github.com/tommyjohnsen/llama-ondemand) (vendored,
  with one documented patch: the RAM-fit check treats `--n-gpu-layers` models as VRAM-resident,
  so host-RAM headroom is `gpu_ram_slack_mib` (default 1024) instead of the weights-file size).
- Requires an NVIDIA GPU (`gpus: all`) and Docker with the CUDA container toolkit.
- No personal paths, no telemetry, no cloud. Everything is local.