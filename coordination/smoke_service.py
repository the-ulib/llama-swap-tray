"""Exercise the real Windows service launcher/backend without loading a model."""
import json
import argparse
from pathlib import Path
import socket
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request

import psutil


def free_port():
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        return listener.getsockname()[1]


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--llama-swap-exe", type=Path)
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[1]
    with tempfile.TemporaryDirectory() as temporary:
        temporary = Path(temporary)
        model_config = temporary / "models.yaml"
        model_config.write_text('models:\n  smoke-only:\n    cmd: "cmd /c exit 0"\n    proxy: "http://127.0.0.1:1"\n', encoding="utf-8")
        cfg = json.loads((root / "coordination.example.json").read_text())
        cfg.update(enabled=False, listen_host="127.0.0.1", listen_port=free_port(),
                   backend_url="http://127.0.0.1:" + str(free_port()),
                   control_token_file=str(temporary / "token"))
        cfg["backend_command"] = [str(args.llama_swap_exe or root / "bin/llama-swap/llama-swap.exe"),
                                  "-config", str(model_config), "-listen", cfg["backend_url"].removeprefix("http://")]
        config = temporary / "config.json"
        config.write_text(json.dumps(cfg))
        process = subprocess.Popen([sys._base_executable, str(root / "coordination/launch.py"), "--config", str(config)],
                                   creationflags=subprocess.CREATE_NO_WINDOW)
        children = []
        try:
            base = f"http://127.0.0.1:{cfg['listen_port']}"
            deadline = time.monotonic() + 15
            while True:
                try:
                    with urllib.request.urlopen(base + "/v1/models", timeout=1) as response:
                        models = json.load(response)
                    break
                except (urllib.error.URLError, OSError):
                    if process.poll() is not None or time.monotonic() > deadline:
                        logs = (temporary / "runtime/coordinator.log").read_text(errors="replace")
                        raise RuntimeError("Service failed to start: " + logs)
                    time.sleep(0.1)
            assert models["data"][0]["id"] == "smoke-only", models
            children = psutil.Process(process.pid).children(recursive=True)
            assert any(child.name() == "llama-swap.exe" for child in children)
            with urllib.request.urlopen(base + "/coordination/status") as response:
                assert json.load(response)["enabled"] is False
            print("Real service launcher and llama-swap proxy: PASS")
        finally:
            process.terminate()
            process.wait(10)
            _, alive = psutil.wait_procs(children, timeout=10)
            assert not alive, f"Orphan backend processes: {alive}"
            print("Real coordinator termination releases backend: PASS")


if __name__ == "__main__":
    main()
