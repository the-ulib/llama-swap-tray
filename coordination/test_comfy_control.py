"""Exercise real HTTP controls without importing ComfyUI or touching a GPU."""
import asyncio
import importlib.util
import json
from pathlib import Path
import tempfile
import threading
import unittest

from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer


def load(name):
    spec = importlib.util.spec_from_file_location(name, Path(__file__).parent / "comfy_extension" / (name + ".py"))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


control = load("control")
WorkerGuard = load("guard").WorkerGuard


class Queue:
    def __init__(self):
        self.mutex = threading.RLock()
        self.queue = []
        self.running = []

    def put(self, item):
        with self.mutex:
            self.queue.append(item)

    def get_current_queue(self):
        with self.mutex:
            return self.running[:], self.queue[:]


class ControlTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        (self.root / "token").write_text("test-token", encoding="ascii")
        path = self.root / "config.json"
        path.write_text(json.dumps({"control_token_file": "token", "enabled": True}))
        self.guard = WorkerGuard(path, lambda: {})
        self.queue = Queue()
        self.exits = []
        routes = web.RouteTableDef()
        control.install_control(routes, self.guard, self.queue, self.exits.append)
        app = web.Application()
        app.add_routes(routes)
        self.client = TestClient(TestServer(app))
        await self.client.start_server()
        self.headers = {"X-Coordination-Token": "test-token"}

    async def asyncTearDown(self):
        await self.client.close()
        self.temp.cleanup()

    async def post(self, force=False, headers=None):
        return await self.client.post("/llama_swap/stop", json={"force": force},
                                      headers=self.headers if headers is None else headers)

    async def test_idle_shutdown_independent_of_coordinator(self):
        response = await self.post()
        self.assertEqual(response.status, 200)
        self.assertTrue((await response.json())["stopping"])
        self.assertTrue(self.guard.shutting_down)
        self.assertEqual(self.exits, [])
        await asyncio.sleep(0.85)
        self.assertEqual(self.exits, [0])
        with self.assertRaises(web.HTTPServiceUnavailable):
            self.queue.put("new-job")

    async def test_busy_refuses_without_changing_queue(self):
        self.queue.running = [[0, "active", {}, {}, []]]
        self.queue.put([1, "pending", {}, {}, []])
        response = await self.post()
        self.assertEqual(response.status, 409)
        body = await response.json()
        self.assertEqual((body["running"], body["pending"]), (1, 1))
        self.assertFalse(self.guard.shutting_down)
        self.assertEqual(len(self.queue.queue), 1)
        self.assertEqual(self.exits, [])

    async def test_confirmed_stop_saves_prompts_before_exit(self):
        self.queue.running = [[0, "active", {"1": {"inputs": {}}}, {}, ["1"], {"secret": "excluded"}]]
        self.queue.put([1, "next", {}, {"workflow": "example"}, []])
        response = await self.post(True)
        self.assertEqual(response.status, 200)
        body = await response.json()
        saved = json.loads(Path(body["backup"]).read_text())
        self.assertEqual(saved["queue_running"][0][1], "active")
        self.assertEqual(saved["queue_pending"][0][1], "next")
        self.assertNotIn("secret", json.dumps(saved))
        self.assertTrue(self.guard.shutting_down)

    async def test_failed_backup_keeps_app_accepting_jobs(self):
        (self.root / "token").unlink()
        # Token is readable but its parent cannot be used as a backup directory.
        self.guard.config = lambda: {"control_token_file": str(self.root / "missing" / "token")}
        self.queue.put([1, "pending", {}, {}, []])
        with self.assertRaises(OSError):
            control.prepare_stop(self.guard, self.queue, True)
        self.assertFalse(self.guard.shutting_down)
        self.queue.put("still-accepting")

    async def test_auth_browser_and_invalid_force_rejected(self):
        for headers in ({}, {"X-Coordination-Token": "wrong"},
                        dict(self.headers, Origin="http://localhost")):
            self.assertEqual((await self.post(headers=headers)).status, 403)
        self.assertEqual((await self.post("true")).status, 400)
        self.assertFalse(self.guard.shutting_down)

    async def test_remote_request_rejected_even_with_token(self):
        from aiohttp.test_utils import make_mocked_request
        from unittest.mock import Mock
        request = make_mocked_request("POST", "/llama_swap/stop", headers=self.headers,
                                      transport=Mock(get_extra_info=lambda key, default=None: ("192.0.2.1", 1234)))
        handler = next(route.handler for route in self.client.app.router.routes())
        with self.assertRaises(web.HTTPForbidden):
            await handler(request)

    async def test_shutdown_racing_with_worker_grant_cannot_dispatch(self):
        self.queue.put([1, "pending", {}, {}, []])
        def poll(pending):
            control.prepare_stop(self.guard, self.queue, True)
            return {"action": "run", "grant": "race"}
        self.guard.poll = poll
        def get(timeout):
            self.fail("Worker dispatched after shutdown was accepted")
        self.assertIsNone(self.guard.before_get(self.queue, get, 1))
        self.assertEqual(len(self.queue.queue), 1)


if __name__ == "__main__":
    unittest.main()
