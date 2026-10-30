import asyncio
from collections import deque
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import patch
from urllib.parse import urlsplit

from aiohttp import ClientSession, ClientTimeout, web
from aiohttp.test_utils import TestServer
import psutil

from coordination.server import Coordinator, CoordinationError, make_app, windows_job
from coordination.migrate_config import migrate

spec = importlib.util.spec_from_file_location("worker_guard", Path(__file__).parent / "comfy_extension" / "guard.py")
guard_module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(guard_module)
WorkerGuard = guard_module.WorkerGuard


class CoordinationTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.events = []
        self.running = []
        self.allow_answer = asyncio.Event()
        self.allow_answer.set()
        self.answer_started = asyncio.Event()
        self.unload_fails = False
        self.memory_backend = "cuda"

        async def unload(request):
            self.events.append("unload-llm")
            if self.unload_fails:
                return web.Response(status=500)
            self.running.clear()
            return web.json_response({"ok": True})

        async def running(request):
            return web.json_response({"running": self.running})

        async def answer(request):
            self.events.append("llm")
            self.running.append({"model": "test"})
            self.answer_started.set()
            await self.allow_answer.wait()
            response = web.StreamResponse(headers={"Content-Type": "text/event-stream"})
            await response.prepare(request)
            await response.write(b'data: {"part":1}\n\n')
            await asyncio.sleep(0.02)
            await response.write(b'data: [DONE]\n\n')
            return response

        async def echo(request):
            if request.headers.get("Origin") and request.host != urlsplit(request.headers["Origin"]).netloc:
                return web.Response(status=403)
            if request.headers.get("Upgrade", "").lower() == "websocket":
                ws = web.WebSocketResponse()
                await ws.prepare(request)
                async for msg in ws:
                    await ws.send_str(msg.data)
                return ws
            return web.json_response({"raw_path": request.raw_path, "body": await request.text()})

        backend = web.Application()
        backend.router.add_get("/unload", unload)
        backend.router.add_get("/running", running)
        backend.router.add_post("/v1/chat/completions", answer)
        backend.router.add_route("*", "/{path:.*}", echo)
        self.backend = TestServer(backend)
        await self.backend.start_server()

        async def comfy_status(request):
            return web.json_response({"protocol": 1, "session": "worker", "memory_backend": self.memory_backend})

        comfy = web.Application()
        comfy.router.add_get("/llama_swap/status", comfy_status)
        async def stats(request):
            return web.json_response({})
        comfy.router.add_get("/system_stats", stats)
        comfy.router.add_route("*", "/{path:.*}", echo)
        self.comfy = TestServer(comfy)
        await self.comfy.start_server()
        self.cfg = json.loads((Path(__file__).parents[1] / "coordination.example.json").read_text())
        self.cfg["llm_models"] = ["test"]
        self.cfg["blocked_models"] = ["other-gpu-service"]
        self.cfg.update(enabled=True, backend_url=str(self.backend.make_url("")),
                        comfy_url=str(self.comfy.make_url("")), max_wait_seconds=2,
                        control_token_file=str(Path(self.temp.name) / "token"))
        Path(self.cfg["control_token_file"]).write_text("test-token")
        self.path = Path(self.temp.name) / "config.json"
        self.path.write_text(json.dumps(self.cfg))
        self.http = ClientSession(timeout=ClientTimeout(total=5))
        self.c = Coordinator(self.path, self.http)
        self.server = TestServer(make_app(self.c), handler_cancellation=False)
        await self.server.start_server()
        self.tasks = []

    async def asyncTearDown(self):
        self.allow_answer.set()
        for task in self.tasks:
            task.cancel()
        await asyncio.gather(*self.tasks, return_exceptions=True)
        if self.c.release_task is not None:
            await self.c.release_task
        await self.server.close()
        await self.http.close()
        await self.comfy.close()
        await self.backend.close()
        self.temp.cleanup()

    async def poll(self, pending=0, completed=None, unloaded=None, error=None):
        return await self.c.worker({"session": "worker", "pending": pending,
                                    "completed": completed, "unloaded": unloaded,
                                    "cleanup_error": error})

    async def eventually(self, predicate):
        deadline = time.monotonic() + 3
        while not predicate():
            if time.monotonic() > deadline:
                self.fail("Timed out waiting for test condition")
            await asyncio.sleep(0.01)

    async def release_comfy(self, pending=0, completed=None):
        reply = await self.poll(pending, completed)
        self.assertEqual(reply["action"], "unload")
        self.events.append("unload-comfy")
        data = {"epoch": reply["epoch"], "allocated_mib": 0, "free_mib": 30000}
        reply = await self.poll(pending, unloaded=data)
        self.assertEqual(reply["action"], "wait")
        return data

    async def test_running_workflow_then_llm_then_next_workflow(self):
        await self.poll(2)
        await self.eventually(lambda: self.c.backend_clean)
        first = await self.poll(2)
        self.assertEqual(first["action"], "run")
        self.events.append("comfy-1")
        task = asyncio.create_task(self.c.enter("a", "test"))
        self.tasks.append(task)
        await self.eventually(lambda: len(self.c.waiters) == 1)
        self.assertFalse(task.done())
        await self.release_comfy(1, first["grant"])
        self.assertTrue(await task)
        self.events.append("llm")
        self.assertEqual((await self.poll(1))["action"], "wait")
        await self.c.leave("a", True)
        self.assertEqual((await self.poll(1))["action"], "run")
        self.events.append("comfy-2")
        self.assertEqual(self.events, ["unload-llm", "comfy-1", "unload-comfy", "llm", "unload-llm", "comfy-2"])

    async def test_restart_waits_for_a_workflow_without_a_previous_permit(self):
        # ComfyUI can already be executing with the backend stopped. A new
        # coordinator must wait for its next worker boundary before loading.
        task = asyncio.create_task(self.c.enter("after-restart", "test"))
        self.tasks.append(task)
        await self.eventually(lambda: bool(self.c.waiters))
        self.assertFalse(task.done())
        self.assertEqual(self.events, [])
        self.assertFalse(self.c.inflight)
        await self.release_comfy()
        self.assertTrue(await task)
        await self.c.leave("after-restart", True)

    async def test_fairness_between_two_llm_requests(self):
        await self.poll()
        a = asyncio.create_task(self.c.enter("a", "test"))
        await self.eventually(lambda: len(self.c.waiters) == 1)
        b = asyncio.create_task(self.c.enter("b", "test"))
        self.tasks += [a, b]
        await self.eventually(lambda: len(self.c.waiters) == 2)
        await self.release_comfy(1)
        await a
        await self.c.leave("a", True)
        first = await self.poll(1)
        self.assertEqual(first["action"], "run")
        self.assertFalse(b.done())
        await self.release_comfy(0, first["grant"])
        await b
        await self.c.leave("b", True)

    async def test_lost_worker_grant_is_replayed(self):
        await self.poll(1)
        await self.eventually(lambda: self.c.backend_clean)
        grant = await self.poll(1)
        self.assertEqual(grant["action"], "run")
        self.assertEqual(await self.poll(1), grant)
        await self.poll(0, completed=grant["grant"])
        self.assertIsNone(self.c.comfy_grant)

    async def test_second_instance_cannot_replace_active_worker(self):
        await self.poll(1)
        await self.eventually(lambda: self.c.backend_clean)
        grant = await self.poll(1)
        reply = await self.c.worker({"session": "second-instance", "pending": 1})
        self.assertEqual(reply["action"], "wait")
        self.assertEqual(self.c.comfy_grant, grant["grant"])
        self.assertEqual(self.c.worker_session, "worker")

    async def test_absent_comfy_preserves_parallel_calls(self):
        async def absent():
            return None
        self.c.probe_comfy = absent
        self.assertFalse(await self.c.enter("a", "test"))
        self.assertFalse(await self.c.enter("b", "test"))
        self.assertEqual(len(self.c.inflight), 2)
        # ComfyUI starts during inference: the worker must wait.
        self.assertEqual((await self.poll(1))["action"], "wait")
        await self.c.leave("a", False)
        await self.c.leave("b", False)
        await self.poll(1)
        await self.eventually(lambda: self.c.backend_clean)
        self.assertEqual((await self.poll(1))["action"], "run")

    async def test_recent_worker_heartbeat_does_not_hold_absent_comfy(self):
        await self.poll()
        async def absent():
            return None
        self.c.probe_comfy = absent
        self.assertFalse(await self.c.enter("a", "test"))
        await self.c.leave("a", False)

    async def test_slow_windows_connection_refusal_can_confirm_absence(self):
        self.c.process_exists = lambda: False
        with patch.object(self.http, "get", side_effect=asyncio.TimeoutError):
            self.assertIsNone(await self.c.probe_comfy())
        # A new worker invalidates negative caching before getting permission.
        self.assertGreater(self.c.absent_until, time.monotonic())
        await self.poll()
        self.assertEqual(self.c.absent_until, 0)

    async def test_process_census_recognizes_module_launch_in_comfy_root(self):
        class Process:
            info = {"name": "python.exe", "cmdline": ["python", "-m", "main"],
                    "cwd": self.c.cfg["comfy_process_path"]}
        with patch("coordination.server.psutil.process_iter", return_value=[Process()]):
            self.assertTrue(self.c.process_exists())

    async def test_comfy_exits_while_request_waits(self):
        await self.poll(1)
        await self.eventually(lambda: self.c.backend_clean)
        self.assertEqual((await self.poll(1))["action"], "run")
        task = asyncio.create_task(self.c.enter("a", "test"))
        self.tasks.append(task)
        await self.eventually(lambda: bool(self.c.waiters))
        async def absent():
            return None
        self.c.probe_comfy = absent
        self.assertFalse(await task)
        self.assertIsNone(self.c.comfy_grant)
        await self.c.leave("a", False)

    async def test_off_is_persistent_and_does_not_touch_comfy(self):
        await self.c.set_enabled(False)
        self.assertFalse(json.loads(self.path.read_text())["enabled"])
        self.assertEqual((await self.poll(2))["action"], "disabled")
        self.assertFalse(await self.c.enter("a", "test"))
        await self.c.leave("a", False)
        self.assertEqual(self.events, [])

    async def test_unsupported_memory_backend_cannot_enable_coordination(self):
        await self.c.set_enabled(False)
        self.memory_backend = "unsupported"
        with self.assertRaisesRegex(CoordinationError, "CUDA"):
            await self.c.set_enabled(True)
        self.assertFalse(json.loads(self.path.read_text())["enabled"])

    async def test_control_origin_uses_configured_port(self):
        async with self.http.post(self.server.make_url("/coordination/settings"), json={"enabled": False},
                                  headers={"Origin": str(self.server.make_url(""))}) as response:
            self.assertEqual(response.status, 200)

    async def test_get_and_websocket_model_routes_cannot_bypass_admission(self):
        async with self.http.get(self.server.make_url("/v1/audio/voices?model=other-gpu-service")) as response:
            self.assertEqual(response.status, 409)
        async with self.http.get(self.server.make_url("/v1/realtime?model=test"), headers={"Upgrade": "websocket"}) as response:
            self.assertEqual(response.status, 409)

    async def test_disabling_during_inference_keeps_hold_until_unload(self):
        task = asyncio.create_task(self.c.enter("a", "test"))
        self.tasks.append(task)
        await self.eventually(lambda: bool(self.c.waiters))
        await self.release_comfy()
        await task
        await self.c.set_enabled(False)
        self.assertTrue(self.c.enabled())
        self.assertEqual((await self.poll(1))["action"], "wait")
        await self.c.leave("a", True)
        self.assertEqual((await self.poll(1))["action"], "disabled")

    async def test_memory_release_failure_blocks_llm(self):
        task = asyncio.create_task(self.c.enter("a", "test"))
        self.tasks.append(task)
        await self.eventually(lambda: bool(self.c.waiters))
        reply = await self.poll()
        await self.poll(unloaded={"epoch": reply["epoch"], "allocated_mib": 2000, "free_mib": 30000})
        with self.assertRaisesRegex(CoordinationError, "retains GPU tensors"):
            await task
        self.assertEqual(self.events, [])

    async def test_unload_failure_does_not_resume_comfy(self):
        task = asyncio.create_task(self.c.enter("a", "test"))
        self.tasks.append(task)
        await self.eventually(lambda: bool(self.c.waiters))
        await self.release_comfy()
        await task
        self.unload_fails = True
        with self.assertLogs("coordination", level="ERROR"):
            await self.c.leave("a", True)
        self.assertEqual((await self.poll(1))["action"], "wait")
        self.assertEqual(self.c.phase, "LLM running")
        self.assertIsNotNone(self.c.error)
        self.assertIn("a", self.c.inflight)

    async def test_streaming_and_backend_unload(self):
        async def client():
            async with self.http.post(self.server.make_url("/v1/chat/completions"), json={"model": "test", "stream": True}) as response:
                self.assertEqual(response.status, 200)
                return await response.text()
        task = asyncio.create_task(client())
        self.tasks.append(task)
        await self.eventually(lambda: bool(self.c.waiters))
        await self.release_comfy()
        self.assertIn("[DONE]", await task)
        await self.eventually(lambda: not self.c.inflight)
        self.assertEqual(self.events, ["unload-comfy", "llm", "unload-llm"])

    async def test_encoded_workflow_path_and_websocket(self):
        async with self.http.get(self.server.make_url("/comfyui/userdata/workflows%2Ftest.json")) as response:
            self.assertEqual((await response.json())["raw_path"], "/userdata/workflows%2Ftest.json")
        async with self.http.ws_connect(self.server.make_url("/comfyui/ws"), origin=str(self.server.make_url(""))) as ws:
            await ws.send_str("hello")
            self.assertEqual((await ws.receive()).data, "hello")

    async def test_external_settings_and_unsupported_workload_rejected(self):
        async with self.http.post(self.server.make_url("/coordination/settings"), json={"enabled": False}, headers={"Origin": "https://example.com"}) as response:
            self.assertEqual(response.status, 403)
        async with self.http.get(self.server.make_url("/upstream/other-gpu-service/")) as response:
            self.assertEqual(response.status, 409)

    async def test_cancelled_waiter_removed(self):
        task = asyncio.create_task(self.c.enter("a", "test"))
        await self.eventually(lambda: bool(self.c.waiters))
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.assertEqual(len(self.c.waiters), 0)

    async def test_client_disconnect_keeps_gpu_until_backend_finishes(self):
        self.allow_answer.clear()
        client_http = ClientSession()
        request = asyncio.create_task(client_http.post(self.server.make_url("/v1/chat/completions"), json={"model": "test"}))
        await self.eventually(lambda: bool(self.c.waiters))
        await self.release_comfy()
        await self.answer_started.wait()
        request.cancel()
        await asyncio.gather(request, return_exceptions=True)
        await client_http.close()
        self.assertEqual((await self.poll(1))["action"], "wait")
        self.assertEqual(len(self.c.inflight), 1)
        self.allow_answer.set()
        await self.eventually(lambda: not self.c.inflight)
        self.assertEqual((await self.poll(1))["action"], "run")

    async def test_enabling_waits_for_uncoordinated_llm(self):
        await self.c.set_enabled(False)
        await self.c.enter("old", "test")
        await self.c.set_enabled(True)
        self.assertEqual((await self.poll(1))["action"], "wait")
        await self.c.leave("old", False)
        await self.poll(1)
        await self.eventually(lambda: self.c.backend_clean)
        self.assertEqual((await self.poll(1))["action"], "run")

    async def test_recovery_requires_verified_backend_unload(self):
        self.c.managed = "failed"
        self.c.inflight.add("failed")
        self.c.error = "previous failure"
        async with self.http.post(self.server.make_url("/coordination/recover")) as response:
            self.assertEqual(response.status, 200)
        self.assertEqual(len(self.c.inflight), 0)
        self.assertIsNone(self.c.error)
        self.assertEqual(self.events, ["unload-llm"])


