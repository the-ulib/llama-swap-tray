"""Compile the actual tray and exercise its stop action against a fake ComfyUI."""
import asyncio
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

from aiohttp import web
from aiohttp.test_utils import TestServer


@unittest.skipUnless(os.name == "nt", "Windows tray")
class TrayControlTests(unittest.IsolatedAsyncioTestCase):
    async def test_stop_without_llm_service_and_launcher_argv_quoting(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            requests = []

            async def status(request):
                return web.json_response({"stop_supported": True})

            async def stop(request):
                self.assertEqual(request.headers.get("X-Coordination-Token"), "fixture-token")
                requests.append(await request.json())
                return web.json_response({"stopping": True, "backup": None})

            app = web.Application()
            app.router.add_get("/llama_swap/status", status)
            app.router.add_post("/llama_swap/stop", stop)
            server = TestServer(app)
            await server.start_server()
            try:
                (root / "runtime").mkdir()
                (root / "runtime" / "coordination-installed.json").write_text("{}")
                (root / "token").write_text("fixture-token")
                recorder = root / "record launcher args.py"
                recorder.write_text("import json,pathlib,sys; pathlib.Path(sys.argv[1]).write_text(json.dumps(sys.argv[2:]))")
                recorded = root / "launcher args.json"
                args = ["with spaces", 'embedded"quote', "trailing\\", 'space and slash\\', '$(not-a-shell)']
                cfg = {"listen_port": server.port, "server_task_name": "nonexistent-tray-test-task",
                       "comfy_url": str(server.make_url("")), "control_token_file": "token",
                       "comfy_stop_command": [sys.executable, str(recorder), str(recorded), *args]}
                (root / "coordinator.json").write_text(json.dumps(cfg))
                harness = root / "Harness.cs"
                harness.write_text('''using System;
using System.Reflection;
using System.Windows.Forms;
class ControlTest {
    [STAThread] static void Main() {
        var context = new SwapTray.TrayContext();
        var type = context.GetType();
        var flags = BindingFlags.Instance | BindingFlags.NonPublic;
        var icon = (NotifyIcon)type.GetField("icon", flags).GetValue(context);
        icon.Visible = false;
        type.GetField("up", flags).SetValue(context, false);
        type.GetMethod("ApplyMenuState", flags).Invoke(context, null);
        var item = (ToolStripMenuItem)type.GetField("miComfyStop", flags).GetValue(context);
        if (!item.Enabled) throw new Exception("Stop ComfyUI unavailable with LLM service down");
        type.GetMethod("StopComfy", flags).Invoke(context, new object[] { false });
        icon.Dispose();
        context.Dispose();
    }
}''')
                csc = Path(os.environ["WINDIR"]) / "Microsoft.NET/Framework64/v4.0.30319/csc.exe"
                executable = root / "tray-test.exe"
                source = Path(__file__).parents[1] / "setup" / "swap-tray.cs"
                compile_result = subprocess.run([str(csc), "/nologo", "/target:exe", "/main:ControlTest",
                    f"/out:{executable}", "/r:System.Drawing.dll", "/r:System.Windows.Forms.dll",
                    "/r:System.Web.Extensions.dll", str(source), str(harness)], capture_output=True, text=True)
                self.assertEqual(compile_result.returncode, 0, compile_result.stdout + compile_result.stderr)
                process = await asyncio.create_subprocess_exec(str(executable), stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE, creationflags=subprocess.CREATE_NO_WINDOW)
                try:
                    out, err = await asyncio.wait_for(process.communicate(), 25)
                except BaseException:
                    process.kill()
                    await process.wait()
                    raise
                self.assertEqual(process.returncode, 0, (out + err).decode(errors="replace"))
                self.assertEqual(requests, [{"force": False}])
                self.assertEqual(json.loads(recorded.read_text()), args)
            finally:
                await server.close()


if __name__ == "__main__":
    unittest.main()
