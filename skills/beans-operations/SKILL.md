---
name: beans-operations
description: "Beans model ops: add/change/remove, wire clients."
---

# Beans — Model Operations for Agents

beans is a self-configuring multi-model OpenAI-compatible inference server on this
machine: one port (127.0.0.1:11434), one API, one directory per model. This skill is
what an agent needs to USE it — not the container's internals.

## The one rule

**The directory name IS the model name.** `BEAN_HOLE/<name>/` = model id `<name>`.
Nothing in `config.json` names the model. Registry lives at `/home/ekl/beans/` here.
Dispatch is **directory-name ONLY** — the gguf basename is NOT an alias (Evan
2026-11-01: one source of truth; a basename map is extra complexity with zero gain
and conflict risk if the same gguf sits in two dirs).

## Hot-fallback GGUFs (Evan 2026-11-01)

A directory may hold MULTIPLE `.gguf` files; `config.json`'s `file` key picks the
active one. As long as that filename exists on disk the model is ready; if missing,
beansd downloads it. To swap models: edit `file` (and `url`/`sha256` for a new
download) — no directory rename, no rebuild.

## Lifecycle — what you do, what beans does

| Action | Result |
|---|---|
| `mkdir <root>/<name>` + drop `config.json` with `url` (no weights) | dir renames to `<name>-pending`, downloads, sha-verifies, renames back, wires up. No restart. |
| Drop weights GGUF + `config.json` | wires up immediately. |
| Edit `<name>/config.json` | restarts only that model's bridge. |
| Rename dir to `<name>-disabled` | ignored; server stopped if running. |
| `rm -rf <name>` | tears it down. |

**Pending marker:** while a download runs the directory is literally `<name>-pending`.
A failed download (checksum mismatch, curl error) STAYS `-pending` with a
`.download.failed` marker until the config changes. Not a hang — fix the config.

## config.json keys

| Key | Required | Meaning |
|---|---|---|
| `file` | yes¹ | GGUF filename in this directory |
| `url` | no | Download URL when GGUF missing (auto-provision) |
| `sha256` | no | Checksum, verified before wiring |
| `ctx` | no | Context window (default 4096) — clients auto-detect it |
| `threads` | no | CPU threads (default 4) |
| `misc_args` | no | Pass-through to llama-server (e.g. `["--n-gpu-layers","999"]`, `["--cache-type-k","q5_1","--cache-type-v","q5_1"]`) |
| `reasoning_budget` | no | For i1-style reasoning models |
| `embeddings` | no | `true` = embeddings endpoint |
| `pooling` | no | e.g. `"mean"` |
| `idle_seconds` | no | Idle timeout before unload (default 300) |
| `vram_headroom_mib` | no | Extra MiB reserved above GGUF size |

¹ `file` may be omitted if the dir has exactly one `.gguf`.
Unknown keys are rejected LOUDLY at startup — a bad config errors, never half-setups.

## Wiring Hermes at it

- Base URL everywhere: `http://127.0.0.1:11434/v1` · model = directory name.
- Aux slots (profile config.yaml, ALWAYS via `hermes config set`, never patch):
  `hermes config set auxiliary.vision.model <name> --profile <p>` + provider
  `custom` / base_url `http://127.0.0.1:11434/v1`.
- `/v1/props` returns llama.cpp shape with real `n_ctx` — Hermes auto-detects
  `llamacpp` + ctx; no provider faking.

## Diagnostics

```bash
curl -s http://127.0.0.1:11434/v1/models        # model + context_length (pending? absent/renamed)
curl -s http://127.0.0.1:11434/v1/props         # llama.cpp detection shape
docker logs beans | grep -E "ERROR|beansd"       # config drift, checksum, alias issues
nvidia-smi --query-gpu=memory.used --format=csv,noheader   # what's resident
```

## Pitfalls

- **VRAM guard:** not enough free VRAM → immediate 503 naming the model + numbers.
  No hold, no retry. Fix the workflow (unload idle model, disable, resize) — don't retry.
- **First request after idle pays load time (~10s)** — warm requests are fast. Don't
  declare a model broken on TTFT; measure the second call.
- **Model not showing on /v1/models** → check for `-pending` / `.download.failed` in
  the tree, then `docker logs beans`.
- **Re-pointing clients** always via `hermes config set --profile <p>` — direct
  config.yaml edits on another profile hit the write-guard anyway.