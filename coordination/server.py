"""Local ComfyUI/llama-swap arbitration and streaming reverse proxy."""
import argparse
import asyncio
from collections import deque
import contextlib
import ipaddress
import json
import logging
import os
from pathlib import Path
import secrets
import subprocess
import time
import uuid

from aiohttp import ClientConnectionError, ClientSession, ClientTimeout, DummyCookieJar, WSMsgType, web
import psutil
from yarl import URL

LOG = logging.getLogger("coordination")
HOP_HEADERS = {"connection", "keep-alive", "proxy-authenticate", "proxy-authorization",
               "te", "trailer", "transfer-encoding", "upgrade", "host"}


class CoordinationError(Exception):
    pass


def read_config(path):
    path = Path(path).resolve()
    cfg = json.loads(Path(path).read_text(encoding="utf-8-sig"))
    if type(cfg["enabled"]) is not bool:
        raise ValueError("enabled must be true or false")
    if cfg["max_wait_seconds"] <= 0:
        raise ValueError("max_wait_seconds must be positive")
    for field in ("control_token_file", "comfy_process_path"):
        value = Path(cfg[field])
        if not value.is_absolute():
            cfg[field] = str(path.parent / value)
    return cfg


class Coordinator:
    def __init__(self, config_path, session):
        self.config_path = Path(config_path)
        self.cfg = read_config(config_path)
        self.http = session
        self.condition = asyncio.Condition()
        self.waiters = deque()
        self.inflight = set()
        self.managed = None
        self.comfy_grant = None
        self.worker_session = None
        self.worker_seen = 0
        self.pending = 0
        self.turn = "llm"
        self.epoch = uuid.uuid4().hex
        self.released = None
        self.backend_clean = False
        self.release_task = None
        self.error = None
        self.phase = "idle"
        self.started = time.time()
        self.start_lock = asyncio.Lock()
        self.absent_until = 0
        self.token = Path(self.cfg["control_token_file"]).read_text(encoding="ascii").strip()

    def reload(self):
        cfg = read_config(self.config_path)
        if cfg["enabled"] != self.cfg["enabled"]:
            self.epoch = uuid.uuid4().hex
            self.released = None
            LOG.info("Coordination requested: %s", cfg["enabled"])
        self.cfg = cfg

    def enabled(self):
        # Turning off cannot release a hold while an admitted LLM is still using it.
        return self.cfg["enabled"] or self.managed is not None

    def process_exists(self):
        target = os.path.normcase(os.path.realpath(self.cfg["comfy_process_path"]))
        for proc in psutil.process_iter(["name", "cmdline", "cwd"]):
            try:
                if "python" not in (proc.info["name"] or "").lower():
                    continue
                if proc.info["cmdline"] is None:
                    return True
                args = proc.info["cmdline"] or []
                raw_cwd = proc.info["cwd"] or ""
                cwd = os.path.normcase(os.path.realpath(raw_cwd)) if raw_cwd else ""
                if cwd == target:
                    return True
                for arg in args:
                    if os.path.basename(arg).lower() == "main.py":
                        script = os.path.normcase(os.path.realpath(os.path.join(raw_cwd, arg)))
                        if os.path.dirname(script) == target:
                            return True
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                # If this is an inaccessible Python process, absence is unproven.
                return True
        return False

    async def probe_comfy(self):
        if time.monotonic() < self.absent_until:
            return None
        try:
            async with self.http.get(self.cfg["comfy_url"] + "/llama_swap/status",
                                     timeout=ClientTimeout(total=0.5)) as response:
                if response.status != 200:
                    raise CoordinationError("ComfyUI extension is missing; restart ComfyUI after installation")
                data = await response.json()
                if data.get("protocol") != 1:
                    raise CoordinationError("Unsupported ComfyUI coordination protocol")
                if data.get("memory_backend") != "cuda":
                    raise CoordinationError("ComfyUI coordination currently requires its PyTorch CUDA memory interface")
                return data
        except (ClientConnectionError, asyncio.TimeoutError) as exc:
            if await asyncio.to_thread(self.process_exists):
                raise CoordinationError("ComfyUI is starting or unresponsive; waiting for its extension") from exc
            # Windows can take ~2s to report a refused loopback connection. A
            # bounded probe plus process census confirms absence sooner. A new
            # worker invalidates this cache before it can start any workflow.
            self.absent_until = time.monotonic() + 1
            return None

    async def unload_backend(self):
        async with self.http.get(self.cfg["backend_url"] + "/unload",
                                 timeout=ClientTimeout(total=120)) as response:
            if response.status >= 400:
                raise CoordinationError(f"llama-swap unload returned HTTP {response.status}")
            await response.read()
        deadline = time.monotonic() + 120
        while True:
            async with self.http.get(self.cfg["backend_url"] + "/running",
                                     timeout=ClientTimeout(total=5)) as response:
                response.raise_for_status()
                data = await response.json()
            if not data["running"]:
                return
            if time.monotonic() >= deadline:
                raise CoordinationError("llama-swap models did not finish unloading")
            await asyncio.sleep(0.25)

    async def release_for_comfy(self):
        try:
            await self.unload_backend()
            async with self.condition:
                self.backend_clean = True
                self.error = None
                self.condition.notify_all()
        except Exception as exc:
            async with self.condition:
                self.error = "Cannot return GPU to ComfyUI: " + str(exc)
                LOG.exception(self.error)
                self.condition.notify_all()

    async def worker(self, data):
        async with self.condition:
            self.reload()
            if data["session"] != self.worker_session:
                # A second ComfyUI can start its worker before failing to bind
                # the occupied port. Only the instance serving the configured
                # API may replace an existing permit owner.
                try:
                    async with self.http.get(self.cfg["comfy_url"] + "/llama_swap/status",
                                             timeout=ClientTimeout(total=0.5)) as response:
                        if response.status != 200:
                            return {"action": "wait"}
                        current = await response.json()
                        if current.get("protocol") != 1 or current.get("session") != data["session"]:
                            return {"action": "wait"}
                except (ClientConnectionError, asyncio.TimeoutError):
                    return {"action": "wait"}
                self.worker_session = data["session"]
                self.comfy_grant = None
                self.released = None
                self.epoch = uuid.uuid4().hex
            self.worker_seen = time.monotonic()
            self.absent_until = 0
            self.pending = int(data["pending"])
            if data.get("completed") == self.comfy_grant and self.comfy_grant is not None:
                self.comfy_grant = None
                self.turn = "llm"
                self.epoch = uuid.uuid4().hex
                self.released = None
            if self.comfy_grant is not None:
                # Only the worker thread polls this endpoint, and only before
                # queue.get. A retry without completion means its grant reply
                # was lost; replaying it cannot start a second concurrent job.
                return {"action": "run", "grant": self.comfy_grant}
            if not self.enabled():
                self.phase = "disabled"
                self.condition.notify_all()
                return {"action": "disabled"}
            released = data.get("unloaded")
            if data.get("cleanup_error") and released and released["epoch"] == self.epoch:
                self.error = "ComfyUI memory release failed: " + data["cleanup_error"]
            if released and released["epoch"] == self.epoch and not data.get("cleanup_error"):
                if released["allocated_mib"] > self.cfg["max_comfy_allocated_mib"]:
                    self.error = "ComfyUI retains GPU tensors after cleanup; queue held"
                elif released.get("free_mib") is None:
                    self.error = "ComfyUI could not measure free CUDA memory"
                else:
                    if self.released is None:
                        LOG.info("ComfyUI memory released: %.0f MiB allocated, %.0f MiB free",
                                 released["allocated_mib"], released["free_mib"])
                    self.released = released
                    self.error = None
            self.condition.notify_all()
            if self.inflight:
                self.phase = "LLM running"
                return {"action": "wait"}
            if self.pending and (not self.waiters or self.turn == "comfy"):
                if not self.backend_clean:
                    self.phase = "unloading LLM for ComfyUI"
                    if self.release_task is None or self.release_task.done():
                        self.release_task = asyncio.create_task(self.release_for_comfy())
                    return {"action": "wait"}
                self.comfy_grant = uuid.uuid4().hex
                self.phase = "ComfyUI running"
                LOG.info("ComfyUI workflow admitted; %s queued at boundary", self.pending)
                return {"action": "run", "grant": self.comfy_grant}
            if self.waiters:
                self.phase = "releasing ComfyUI memory"
                if self.error:
                    return {"action": "wait"}
                if self.released is None:
                    return {"action": "unload", "epoch": self.epoch}
                self.phase = "ComfyUI held for LLM"
            else:
                self.phase = "idle"
            return {"action": "wait"}

    async def enter(self, request_id, model, disconnected=lambda: False):
        self.reload()
        probe_started = time.monotonic()
        comfy = await self.probe_comfy() if self.enabled() else None
        async with self.condition:
            # A fresh worker can join between the probe and this lock. Its first
            # poll cannot grant a job once this inference is registered.
            present = comfy is not None or self.worker_seen > probe_started or self.managed is not None
            if comfy is None and self.worker_seen <= probe_started:
                self.comfy_grant = None
                self.pending = 0
                self.worker_session = None
                self.released = None
            if not self.enabled() or not present:
                self.inflight.add(request_id)
                self.backend_clean = False
                return False
            self.waiters.append(request_id)
            self.condition.notify_all()
            deadline = time.monotonic() + self.cfg["max_wait_seconds"]
            last_probe = time.monotonic()
            try:
                while True:
                    self.reload()
                    if disconnected():
                        raise CoordinationError("Client disconnected while waiting for ComfyUI")
                    if not self.cfg["enabled"] and self.managed is None:
                        self.inflight.add(request_id)
                        self.backend_clean = False
                        return False
                    if self.error:
                        raise CoordinationError(self.error)
                    if time.monotonic() - last_probe >= 1:
                        latest = await self.probe_comfy()
                        last_probe = time.monotonic()
                        if latest is None and self.managed is None:
                            self.comfy_grant = None
                            self.pending = 0
                            self.worker_session = None
                            self.released = None
                            self.inflight.add(request_id)
                            self.backend_clean = False
                            return False
                    ready = (self.waiters[0] == request_id and not self.inflight
                             and self.comfy_grant is None and self.released is not None
                             and time.monotonic() - self.worker_seen < self.cfg["worker_timeout_seconds"]
                             and (not self.pending or self.turn == "llm"))
                    if ready:
                        need = self.cfg["model_minimum_free_vram_mib"].get(
                            model, self.cfg["minimum_free_vram_mib"])
                        if self.released["free_mib"] < need:
                            raise CoordinationError(f"Only {self.released['free_mib']:.0f} MiB VRAM free; {need} required")
                        self.managed = request_id
                        self.inflight.add(request_id)
                        self.backend_clean = False
                        self.phase = "LLM running"
                        LOG.info("LLM admitted: %s", model)
                        return True
                    if time.monotonic() >= deadline:
                        raise CoordinationError("Timed out waiting for a ComfyUI workflow boundary")
                    with contextlib.suppress(asyncio.TimeoutError):
                        await asyncio.wait_for(self.condition.wait(), timeout=0.25)
            finally:
                self.waiters.remove(request_id)
                self.condition.notify_all()

    async def leave(self, request_id, managed):
        # Keep the lease during unload. A disconnected client does not imply the
        # backend has finished executing or released its CUDA allocations.
        error = None
        if managed:
            try:
                await self.unload_backend()
            except Exception as exc:
                error = "LLM unload failed; ComfyUI remains held: " + str(exc)
                LOG.exception(error)
        async with self.condition:
            if error:
                self.error = error
                self.phase = "recovery required"
                return
            self.inflight.discard(request_id)
            if self.managed == request_id:
                self.managed = None
                self.backend_clean = True
                self.turn = "comfy"
                LOG.info("LLM unloaded; returning GPU to ComfyUI")
            self.condition.notify_all()

    def snapshot(self):
        self.reload()
        return {"enabled": self.cfg["enabled"], "effective_enabled": self.enabled(),
                "phase": self.phase, "llm_active": len(self.inflight),
                "llm_waiting": len(self.waiters), "comfy_active": self.comfy_grant is not None,
                "comfy_pending": self.pending, "error": self.error,
                "worker_connected": time.monotonic() - self.worker_seen < 2,
                "uptime_seconds": round(time.time() - self.started)}

    async def set_enabled(self, value):
        if type(value) is not bool:
            raise ValueError("enabled must be true or false")
        if value:
            await self.probe_comfy()
        async with self.condition:
            cfg = read_config(self.config_path)
            cfg["enabled"] = value
            temporary = self.config_path.with_suffix(".json.tmp")
            temporary.write_text(json.dumps(cfg, indent=2) + "\n", encoding="utf-8")
            temporary.replace(self.config_path)
            self.reload()
            self.condition.notify_all()
        return self.snapshot()


