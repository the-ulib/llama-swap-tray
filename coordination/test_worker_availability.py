"""ComfyUI continues without llama-swap only after absence is verified."""
from collections import deque
import importlib.util
from pathlib import Path
import socket
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch
from urllib.error import URLError

spec = importlib.util.spec_from_file_location(
    "availability_guard", Path(__file__).parent / "comfy_extension" / "guard.py")
guard_module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(guard_module)


class BackendAvailabilityTests(unittest.TestCase):
    def setUp(self):
        self.cfg = {"enabled": True, "backend_url": "http://127.0.0.1:9293",
                    "backend_command": [r"C:\llama-swap\bin\llama-swap.exe"]}

    def check(self, names=(), connection_error=None):
        processes = [SimpleNamespace(info={"name": name}) for name in names]
        with patch.object(guard_module.socket, "create_connection", side_effect=connection_error), \
                patch.object(guard_module.psutil, "process_iter", return_value=processes):
            return guard_module.backend_stopped(self.cfg)

    def test_refused_port_and_no_llama_processes_confirms_stopped(self):
        self.assertTrue(self.check(["python.exe", "explorer.exe"], ConnectionRefusedError()))

    def test_reachable_backend_prevents_bypass_even_if_models_are_idle(self):
        self.assertFalse(self.check())

    def test_timeout_or_other_socket_error_does_not_prove_absence(self):
        for error in (socket.timeout(), OSError("access denied")):
            with self.subTest(error=error):
                self.assertFalse(self.check(connection_error=error))

    def test_backend_or_orphaned_model_process_prevents_bypass(self):
        for name in ("llama-swap.exe", "LLAMA-SERVER.EXE", "llama-server", "llama-swap"):
            with self.subTest(name=name):
                self.assertFalse(self.check([name], ConnectionRefusedError()))

    def test_custom_backend_executable_is_checked(self):
        self.cfg["backend_command"] = [r"C:\custom\renamed-swap.exe"]
        self.assertFalse(self.check(["renamed-swap.exe"], ConnectionRefusedError()))

    def test_unknown_process_name_prevents_bypass(self):
        self.assertFalse(self.check([None], ConnectionRefusedError()))

    def test_denied_process_census_prevents_bypass(self):
        with patch.object(guard_module.socket, "create_connection", side_effect=ConnectionRefusedError()), \
                patch.object(guard_module.psutil, "process_iter", side_effect=guard_module.psutil.AccessDenied()):
            self.assertFalse(guard_module.backend_stopped(self.cfg))

    def test_invalid_or_remote_configuration_prevents_bypass(self):
        for changes in ({"backend_url": "http://192.168.1.1:9293"},
                        {"backend_url": "http://localhost:9293"},
                        {"backend_url": "http://127.0.0.1:invalid"},
                        {"backend_command": []}, {"backend_command": "invalid"}):
            with self.subTest(changes=changes):
                old = self.cfg.copy()
                self.cfg.update(changes)
                self.assertFalse(self.check(connection_error=ConnectionRefusedError()))
                self.cfg = old

    def test_missing_config_prevents_bypass(self):
        self.assertFalse(guard_module.backend_stopped({}))


class WorkerAvailabilityTests(unittest.TestCase):
    def setUp(self):
        self.queue = SimpleNamespace(mutex=threading.RLock(), queue=deque(["first", "second"]))
        self.cleanup = Mock(return_value={"allocated_mib": 0, "free_mib": 30000})
        self.guard = guard_module.WorkerGuard("unused", self.cleanup)
        self.guard.config = Mock(return_value={"enabled": True})
        self.guard.poll = Mock(side_effect=URLError("coordinator offline"))
        self.guard.stop.wait = Mock()

    def get(self, timeout):
        return self.queue.queue.popleft() if self.queue.queue else None

    def test_verified_stop_runs_queued_job_and_clears_connection_error(self):
        with patch.object(guard_module, "backend_stopped", return_value=True):
            self.assertEqual(self.guard.before_get(self.queue, self.get, 0), "first")
        self.assertEqual(list(self.queue.queue), ["second"])
        self.assertEqual(self.guard.state, "running (llama-swap stopped)")
        self.assertIsNone(self.guard.error)
        self.cleanup.assert_not_called()

    def test_unverified_stop_keeps_queue_intact(self):
        with patch.object(guard_module, "backend_stopped", return_value=False):
            self.assertIsNone(self.guard.before_get(self.queue, self.get, 0))
        self.assertEqual(list(self.queue.queue), ["first", "second"])
        self.assertEqual(self.guard.state, "coordinator unavailable")
        self.assertIsNotNone(self.guard.error)

    def test_explicit_disable_does_not_require_backend_absence(self):
        self.guard.config.return_value = {"enabled": False}
        with patch.object(guard_module, "backend_stopped") as probe:
            self.assertEqual(self.guard.before_get(self.queue, self.get, 0), "first")
        probe.assert_not_called()

    def test_restart_rejoins_coordination_before_the_next_job(self):
        with patch.object(guard_module, "backend_stopped", return_value=True):
            self.assertEqual(self.guard.before_get(self.queue, self.get, 0), "first")
        self.guard.poll = Mock(side_effect=[
            {"action": "wait"}, {"action": "unload", "epoch": "restarted"},
            {"action": "run", "grant": "new-permit"}])
        self.assertEqual(self.guard.before_get(self.queue, self.get, 1), "second")
        self.cleanup.assert_called_once_with()
        self.assertEqual(self.guard.grant, "new-permit")
        self.assertEqual(self.guard.poll.call_count, 3)

    def test_shutdown_still_blocks_dequeue(self):
        self.guard.shutting_down = True
        with patch.object(guard_module, "backend_stopped", return_value=True):
            self.assertIsNone(self.guard.before_get(self.queue, self.get, 0))
        self.assertEqual(len(self.queue.queue), 2)


if __name__ == "__main__":
    unittest.main()
