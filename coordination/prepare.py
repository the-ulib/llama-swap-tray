"""Build machine-local settings without imposing model names or install paths."""
import argparse
import json
from pathlib import Path
import re
import secrets

import yaml

from .migrate_config import migrate


def prepare(root, comfy_root, public_port=9292, backend_port=9293, comfy_port=8188,
            llm_models=(), comfy_models=(), allow_persistent_unload=False,
            task_name="llama-swap\\server"):
    root = Path(root).resolve()
    comfy_root = Path(comfy_root).resolve()
    if not (comfy_root / "main.py").is_file():
        raise ValueError("ComfyRoot must contain main.py (the core ComfyUI folder)")
    if len({public_port, backend_port, comfy_port}) != 3 or not all(0 < p < 65536 for p in (public_port, backend_port, comfy_port)):
        raise ValueError("Public, backend and ComfyUI ports must be distinct valid ports")
    text = (root / "config.yaml").read_text(encoding="utf-8-sig")
    native = yaml.safe_load(text)
    models = native.get("models", {})
    macros = native.get("macros", {})

    def command(model):
        cmd = model.get("cmd", "")
        return re.sub(r"\$\{([^}]+)\}", lambda m: str(macros.get(m[1], m[0])), cmd)

    # Explicit IDs support any synchronous LLM server. Automatic selection is
    # deliberately limited to recognizable llama-server commands.
    previous_path = root / "coordinator.json"
    previous = json.loads(previous_path.read_text(encoding="utf-8-sig")) if previous_path.exists() else {}
    selected = set(llm_models or previous.get("llm_model_ids", [])) if llm_models or previous.get("llm_model_ids") else {
        name for name, model in models.items() if "llama-server" in command(model).lower()
    }
    if not selected or not selected <= set(models):
        raise ValueError("Select existing synchronous LLM model IDs with -LlmModels")
    removed = set(comfy_models)
    if not removed <= set(models):
        raise ValueError("A requested ComfyModelIds entry does not exist")
    # Refuse conflicting process lifecycle management instead of silently
    # changing a model that the user did not select for migration.
    for name, model in models.items():
        cmd = command(model).replace("\\", "/").lower()
        if str(comfy_root).replace("\\", "/").lower() in cmd and "main.py" in cmd and name not in removed:
            raise ValueError(f"ComfyUI is also managed as '{name}'; pass -ComfyModelIds {name}")
    if selected & removed:
        raise ValueError("ComfyUI entries cannot also be selected as LLM models")
    if native.get("hooks", {}).get("on_startup", {}).get("preload"):
        raise ValueError("Remove startup preloads before installing coordination; loads must pass through admission")
    persistent = [name for name, group in native.get("groups", {}).items()
                  if group.get("persistent") and set(group.get("members", [])) - removed]
    if persistent and not allow_persistent_unload:
        raise ValueError("Persistent groups would be unloaded: " + ", ".join(persistent) +
                         ". Use -AllowPersistentUnload to explicitly permit whole-GPU handovers")
    cfg = json.loads((root / "coordination.example.json").read_text())
    for key in ("listen_host", "max_wait_seconds", "worker_timeout_seconds", "max_comfy_allocated_mib",
                "minimum_free_vram_mib", "model_minimum_free_vram_mib", "comfy_start_command", "comfy_stop_command"):
        if key in previous:
            cfg[key] = previous[key]
    cfg.update(listen_port=public_port, backend_url=f"http://127.0.0.1:{backend_port}",
               comfy_url=f"http://127.0.0.1:{comfy_port}", comfy_process_path=str(comfy_root),
               server_task_name=task_name, llm_models=sorted(selected),
               allow_persistent_unload=allow_persistent_unload,
               control_token_file=str(root / "runtime" / "coordination-token.txt"))
    cfg["backend_command"] = [str(root / "bin" / "llama-swap" / "llama-swap.exe"),
                              "-config", str(root / "config.yaml"), "-watch-config", "-listen", f"127.0.0.1:{backend_port}"]
    cfg["blocked_models"] = sorted({alias for name, model in models.items() if name not in selected | removed
                                    for alias in [name, *model.get("aliases", [])]})
    cfg["llm_models"] = sorted({alias for name in selected for alias in [name, *models[name].get("aliases", [])]})
    cfg["llm_model_ids"] = sorted(selected)
    return cfg, migrate(text, removed)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", required=True)
    parser.add_argument("--comfy-root", required=True)
    parser.add_argument("--public-port", type=int, default=9292)
    parser.add_argument("--backend-port", type=int, default=9293)
    parser.add_argument("--comfy-port", type=int, default=8188)
    parser.add_argument("--llm-model", action="append", default=[])
    parser.add_argument("--comfy-model", action="append", default=[])
    parser.add_argument("--allow-persistent-unload", action="store_true")
    parser.add_argument("--task-name", default="llama-swap\\server")
    args = parser.parse_args()
    cfg, migrated = prepare(args.root, args.comfy_root, args.public_port, args.backend_port, args.comfy_port,
                            args.llm_model, args.comfy_model, args.allow_persistent_unload, args.task_name)
    destination = Path(args.root) / "runtime" / "coordination-staged"
    destination.mkdir(parents=True, exist_ok=True)
    (destination / "coordinator.json").write_text(json.dumps(cfg, indent=2) + "\n", encoding="utf-8")
    (destination / "config.yaml").write_text(migrated, encoding="utf-8")
    token = Path(cfg["control_token_file"])
    if not token.exists():
        token.write_text(secrets.token_hex(32), encoding="ascii")


if __name__ == "__main__":
    main()