def local_request(request):
    try:
        local = ipaddress.ip_address(request.remote).is_loopback
    except (ValueError, TypeError):
        local = False
    origin = request.headers.get("Origin")
    if not origin:
        return local
    try:
        parsed = URL(origin)
        return local and parsed.host in ("127.0.0.1", "localhost", "::1") and parsed.port == request.url.port
    except ValueError:
        return False


def proxy_headers(headers):
    extra = {name.strip().lower() for name in headers.get("Connection", "").split(",")}
    return [(key, value) for key, value in headers.items()
            if key.lower() not in HOP_HEADERS | extra]


async def websocket_proxy(request, http, url, headers):
    protocols = [p.strip() for p in request.headers.get("Sec-WebSocket-Protocol", "").split(",") if p.strip()]
    async with http.ws_connect(url, headers=headers, autoping=False, protocols=protocols) as upstream:
        downstream = web.WebSocketResponse(autoping=False, protocols=upstream.protocol and [upstream.protocol] or [])
        await downstream.prepare(request)

        async def copy(source, target):
            async for message in source:
                if message.type == WSMsgType.TEXT:
                    await target.send_str(message.data)
                elif message.type == WSMsgType.BINARY:
                    await target.send_bytes(message.data)
                elif message.type == WSMsgType.PING:
                    await target.ping(message.data)
                elif message.type == WSMsgType.PONG:
                    await target.pong(message.data)
                else:
                    break
        tasks = [asyncio.create_task(copy(upstream, downstream)),
                 asyncio.create_task(copy(downstream, upstream))]
        try:
            await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
        finally:
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            await downstream.close()
        return downstream


