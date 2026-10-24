# Optional ComfyUI coordination

This opt-in component lets independently launched ComfyUI and synchronous LLM
requests take turns on one GPU. It is **off by default**. The ordinary tray
installation still needs only PowerShell and .NET; it does not install Python.

Initial support: Windows x64, one NVIDIA GPU, and ComfyUI's PyTorch CUDA memory
interface. The ordinary llama-swap-tray Vulkan/CPU options remain available, but
Vulkan, DirectML and CPU-only ComfyUI are not supported by this optional feature.
Enabling is rejected when a running ComfyUI reports an unsupported memory backend.

## Install

First complete the ordinary llama-swap-tray setup and configure your LLM models.
Unload the models and empty ComfyUI's queue. In an administrator PowerShell,
from this repository's folder:

```powershell
.\setup\07-coordination.ps1 -ComfyRoot 'D:\AI\ComfyUI'
```

`ComfyRoot` is the folder containing `main.py` and `custom_nodes`. For a portable
installation that is usually the `ComfyUI` subfolder of the portable bundle.
Pinokio, portable and ordinary venv installations are supported through this path;
the coordinator does not start or stop them as llama-swap model processes.

The installer uses a compatible 64-bit Python 3.10+ already on PATH, or downloads
the signed official Python 3.14.9 installer into `runtime` and verifies its
Authenticode publisher before installing it in that folder. It creates its own
`.coord-env`; ComfyUI's environment is not modified. To choose a runtime explicitly:

```powershell
.\setup\07-coordination.ps1 -ComfyRoot 'D:\AI\ComfyUI' -Python 'D:\Python\python.exe'
```

The installer generates the ignored `coordinator.json` from the tracked
`coordination.example.json`, backs up config/task definitions, installs the custom
node extension with its local settings, and rebuilds the tray. Restart an idle
ComfyUI once, then check **Coordinate with ComfyUI** in the tray menu.

Additional options:

- `-PublicPort`, `-BackendPort`, `-ComfyPort`: distinct ports, default 9292/9293/8188.
  Existing LLM clients keep using the public port. The upstream backend binds only
  to loopback. The tray and update script read the generated settings.
- `-LlmModels model-a,model-b`: explicitly select synchronous LLM servers. Without
  it, the installer recognizes `llama-server` after expanding config macros.
  Other engines need explicit selection. Aliases are included automatically.
- `-ComfyModelIds my-comfy`: explicitly remove an existing ComfyUI process entry
  and its group membership. Other models and comments are preserved. A detected
  conflicting ComfyUI process entry without this option causes a preflight error.
- `-AllowPersistentUnload`: explicitly permit whole-GPU handovers to unload
  models in persistent groups. The default example's always-on small model uses
  such a group, so this option is necessary if you keep that group. Without it,
  installation refuses to silently override persistence.
- `-TaskFolder`: use an existing matching installation's task folder. The
  installer refuses to replace a task belonging to another installation.

Remove llama-swap startup preloads before installing: they load outside request
admission. Do not add them while coordination is enabled. Manual runtime-loading
API routes are also blocked while on. Unselected services, async job servers,
and LLM websocket inference require coordination off; they must not bypass the
GPU gate. No model IDs, checkpoints, hardware-specific budgets or Pinokio paths
are built into the coordinator.

Re-running the installer retains custom timeout/memory/start-command settings and
the original rollback point. It leaves coordination off again until the updated
ComfyUI extension has been loaded and enabled. Install failures restore the
previous task/config. Current working files and task XML are backed up first.

## Behavior

With coordination on:

1. An incoming LLM request waits for the current ComfyUI workflow to finish.
2. ComfyUI holds the remaining jobs with their original IDs and order.
3. The worker unloads models and execution caches, then acknowledges GPU release.
4. The coordinator forwards the LLM request, including streaming responses.
5. The LLM is unloaded and verified before ComfyUI resumes its next workflow.

With both queues busy, one workflow alternates with one LLM request. The switching
boundary is a complete workflow, not a sampler step or video frame. Model loads
and loss of execution/prompt caches add latency. Clients must allow time for the
workflow boundary, model startup and inference.

With coordination off, LLM requests go through normally and ComfyUI is left alone;
GPU sharing is manual. Off does not restore the removed ComfyUI process-killing
entry. A running handover finishes releasing the LLM before a disabled hold is
lifted. With ComfyUI absent, LLM concurrency and idle TTL remain normal. A newly
started ComfyUI checks ownership before starting its first workflow.

ComfyUI can be opened directly on its own port or at `/comfyui/` on the public
API. By default it must be started normally. To add automatic startup, configure
`comfy_start_command` with an argv array that starts a separately managed task,
such as `schtasks /Run /TN your-comfy-task`. Do not put it back in a swapping group.

## Settings and recovery

The tray checkbox persists `enabled` in `coordinator.json`. Status is available
at `/coordination/status`. Other useful fields are:

- `max_wait_seconds`: maximum workflow-boundary wait (1800 by default).
- `max_comfy_allocated_mib`: maximum live PyTorch allocation after cleanup (512).
- `minimum_free_vram_mib`: minimum admission floor (2048), **not** a model-size
  estimate. `model_minimum_free_vram_mib` can hold measured per-model requirements.
- `comfy_start_command`: optional fixed argv for an independent startup task.

Port/command/token-path changes require a coordinator restart. Ordinary settings
are read automatically. Logs, secrets and local settings are excluded from Git.

Missing/unresponsive extensions and failed memory release produce an error rather
than loading over ComfyUI. A disconnected active LLM call is drained before GPU
release. If the coordinator stops, the Windows Job Object terminates its backend
and child model processes; independent ComfyUI finishes its current workflow and
holds before the next. Restart the server task to recover. The tray's stop action
therefore stops the LLM service and does not kill ComfyUI.

For a verified retry after an unload error, make a local POST to
`/coordination/recover`. It refuses while a live request/workflow owns the GPU,
then unloads and verifies the backend before releasing the hold. Control routes
are local-only; worker messages also require the generated local token.

Run `setup\08-rollback-coordination.ps1` as administrator while both queues are
idle to restore the original task and config. It saves the current config first,
because restoring the original backup can undo later model edits. The dormant
extension stays installed with coordination off. Restart the tray after rollback.

## Validation

Non-invasive tests (no tasks registered, model loads or installed ComfyUI changes):

```powershell
.\tests\smoke.ps1
.\tests\coordination.ps1
.\tests\coordination-installer.ps1 -Python .\.coord-env\Scripts\python.exe
```

`tests\coordination.ps1` accepts `-Python` and optionally `-LlamaSwapExe` for a real
backend/launcher smoke test on temporary ports. Installer tests execute the real
scripts against temporary files while mocking every task/API operation.

For a **disposable Windows Sandbox/VM only**, run
`tests\integration.ps1 -WithCoordination`. It tests actual installation, tasks,
CPU inference and rollback with a fake ComfyUI memory protocol, without a GPU.
Real hardware validation still needs an actual queued ComfyUI workflow: submit
several workflows, request an LLM while one runs, verify the remaining prompt IDs
stay queued during inference, and verify all outputs finish in the same ComfyUI
session afterward. The original implementation was exercised this way on an
RTX 5090; that does not establish support for other memory backends.
