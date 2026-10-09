# llama-swap-tray

**One-click Windows setup for [llama-swap](https://github.com/mostlygeek/llama-swap): run multiple LLMs and other GPU apps (ComfyUI, music/image generators, …) on a single graphics card — with automatic model swapping, boot autostart, a taskbar tray control, and a self-verifying update mechanism.**

```
Open WebUI / agents / any        ┌─────────────────────┐      ┌── llama-server (LLM A, on demand)
OpenAI-compatible client  ───►   │  llama-swap  :9292  │ ───► ├── llama-server (LLM B, always on)
                                 │  (boot task, SYSTEM)│      ├── ComfyUI / other GPU apps
Browser: dashboard at /ui  ───►  └─────────────────────┘      └── … one VRAM owner, zero conflicts
```

## Why

If several AI apps (LLM server, image generation, …) share one GPU, they fight over
VRAM: models silently fall back to CPU inference, generations crash, agents hang.
llama-swap fixes this by being the **single owner of the GPU** — every model and app
is started on demand, swapped when something else needs the VRAM, and unloaded when
idle. Requests for an unloaded model *queue* for a few seconds instead of degrading.

This repo packages that idea into a complete, reproducible Windows deployment:

- **`install.cmd`** — one double-click: downloads llama.cpp + llama-swap, registers
  a boot task (server available before login), sets firewall + permissions, compiles
  and registers the tray app.
- **`swap-tray.exe`** — taskbar icon showing live state (green = idle, blue = models
  loaded, gray = stopped) with one-click *unload VRAM* / *stop (gaming mode)* /
  *start* / *disable autostart permanently* — all without UAC prompts.
- **`update.cmd`** — checks whether updates exist, backs up the working state,
  installs the newest releases, **verifies with a real inference**, and rolls back
  automatically with an error description if anything broke.

Everything lives in this one folder. No installer, no services, no registry magic —
two scheduled tasks and a config file.

## Requirements

- Windows 10/11, 64-bit (uses the preinstalled .NET Framework C# compiler — no dev tools needed)
- A GPU supported by llama.cpp (NVIDIA/CUDA by default; AMD/Intel via Vulkan; CPU-only works too)
- GGUF model files (e.g. downloaded with [LM Studio](https://lmstudio.ai/) or from Hugging Face)
- PowerShell 5.1 (default on every Windows)

## Quick start

1. **Clone** (a path without spaces is easiest):
   ```
   git clone https://github.com/the-ulib/llama-swap-tray.git C:\llama-swap-tray
   ```
2. **Configure**: copy `config.example.yaml` → `config.yaml`, set the `llama:` macro
   to `<your clone path>\bin\llama.cpp\llama-server.exe` and point the model entries
   at your GGUF files. The example file documents the important flags.
3. **Non-NVIDIA GPU?** Edit `$Backend` at the top of `setup\01-llama-cpp.ps1`
   (`cuda` | `vulkan` | `cpu`).
4. **Double-click `install.cmd`** (UAC prompt, prerequisite checklist, then steps 01–04 run).
5. Point your clients (Open WebUI, agents, anything OpenAI-compatible) at
   `http://<host>:9292/v1`. The dashboard lives at `http://<host>:9292/ui`.

To change the port: edit `$Port` in `setup\03-server-task.ps1` **and** the `Api`
constant in `setup\swap-tray.cs`, then re-run `install.cmd`.

For existing installations, run `setup\06-fix-task-limits.ps1` as administrator
from PowerShell. It backs up both task definitions and removes Windows Task
Scheduler's default 72-hour execution limit without starting or stopping either
task. This is safe while the server is in gaming mode. New installations set
unlimited execution time for both the server and tray tasks automatically.

## What gets installed where

| What | Where |
|---|---|
| llama-swap binary | `bin\llama-swap\` (downloaded by setup 02) |
| llama.cpp build | `bin\llama.cpp\` (downloaded by setup 01, incl. CUDA runtime DLLs) |
| Tray app | `swap-tray.exe` (compiled locally from `setup\swap-tray.cs`) |
| Scheduled tasks | Task Scheduler folder `llama-swap`: `server` (SYSTEM, at boot) and `tray` (at logon) |
| Update backups | `backup\<timestamp>\` (last 3 kept) |

The `server` task gets an ACL entry so **local users can start/stop it without UAC**
(that is what makes the tray seamless). Uninstall: delete both tasks
(`schtasks /Delete /TN "llama-swap\server" /F` and `…\tray`), then delete the folder.

## The tray app

Right-click the λ icon:

- **Status line** — which models are loaded right now (also shown as icon color + tooltip)
- **Open dashboard** — llama-swap's web UI (also on double-click)
- **Edit config.yaml** — opens your config in the default editor; llama-swap
  hot-reloads it on save, so changes apply without a restart
- **Unload models** — frees all VRAM instantly; models reload on the next request.
  Enough for a quick game.
- **STOP llama-swap (gaming mode)** — unloads everything and ends the server task.
  Nothing can claim the GPU until you start it again (or reboot — the boot task returns).
- **Start llama-swap** — starts the server task again.
- **Disable/enable autostart** — persistent across reboots. The tray itself always
  keeps auto-starting so re-enabling stays one click away.

## Managing other GPU apps (ComfyUI etc.)

Any app can be a llama-swap "model": add an entry with `cmd:`, a health
`checkEndpoint:` and put it in the same swap group as your big LLMs (see the
commented ComfyUI example in `config.example.yaml`). llama-swap then starts it on
demand, stops it when an LLM needs the VRAM, and proxies it at
`http://<host>:9292/upstream/<name>/`. Hard-earned rules for this to work reliably:

1. **Always set `cmdStop: taskkill /f /t /pid ${PID}`.** Python venv launchers and
   pip console-script exes are *stubs* that spawn the real interpreter as a child;
   the default stop only kills the stub and the orphan keeps port + VRAM, wedging
   every future swap.
2. **Browser UIs belong on the app's direct port, not on `/upstream/`.** Two reasons:
   llama-swap decodes `%2F` in proxied paths (breaks e.g. ComfyUI's workflow loading),
   and an open browser tab that polls the app through the proxy re-claims it every
   few seconds — starving every LLM request forever. Use a fixed `--port`, open the
   firewall for it, and let only *programmatic* clients go through `/upstream/`.
3. **Async job APIs (submit → poll) are invisible to the swapper.** llama-swap drains
   in-flight HTTP requests before swapping, but a background job between polls gets
   killed when an LLM claims the GPU. Agents must submit, poll to completion **and
   download results within one tool call** — never return to the LLM mid-job.
4. **Apps spawned by llama-swap inherit a minimal environment.** If a tool shells
   out to `ffmpeg` or similar, pass an explicit `env:` `PATH=` in its entry — the
   error is otherwise swallowed and the app reports success with empty results.

## Updating

Double-click `update.cmd`. It exits in seconds when everything is current; otherwise:
pre-check (only update from a *working* state) → backup → download newest releases →
verify (GPU detected, API up, real inference produced tokens — with an automatically
selected model from your config) → automatic rollback + error description on failure.

## Testing

- **`tests\smoke.ps1`** — non-invasive, runs anywhere in seconds: all scripts parse,
  the tray app compiles with the inbox C# compiler, the repo is complete, no
  machine-specific paths slipped in.
- **`tests\integration.ps1`** — a REAL end-to-end installation (cpu backend, ~1 MB
  test model): downloads, task registration, no-UAC permissions, tray build, live
  inference, stop/start, update version check. **Run it only on a disposable
  system** — it refuses to run when it detects an existing installation.
- **`tests\sandbox.wsb`** — the easiest disposable system: double-click to open a
  [Windows Sandbox](https://learn.microsoft.com/windows/security/application-security/application-isolation/windows-sandbox/)
  (Windows Pro/Enterprise feature) that copies the repo and runs the integration
  test automatically. Closing the window discards everything. Note: the sandbox has
  no GPU passthrough, which is exactly why the test forces the cpu backend — GPU
  specifics still need one manual run on real hardware.

## Troubleshooting

| Symptom | Cause / fix |
|---|---|
| Inference is absurdly slow (~4 t/s) | CUDA build without the cudart runtime DLLs — llama.cpp silently falls back to CPU. Re-run `setup\01` (it always fetches both zips). Check: `bin\llama.cpp\llama-server.exe --list-devices` must show your GPU. |
| Requests hang forever, no model starts | Group membership must be a `members:` list *inside* the group — a `group:` key on the model entry is silently ignored. |
| Swap hangs, orphan processes, port stays busy | Missing `cmdStop` tree-kill on an entry whose launcher spawns children (see above). |
| Model answers are empty | Reasoning models spend the whole `max_tokens` budget on thinking. Raise `max_tokens`, or test via `/v1/completions` (no chat template). |
| Model reports no image support | llama.cpp loads vision from a separate projector file: add `--mmproj <mmproj-*.gguf>` to the model's `cmd`. Costs 0.2–1.3 GB extra VRAM. |
| "model not found" from clients | Clients must use the model IDs from your `config.yaml` — check `http://<host>:9292/v1/models`. |
| Antivirus quarantines files | Heuristics dislike hidden scripts and freshly compiled exes. Add an exclusion for this folder. Two gotchas: once-quarantined *paths* can stay write-locked until reboot (use a new filename instead of fighting), and shortcuts to scripts in `shell:startup` get re-flagged — which is exactly why this project uses scheduled tasks and a compiled tray app instead. |
| Editing scripts: PowerShell traps | Native tools like llama-server write to stderr; `2>&1` in PS 5.1 turns that into script-killing error records — route redirects through `cmd /c`. And never edit UTF-8 files with `Get-Content \| Set-Content` (encoding corruption). |

## Credits

This project glues together two excellent upstream projects, downloaded at install time:

- [llama-swap](https://github.com/mostlygeek/llama-swap) (MIT) — the model-swapping proxy
- [llama.cpp](https://github.com/ggml-org/llama.cpp) (MIT) — the inference server

The scripts, tray app and documentation in this repo are MIT-licensed (see LICENSE).
