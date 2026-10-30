"""Worker-boundary guard. No GPU operations are performed on the HTTP thread."""
import ipaddress
import json
import logging
import ntpath
import socket
import threading
import time
import urllib.error
import urllib.request
import uuid
from urllib.parse import urlsplit

import psutil


def backend_stopped(cfg):
    """Require both a refused local connection and no backend/model processes."""
    try:
        backend = urlsplit(cfg["backend_url"])
        if backend.scheme != "http" or not ipaddress.ip_address(backend.hostname).is_loopback:
            return False
        command = cfg["backend_command"]
        if not isinstance(command, list) or not command or not isinstance(command[0], str) or not command[0]:
            return False
        names = {"llama-swap", "llama-swap.exe", "llama-server", "llama-server.exe",
                 ntpath.basename(command[0]).lower()}
        # Windows can take about two seconds to report connection refusal.
        with socket.create_connection((backend.hostname, backend.port or 80), timeout=2.5):
            return False
    except ConnectionRefusedError:
        pass
    except (OSError, ValueError, TypeError, KeyError):
        return False
    try:
        for process in psutil.process_iter(["name"]):
            try:
                name = process.info["name"]
                if not name or name.lower() in names:
                    return False
            except psutil.NoSuchProcess:
                continue
    except (psutil.Error, OSError):
        return False
    return True


class WorkerGuard:
    def __init__(self, config_path, cleanup):
        self.config_path = config_path
        self.cleanup = cleanup
        self.session = uuid.uuid4().hex
        self.completed = None
        self.unloaded = None
        self.cleanup_error = None
        self.grant = None
        self.state = "starting"
        self.error = None
        self.stop = threading.Event()
        self.shutting_down = False

    def config(self):
        with open(self.config_path, encoding="utf-8-sig") as handle:
            return json.load(handle)

    def poll(self, pending):
        cfg = self.config()
        # Even when disabled, report boundaries when the coordinator is up. This
        # lets enabling wait for a job that was started in uncoordinated mode.
        with open(cfg["control_token_file"], encoding="ascii") as handle:
            token = handle.read().strip()
        payload = json.dumps({
            "session": self.session, "pending": pending,
            "completed": self.completed, "unloaded": self.unloaded,
            "cleanup_error": self.cleanup_error,
        }).encode()
        url = f"http://127.0.0.1:{cfg['listen_port']}/coordination/worker"
        req = urllib.request.Request(url, payload, {
            "Content-Type": "application/json", "X-Coordination-Token": token,
        })
        with urllib.request.urlopen(req, timeout=5) as response:
            reply = json.load(response)
        self.completed = None
        self.error = None
        return reply

    def before_get(self, queue, original_get, timeout=None):
        if self.grant is not None:
            self.completed = self.grant
            self.grant = None
        started = time.monotonic()
        while not self.stop.is_set():
            with queue.mutex:
                if self.shutting_down:
                    self.state = "stopping"
                    return None
                pending = len(queue.queue)
            try:
                reply = self.poll(pending)
                action = reply["action"]
                self.state = action
                if action == "unload":
                    try:
                        self.unloaded = {"epoch": reply["epoch"], **self.cleanup()}
                        self.cleanup_error = None
                    except Exception as exc:
                        logging.exception("llama-swap: ComfyUI memory release failed")
                        self.unloaded = {"epoch": reply["epoch"]}
                        self.cleanup_error = str(exc)
                    continue
                if action in ("run", "disabled") and pending:
                    # get(0) never waits under a granted permit. A queue edit may
                    # have removed the last item since the count was read.
                    with queue.mutex:
                        if self.shutting_down:
                            return None
                        item = original_get(timeout=0)
                    self.grant = reply.get("grant") if item is not None else None
                    if item is None and reply.get("grant"):
                        self.completed = reply["grant"]
                    if item is not None:
                        self.state = "running"
                        return item
            except (OSError, ValueError, urllib.error.URLError) as exc:
                self.error = str(exc)
                self.state = "coordinator unavailable"
                # A missing coordinator alone says nothing about GPU ownership.
                # Resume only after confirming the backend and model processes
                # are gone. On restart, polling resumes at the next job boundary.
                try:
                    cfg = self.config()
                    disabled = not cfg["enabled"]
                    stopped = not disabled and backend_stopped(cfg)
                except (OSError, ValueError, KeyError):
                    disabled = stopped = False
                if disabled or stopped:
                    self.error = None
                    self.state = "disabled" if disabled else "llama-swap stopped"
                    with queue.mutex:
                        if self.shutting_down:
                            return None
                        item = original_get(timeout=0)
                    if item is not None:
                        self.state = "running" if disabled else "running (llama-swap stopped)"
                    if item is None:
                        self.stop.wait(min(timeout or 0.5, 0.5))
                    return item
            if timeout is not None and time.monotonic() - started >= timeout:
                return None
            self.stop.wait(0.25)
        return None
