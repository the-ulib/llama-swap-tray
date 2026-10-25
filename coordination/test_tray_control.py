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
using System.Diagnostics;
using System.Threading;
using System.Windows.Forms;
class ControlTest {
    [STAThread] static void Main() {
        var context = new SwapTray.TrayContext();
        var type = context.GetType();
        var flags = BindingFlags.Instance | BindingFlags.NonPublic;
        var icon = (NotifyIcon)type.GetField("icon", flags).GetValue(context);
        icon.Visible = false;
        var wait = Stopwatch.StartNew();
        while (!(bool)type.GetField("statusKnown", flags).GetValue(context) && wait.ElapsedMilliseconds < 10000) {
            Application.DoEvents();
            Thread.Sleep(10);
        }
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

    async def test_menu_and_ui_timer_stay_responsive_during_slow_checks(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)

            async def stalled_status(request):
                await asyncio.sleep(1)
                return web.json_response({"stop_supported": True})

            app = web.Application()
            app.router.add_get("/llama_swap/status", stalled_status)
            server = TestServer(app)
            await server.start_server()
            try:
                (root / "runtime").mkdir()
                (root / "runtime" / "coordination-installed.json").write_text("{}")
                (root / "coordinator.json").write_text(json.dumps({
                    "listen_port": server.port, "server_task_name": "fixture-task",
                    "comfy_url": str(server.make_url("")), "control_token_file": "token"}))
                fake_query = root / "SlowQuery.cs"
                fake_query.write_text('''using System;
using System.IO;
using System.Threading;
class SlowQuery {
    static void Main() {
        File.WriteAllText(Path.Combine(AppDomain.CurrentDomain.BaseDirectory, "query-started"), "yes");
        Thread.Sleep(1500);
        Console.WriteLine("<Task><Enabled>true</Enabled></Task>");
    }
}''')
                harness = root / "Responsive.cs"
                harness.write_text('''using System;
using System.ComponentModel;
using System.Diagnostics;
using System.IO;
using System.Reflection;
using System.Threading;
using System.Windows.Forms;
class ResponsiveTest {
    [STAThread] static int Main() {
        try { Run(); return 0; }
        catch (Exception ex) { Console.Error.WriteLine(ex); return 1; }
    }
    static void Run() {
        var startup = Stopwatch.StartNew();
        var context = new SwapTray.TrayContext();
        long startupMs = startup.ElapsedMilliseconds;
        var flags = BindingFlags.Instance | BindingFlags.NonPublic;
        var type = context.GetType();
        ((NotifyIcon)type.GetField("icon", flags).GetValue(context)).Visible = false;
        var menu = (ContextMenuStrip)type.GetField("menu", flags).GetValue(context);
        var opening = typeof(ContextMenuStrip).GetMethod("OnOpening", flags);
        int ticks = 0;
        var heartbeat = new System.Windows.Forms.Timer();
        heartbeat.Interval = 20;
        heartbeat.Tick += delegate { ticks++; };
        heartbeat.Start();
        var ready = Stopwatch.StartNew();
        string queryMarker = Path.Combine(AppDomain.CurrentDomain.BaseDirectory, "query-started");
        while (!File.Exists(queryMarker) && ready.ElapsedMilliseconds < 10000) {
            Application.DoEvents();
            Thread.Sleep(10);
        }
        ticks = 0;
        long maximumMs = 0;
        var elapsed = Stopwatch.StartNew();
        while (elapsed.ElapsedMilliseconds < 600) {
            var watch = Stopwatch.StartNew();
            opening.Invoke(menu, new object[] { new CancelEventArgs() });
            maximumMs = Math.Max(maximumMs, watch.ElapsedMilliseconds);
            Application.DoEvents();
            Thread.Sleep(20);
        }
        heartbeat.Dispose();
        context.Dispose();
        // Allow the deliberately slow child query to exit before fixture cleanup.
        Thread.Sleep(1800);
        if (startupMs > 500 || maximumMs > 150 || ticks < 10)
            throw new Exception("Blocked UI: startup=" + startupMs + ", menu=" + maximumMs + ", ticks=" + ticks);
        if (!File.Exists(queryMarker))
            throw new Exception("Slow Task Scheduler fixture was not exercised");
        Console.WriteLine("startup_ms=" + startupMs + ", max_menu_ms=" + maximumMs + ", ui_ticks=" + ticks);
    }
}''')
                csc = Path(os.environ["WINDIR"]) / "Microsoft.NET/Framework64/v4.0.30319/csc.exe"
                for source, executable, extra in (
                    (fake_query, root / "schtasks.exe", []),
                    (harness, root / "responsive.exe", ["/main:ResponsiveTest", "/r:System.Drawing.dll",
                      "/r:System.Windows.Forms.dll", "/r:System.Web.Extensions.dll",
                      str(Path(__file__).parents[1] / "setup" / "swap-tray.cs")])):
                    result = subprocess.run([str(csc), "/nologo", "/target:exe", f"/out:{executable}",
                                             str(source), *extra], capture_output=True, text=True)
                    self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                process = await asyncio.create_subprocess_exec(str(root / "responsive.exe"),
                    cwd=str(root), stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
                    creationflags=subprocess.CREATE_NO_WINDOW)
                try:
                    out, err = await asyncio.wait_for(process.communicate(), 15)
                except BaseException:
                    process.kill()
                    await process.wait()
                    raise
                self.assertEqual(process.returncode, 0, (out + err).decode(errors="replace"))
                print(out.decode().strip())
            finally:
                await server.close()


if __name__ == "__main__":
    unittest.main()