class WorkerTests(unittest.TestCase):
    def test_migration_preserves_all_other_models_and_is_idempotent(self):
        original = ('# user comment\nmodels:\n  first:\n    cmd: one\n'
                    '  comfyui_auto:\n    cmd: comfy\n    ttl: 600\n'
                    '  last:\n    cmd: two\ngroups:\n  gpu:\n    members:\n'
                    '      - "first"\n      - "comfyui_auto"\n      - "last"\n')
        migrated = migrate(original, ["comfyui_auto"])
        self.assertNotIn("  comfyui_auto:", migrated)
        self.assertIn('# user comment\nmodels:\n  first:\n    cmd: one\n  last:\n    cmd: two\n', migrated)
        self.assertEqual(migrate(migrated, ["comfyui_auto"]), migrated)

    def test_hold_cleanup_and_original_queue_order(self):
        class Queue:
            mutex = threading.RLock()
            queue = deque(["job-4", "job-5"])

        queue = Queue()
        calls = []
        guard = WorkerGuard("unused", lambda: calls.append(threading.get_ident()) or {"allocated_mib": 0, "free_mib": 30000})
        replies = iter([{"action": "unload", "epoch": "one"}, {"action": "run", "grant": "a"}, {"action": "run", "grant": "b"}])
        guard.poll = lambda pending: next(replies)
        get = lambda timeout: queue.queue.popleft()
        self.assertEqual(guard.before_get(queue, get, 1), "job-4")
        self.assertEqual(guard.before_get(queue, get, 1), "job-5")
        self.assertEqual(calls, [threading.get_ident()])
        self.assertEqual(guard.completed, "a")


@unittest.skipUnless(os.name == "nt", "Windows process lifecycle")
class WindowsLifecycleTests(unittest.TestCase):
    def test_backend_job_kills_descendants_when_handle_closes(self):
        import ctypes
        command = "import subprocess,sys,time; subprocess.Popen([sys.executable,'-c','import time; time.sleep(30)']); time.sleep(30)"
        process = subprocess.Popen([sys._base_executable, "-c", command], creationflags=subprocess.CREATE_NO_WINDOW)
        job = windows_job(process)
        try:
            deadline = time.monotonic() + 5
            children = []
            while not children and time.monotonic() < deadline:
                children = psutil.Process(process.pid).children()
                time.sleep(0.02)
            self.assertEqual(len(children), 1)
            ctypes.windll.kernel32.CloseHandle(ctypes.c_void_p(job))
            job = None
            process.wait(5)
            _, alive = psutil.wait_procs(children, timeout=5)
            self.assertEqual(alive, [])
        finally:
            if job is not None:
                ctypes.windll.kernel32.CloseHandle(ctypes.c_void_p(job))
            if process.poll() is None:
                process.kill()
            process.wait(5)


if __name__ == "__main__":
    unittest.main()