async def forward(request, http, base, path=None):
    # encoded=True preserves ComfyUI workflow names containing encoded slashes.
    url = URL(base.rstrip("/") + (path if path is not None else request.raw_path), encoded=True)
    headers = proxy_headers(request.headers)
    # Keep the browser-facing authority. ComfyUI compares Host and Origin,
    # including their ports, before accepting UI websocket and API requests.
    headers.append(("Host", request.host))
    if request.headers.get("Upgrade", "").lower() == "websocket":
        headers = [(k, v) for k, v in headers if not k.lower().startswith("sec-websocket-")]
        return await websocket_proxy(request, http, url, headers)
    body = await request.read()
    async with http.request(request.method, url, headers=headers, data=body,
                            allow_redirects=False) as upstream:
        downstream = web.StreamResponse(status=upstream.status, headers=proxy_headers(upstream.headers))
        gone = False
        try:
            await downstream.prepare(request)
        except (ConnectionError, RuntimeError):
            gone = True
        async for chunk in upstream.content.iter_any():
            if not gone:
                try:
                    await downstream.write(chunk)
                except (ConnectionError, RuntimeError):
                    gone = True
        if not gone:
            with contextlib.suppress(ConnectionError, RuntimeError):
                await downstream.write_eof()
        return downstream


def make_app(coordinator):
    app = web.Application(client_max_size=512 * 1024 * 1024)
    c = coordinator

    async def status(request):
        return web.json_response(c.snapshot())

    async def settings(request):
        if not local_request(request):
            raise web.HTTPForbidden(text="Settings are local-only")
        try:
            return web.json_response(await c.set_enabled((await request.json())["enabled"]))
        except (ValueError, KeyError, CoordinationError) as exc:
            raise web.HTTPBadRequest(text=str(exc)) from exc

    async def worker(request):
        if not local_request(request) or not secrets.compare_digest(
                request.headers.get("X-Coordination-Token", ""), c.token):
            raise web.HTTPForbidden(text="Invalid local worker token")
        try:
            return web.json_response(await c.worker(await request.json()))
        except (CoordinationError, ValueError, KeyError) as exc:
            return web.json_response({"error": str(exc)}, status=409)

    async def recover(request):
        if not local_request(request):
            raise web.HTTPForbidden()
        # Called after a failure, never in parallel with a live proxy handler.
        live = [task for task in asyncio.all_tasks() if task.get_name().startswith("llm:") and not task.done()]
        if live or c.comfy_grant:
            raise web.HTTPConflict(text="A workflow or inference request is still active")
        await c.unload_backend()
        async with c.condition:
            c.inflight.clear()
            c.managed = None
            c.backend_clean = True
            c.error = None
            c.turn = "comfy"
            c.epoch = uuid.uuid4().hex
            c.released = None
            c.condition.notify_all()
        return web.json_response(c.snapshot())

    async def proxy(request):
        c.reload()
        path = request.path
        if path == "/comfyui":
            raise web.HTTPFound(location="comfyui/")
        if path.startswith("/comfyui/"):
            tail = request.raw_path[len("/comfyui"):]
            try:
                async with c.http.get(c.cfg["comfy_url"] + "/system_stats",
                                      timeout=ClientTimeout(total=2)) as response:
                    response.raise_for_status()
                    await response.read()
            except ClientConnectionError:
                if request.method == "GET" and path == "/comfyui/":
                    if not c.cfg["comfy_start_command"]:
                        return web.Response(status=503, text="Start ComfyUI normally; automatic startup is not configured")
                    async with c.start_lock:
                        if not await asyncio.to_thread(c.process_exists):
                            subprocess.Popen(c.cfg["comfy_start_command"], creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
                    return web.Response(status=503, text="ComfyUI is starting. Reload shortly.", headers={"Retry-After": "5"})
                return web.Response(status=503, text="ComfyUI is not running", headers={"Retry-After": "5"})
            return await forward(request, c.http, c.cfg["comfy_url"], tail)
        inference = ((path.startswith("/v1/") and path != "/v1/models" and not path.startswith("/v1/models/"))
                     or path.startswith("/upstream/"))
        model = request.query.get("model", "")
        if inference:
            if request.can_read_body:
                with contextlib.suppress(ValueError):
                    body = await request.json()
                    model = body.get("model", "") if isinstance(body, dict) else ""
            if path.startswith("/upstream/"):
                model = path.split("/", 3)[2]
            if c.enabled() and (model not in c.cfg["llm_models"] or model in c.cfg["blocked_models"]
                                or request.headers.get("Upgrade", "").lower() == "websocket"):
                return web.json_response({"error": {"message": "Only configured synchronous LLM models may run in coordination mode; disable coordination to use this service"}}, status=409)
        # Explicit model loads would bypass request admission. Normal dashboard,
        # activity, logs, unload and list requests remain available.
        if c.enabled() and (("/load" in path and "unload" not in path)
                                  or (path.startswith("/api/") and request.method in ("POST", "PUT", "DELETE") and not path.startswith("/api/models/unload"))):
            return web.json_response({"error": {"message": "Manual runtime changes require coordination to be disabled"}}, status=409)
        if not inference:
            return await forward(request, c.http, c.cfg["backend_url"])
        request_id = uuid.uuid4().hex
        managed = False
        entered = False
        asyncio.current_task().set_name("llm:" + request_id)
        try:
            managed = await c.enter(request_id, model, lambda: request.transport is None or request.transport.is_closing())
            entered = True
            return await forward(request, c.http, c.cfg["backend_url"])
        except CoordinationError as exc:
            return web.json_response({"error": {"message": str(exc), "type": "gpu_coordination"}}, status=503, headers={"Retry-After": "5"})
        finally:
            if entered:
                cleanup = asyncio.create_task(c.leave(request_id, managed))
                try:
                    await asyncio.shield(cleanup)
                except asyncio.CancelledError:
                    await cleanup
                    raise

    app.router.add_get("/coordination/status", status)
    app.router.add_post("/coordination/settings", settings)
    app.router.add_post("/coordination/worker", worker)
    app.router.add_post("/coordination/recover", recover)
    app.router.add_route("*", "/{path:.*}", proxy)
    return app


def windows_job(process):
    """Kill backend and its descendants if the coordinator dies (not ComfyUI)."""
    import ctypes
    from ctypes import wintypes

    class BASIC(ctypes.Structure):
        _fields_ = [("PerProcessUserTimeLimit", ctypes.c_int64), ("PerJobUserTimeLimit", ctypes.c_int64),
                    ("LimitFlags", wintypes.DWORD), ("MinimumWorkingSetSize", ctypes.c_size_t),
                    ("MaximumWorkingSetSize", ctypes.c_size_t), ("ActiveProcessLimit", wintypes.DWORD),
                    ("Affinity", ctypes.c_size_t), ("PriorityClass", wintypes.DWORD), ("SchedulingClass", wintypes.DWORD)]

    class IO(ctypes.Structure):
        _fields_ = [(name, ctypes.c_uint64) for name in ("ReadOperationCount", "WriteOperationCount", "OtherOperationCount", "ReadTransferCount", "WriteTransferCount", "OtherTransferCount")]

    class EXTENDED(ctypes.Structure):
        _fields_ = [("BasicLimitInformation", BASIC), ("IoInfo", IO), ("ProcessMemoryLimit", ctypes.c_size_t),
                    ("JobMemoryLimit", ctypes.c_size_t), ("PeakProcessMemoryUsed", ctypes.c_size_t), ("PeakJobMemoryUsed", ctypes.c_size_t)]
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.CreateJobObjectW.restype = wintypes.HANDLE
    kernel.CreateJobObjectW.argtypes = [ctypes.c_void_p, wintypes.LPCWSTR]
    kernel.SetInformationJobObject.argtypes = [wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD]
    kernel.AssignProcessToJobObject.argtypes = [wintypes.HANDLE, wintypes.HANDLE]
    job = kernel.CreateJobObjectW(None, None)
    limits = EXTENDED()
    limits.BasicLimitInformation.LimitFlags = 0x2000  # JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
    if not job or not kernel.SetInformationJobObject(job, 9, ctypes.byref(limits), ctypes.sizeof(limits)) or not kernel.AssignProcessToJobObject(job, int(process._handle)):
        process.terminate()
        raise ctypes.WinError(ctypes.get_last_error())
    return job


async def serve(path, external_backend=False):
    cfg = read_config(path)
    token_file = Path(cfg["control_token_file"])
    token_file.parent.mkdir(parents=True, exist_ok=True)
    if not token_file.exists():
        token_file.write_text(secrets.token_hex(32), encoding="ascii")
    process = None
    job = None
    log = None
    if not external_backend:
        log = open(token_file.parent / "llama-swap.log", "ab", buffering=0)
        process = subprocess.Popen(cfg["backend_command"], stdout=log, stderr=log,
                                   creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        if os.name == "nt":
            job = windows_job(process)
    async with ClientSession(timeout=ClientTimeout(total=None, sock_connect=5), auto_decompress=False,
                             cookie_jar=DummyCookieJar()) as http:
        c = Coordinator(path, http)
        runner = web.AppRunner(make_app(c), handler_cancellation=False, shutdown_timeout=120, access_log=None)
        await runner.setup()
        site = web.TCPSite(runner, cfg["listen_host"], cfg["listen_port"])
        try:
            await site.start()
            LOG.info("Listening on %s:%s; coordination %s", cfg["listen_host"], cfg["listen_port"], cfg["enabled"])
            while process is None or process.poll() is None:
                await asyncio.sleep(1)
            raise RuntimeError("llama-swap backend exited")
        finally:
            await runner.cleanup()
            if c.release_task is not None:
                await c.release_task
            if process is not None:
                if job is not None:
                    import ctypes
                    ctypes.windll.kernel32.CloseHandle(ctypes.c_void_p(job))
                elif process.poll() is None:
                    process.terminate()
                await asyncio.to_thread(process.wait)
            if log is not None:
                log.close()


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", default=str(Path(__file__).resolve().parents[1] / "coordinator.json"))
    parser.add_argument("--external-backend", action="store_true")
    args = parser.parse_args()
    log_dir = Path(args.config).resolve().parent / "runtime"
    log_dir.mkdir(parents=True, exist_ok=True)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s",
                        handlers=[logging.FileHandler(log_dir / "coordinator.log", encoding="utf-8")])
    with contextlib.suppress(KeyboardInterrupt):
        asyncio.run(serve(args.config, args.external_backend))


if __name__ == "__main__":
    main()
