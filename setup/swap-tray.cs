// llama-swap tray app (a real windowless Windows application)
// Built by setup\04-tray-task.ps1 with the C# compiler that ships with Windows:
//   csc /target:winexe /out:swap-tray.exe /r:System.Drawing.dll /r:System.Windows.Forms.dll /r:System.Web.Extensions.dll swap-tray.cs
using System;
using System.Collections;
using System.Collections.Generic;
using System.Diagnostics;
using System.Drawing;
using System.Drawing.Drawing2D;
using System.IO;
using System.Net;
using System.Threading;
using System.Web.Script.Serialization;
using System.Windows.Forms;

namespace SwapTray
{
    static class Program
    {
        [STAThread]
        static void Main()
        {
            bool createdNew;
            using (var mutex = new Mutex(true, "llama-swap-tray-single-instance", out createdNew))
            {
                if (!createdNew) return; // another instance is already running
                Application.EnableVisualStyles();
                Application.Run(new TrayContext());
            }
        }
    }

    class TrayContext : ApplicationContext
    {
        // Match setup\03-server-task.ps1; use IPv4 to avoid localhost IPv6 timeouts.
        const string Api = "http://127.0.0.1:9292";
        const string TaskName = "llama-swap\\server";
        static string apiOverride, taskOverride;
        static string ApiUrl { get { return apiOverride ?? Api; } }
        static string ActiveTaskName { get { return taskOverride ?? TaskName; } }

        NotifyIcon icon;
        Icon icoIdle, icoLoaded, icoOff;
        ToolStripMenuItem miStatus, miUnload, miStop, miStart, miAuto, miCoord, miComfyStop;
        ContextMenuStrip menu;
        Control dispatcher;
        int refreshPending;
        volatile bool closing;
        bool statusKnown;
        bool? autostartEnabled;

        sealed class TrayState
        {
            public bool up, comfyCanStop, coordinationAvailable, coordinationEnabled;
            public bool? autostartEnabled;
            public string coordinationPhase = "";
            public List<string> models = new List<string>();
        }
        string comfyUrl, comfyTokenPath;
        bool comfyCanStop;
        string[] comfyStopCommand = new string[0];
        bool coordinationInstalled, coordinationAvailable, coordinationEnabled, configurationError;
        string coordinationPhase = "";
        System.Windows.Forms.Timer timer;
        bool up;
        List<string> models = new List<string>();

        public TrayContext()
        {
            string marker = Path.Combine(AppDomain.CurrentDomain.BaseDirectory, "runtime", "coordination-installed.json");
            string settings = Path.Combine(AppDomain.CurrentDomain.BaseDirectory, "coordinator.json");
            if (File.Exists(marker) && File.Exists(settings))
            {
                try
                {
                    var cfg = new JavaScriptSerializer().Deserialize<Dictionary<string, object>>(File.ReadAllText(settings));
                    apiOverride = "http://127.0.0.1:" + Convert.ToInt32(cfg["listen_port"]);
                    taskOverride = Convert.ToString(cfg["server_task_name"]);
                    coordinationInstalled = true;
                }
                catch { configurationError = true; }
            }
            dispatcher = new Control();
            dispatcher.CreateControl();
            LoadComfyControl();
            icoIdle   = MakeIcon(Color.FromArgb(46, 160, 67));    // green: running, VRAM free
            icoLoaded = MakeIcon(Color.FromArgb(31, 111, 235));   // blue: models loaded
            icoOff    = MakeIcon(Color.FromArgb(110, 118, 129));  // gray: stopped

            miStatus = new ToolStripMenuItem("Status: ...");
            miStatus.Enabled = false;
            var miDash = new ToolStripMenuItem("Open dashboard", null, delegate { OpenDashboard(); });
            var miConfig = new ToolStripMenuItem("Edit config.yaml", null, delegate { OpenConfig(); });
            miUnload = new ToolStripMenuItem("Unload models (free VRAM)", null, OnUnload);
            miStop = new ToolStripMenuItem("STOP llama-swap (gaming mode)", null, OnStop);
            miStart = new ToolStripMenuItem("Start llama-swap", null, OnStart);
            miAuto = new ToolStripMenuItem("Autostart...", null, OnToggleAutostart);
            miCoord = new ToolStripMenuItem("Coordinate with ComfyUI", null, OnToggleCoordination);
            miComfyStop = new ToolStripMenuItem("Stop ComfyUI", null, OnStopComfy);
            miComfyStop.Visible = comfyUrl != null;
            var miExit = new ToolStripMenuItem("Quit tray", null, delegate { ExitThread(); });

            menu = new ContextMenuStrip();
            menu.Items.AddRange(new ToolStripItem[] {
                miStatus, new ToolStripSeparator(),
                miDash, miConfig, miUnload, miStop, miStart, miCoord, miComfyStop,
                new ToolStripSeparator(), miAuto,
                new ToolStripSeparator(), miExit });
            menu.Opening += delegate { ApplyMenuState(); RefreshState(); };

            icon = new NotifyIcon();
            icon.Icon = icoOff;
            icon.Text = "llama-swap";
            icon.ContextMenuStrip = menu;
            icon.DoubleClick += delegate { OpenDashboard(); };
            icon.Visible = true;

            timer = new System.Windows.Forms.Timer();
            timer.Interval = 5000;
            timer.Tick += delegate { RefreshState(); };
            timer.Start();
            ApplyMenuState();
            RefreshState();
        }

        protected override void ExitThreadCore()
        {
            Dispose();
            base.ExitThreadCore();
        }

        protected override void Dispose(bool disposing)
        {
            if (disposing && !closing)
            {
                closing = true;
                timer.Stop();
                timer.Dispose();
                icon.Visible = false;
                icon.Dispose();
                menu.Dispose();
                dispatcher.Dispose();
            }
            base.Dispose(disposing);
        }

        void OpenDashboard()
        {
            try { Process.Start(ApiUrl + "/ui"); } catch { }
        }

        void OpenConfig()
        {
            // config.yaml sits next to the exe (repo root)
            string cfg = Path.Combine(AppDomain.CurrentDomain.BaseDirectory, "config.yaml");
            if (!File.Exists(cfg))
            {
                Balloon("config.yaml not found: " + cfg);
                return;
            }
            try { Process.Start(cfg); }                                   // default editor
            catch { try { Process.Start("notepad.exe", "\"" + cfg + "\""); } catch { } }
            Balloon("Changes apply automatically (watch-config).");
        }

        static Icon MakeIcon(Color c)
        {
            var bmp = new Bitmap(32, 32);
            using (var g = Graphics.FromImage(bmp))
            {
                g.SmoothingMode = SmoothingMode.AntiAlias;
                using (var b = new SolidBrush(c)) g.FillEllipse(b, 1, 1, 30, 30);
                using (var f = new Font("Segoe UI", 16, FontStyle.Bold))
                using (var sf = new StringFormat())
                {
                    sf.Alignment = StringAlignment.Center;
                    sf.LineAlignment = StringAlignment.Center;
                    g.DrawString("λ", f, Brushes.White, new RectangleF(0, -1, 32, 32), sf);
                }
            }
            return Icon.FromHandle(bmp.GetHicon());
        }

        static string HttpGet(string url, int timeoutMs)
        {
            var req = (HttpWebRequest)WebRequest.Create(url);
            req.Timeout = timeoutMs;
            req.ReadWriteTimeout = timeoutMs;
            using (var resp = (HttpWebResponse)req.GetResponse())
            using (var sr = new StreamReader(resp.GetResponseStream()))
                return sr.ReadToEnd();
        }

        void RefreshState()
        {
            if (closing || Interlocked.CompareExchange(ref refreshPending, 1, 0) != 0) return;
            ThreadPool.QueueUserWorkItem(delegate
            {
                try
                {
                    var state = ReadState();
                    if (closing) { Interlocked.Exchange(ref refreshPending, 0); return; }
                    dispatcher.BeginInvoke((MethodInvoker)delegate
                    {
                        try { if (!closing) ApplyState(state); }
                        finally { Interlocked.Exchange(ref refreshPending, 0); }
                    });
                }
                catch (Exception ex)
                {
                    Debug.WriteLine(ex);
                    Interlocked.Exchange(ref refreshPending, 0);
                }
            });
        }

        TrayState ReadState()
        {
            var state = new TrayState();
            state.comfyCanStop = false;
            if (comfyUrl != null)
            {
                try
                {
                    var reply = new JavaScriptSerializer().Deserialize<Dictionary<string, object>>(HttpGet(comfyUrl + "/llama_swap/status", 500));
                    state.comfyCanStop = reply.ContainsKey("stop_supported") && Convert.ToBoolean(reply["stop_supported"]);
                }
                catch { }
            }
            state.coordinationAvailable = false;
            if (coordinationInstalled)
            {
                try
                {
                    var reply = new JavaScriptSerializer().Deserialize<Dictionary<string, object>>(HttpGet(ApiUrl + "/coordination/status", 1500));
                    state.coordinationEnabled = Convert.ToBoolean(reply["enabled"]);
                    state.coordinationPhase = Convert.ToString(reply["phase"]);
                    if (reply["error"] != null) state.coordinationPhase = Convert.ToString(reply["error"]);
                    state.coordinationAvailable = true;
                }
                catch { }
            }
            try
            {
                var json = HttpGet(ApiUrl + "/running", 1500);
                var root = new JavaScriptSerializer().Deserialize<Dictionary<string, object>>(json);
                var list = new List<string>();
                var arr = root.ContainsKey("running") ? root["running"] as ArrayList : null;
                if (arr != null)
                    foreach (object o in arr)
                    {
                        var d = o as Dictionary<string, object>;
                        if (d != null && d.ContainsKey("model")) list.Add(Convert.ToString(d["model"]));
                    }
                state.up = true;
                state.models = list;
            }
            catch
            {
                state.up = false;
                state.models = new List<string>();
            }

            if (!closing) state.autostartEnabled = IsAutostartEnabled();
            return state;
        }

        void ApplyState(TrayState state)
        {
            up = state.up;
            models = state.models;
            comfyCanStop = state.comfyCanStop;
            coordinationAvailable = state.coordinationAvailable;
            coordinationEnabled = state.coordinationEnabled;
            coordinationPhase = state.coordinationPhase;
            autostartEnabled = state.autostartEnabled;
            statusKnown = true;
            if (!up) { icon.Icon = icoOff; SetText("llama-swap: stopped"); }
            else if (models.Count > 0) { icon.Icon = icoLoaded; SetText("llama-swap: " + string.Join(", ", models.ToArray())); }
            else { icon.Icon = icoIdle; SetText("llama-swap: ready (VRAM free)"); }
            if (coordinationAvailable && coordinationEnabled) SetText("llama-swap: " + coordinationPhase);
            if (coordinationAvailable && coordinationPhase == "ComfyUI running") icon.Icon = icoLoaded;
            ApplyMenuState();
        }

        void SetText(string t)
        {
            if (t.Length > 63) t = t.Substring(0, 60) + "...";
            icon.Text = t;
        }

        void ApplyMenuState()
        {
            if (up)
            {
                miStatus.Text = "running: " + (models.Count > 0 ? string.Join(", ", models.ToArray()) : "idle, VRAM free");
                miStop.Enabled = true;
                miUnload.Enabled = models.Count > 0;
                miStart.Enabled = false;
            }
            else
            {
                miStatus.Text = "stopped";
                miStop.Enabled = false;
                miUnload.Enabled = false;
                miStart.Enabled = true;
            }
            miAuto.Text = autostartEnabled.GetValueOrDefault()
                ? "Disable autostart (persistent)"
                : "Re-enable autostart (currently OFF)";
            miCoord.Text = coordinationInstalled ? "Coordinate with ComfyUI" : "ComfyUI coordination (not installed)";
            miAuto.Enabled = autostartEnabled.HasValue;
            if (!autostartEnabled.HasValue) miAuto.Text = "Autostart: checking...";
            miComfyStop.Enabled = comfyCanStop;
            miCoord.Enabled = up && coordinationAvailable;
            miCoord.Checked = coordinationAvailable && coordinationEnabled;
            if (coordinationInstalled)
            {
                miStop.Text = "Stop LLM service (ComfyUI stays running)";
                miUnload.Text = "Unload LLM models";
                if (models.Count == 0 && up) miStatus.Text = "running: no LLMs loaded";
            }
            if (coordinationAvailable && coordinationEnabled) miStatus.Text = coordinationPhase;
            if (!statusKnown)
            {
                miStatus.Text = "Checking status...";
                miStop.Enabled = miStart.Enabled = miUnload.Enabled = false;
            }
            if (configurationError)
            {
                miStatus.Text = "Invalid coordinator.json; correct it and restart the tray";
                miStop.Enabled = miStart.Enabled = miUnload.Enabled = miAuto.Enabled = miCoord.Enabled = miComfyStop.Enabled = false;
            }
        }

        void LoadComfyControl()
        {
            string root = AppDomain.CurrentDomain.BaseDirectory;
            if (!File.Exists(Path.Combine(root, "runtime", "coordination-installed.json"))) return;
            try
            {
                var cfg = new JavaScriptSerializer().Deserialize<Dictionary<string, object>>(File.ReadAllText(Path.Combine(root, "coordinator.json")));
                var uri = new Uri(Convert.ToString(cfg["comfy_url"]));
                IPAddress address;
                bool local = uri.Host.Equals("localhost", StringComparison.OrdinalIgnoreCase)
                    || (IPAddress.TryParse(uri.Host, out address) && IPAddress.IsLoopback(address));
                if (!local || uri.Scheme != "http" || uri.UserInfo.Length != 0) return;
                if (cfg.ContainsKey("comfy_stop_command"))
                {
                    var command = cfg["comfy_stop_command"] as ArrayList;
                    if (command == null) throw new InvalidDataException("comfy_stop_command must be an argv array");
                    var args = new List<string>();
                    foreach (object arg in command) args.Add(Convert.ToString(arg));
                    comfyStopCommand = args.ToArray();
                }
                comfyTokenPath = Path.GetFullPath(Path.Combine(root, Convert.ToString(cfg["control_token_file"])));
                comfyUrl = uri.AbsoluteUri.TrimEnd('/');
            }
            catch { comfyUrl = null; }
        }

        // Quote argv for CreateProcess; never interpret settings through a shell.
        static string QuoteArgument(string value)
        {
            var quoted = new System.Text.StringBuilder("\"");
            int slashes = 0;
            foreach (char c in value)
            {
                if (c == '\\') { slashes++; continue; }
                quoted.Append('\\', c == '"' ? slashes * 2 + 1 : slashes);
                quoted.Append(c);
                slashes = 0;
            }
            quoted.Append('\\', slashes * 2);
            return quoted.Append('"').ToString();
        }

        void StopComfyLauncher()
        {
            if (comfyStopCommand.Length == 0) return;
            var args = new List<string>();
            for (int i = 1; i < comfyStopCommand.Length; i++) args.Add(QuoteArgument(comfyStopCommand[i]));
            var start = new ProcessStartInfo(comfyStopCommand[0], string.Join(" ", args.ToArray()));
            start.UseShellExecute = false;
            start.CreateNoWindow = true;
            using (var process = Process.Start(start))
            {
                if (!process.WaitForExit(15000) || process.ExitCode != 0)
                    throw new InvalidOperationException("ComfyUI is stopping, but its launcher stop command failed.");
            }
        }

        void OnStopComfy(object sender, EventArgs args)
        {
            miComfyStop.Enabled = false;
            ThreadPool.QueueUserWorkItem(delegate { StopComfy(false); });
        }

        void StopComfy(bool force)
        {
            try
            {
                var req = (HttpWebRequest)WebRequest.Create(comfyUrl + "/llama_swap/stop");
                req.Method = "POST";
                req.ContentType = "application/json";
                req.Timeout = req.ReadWriteTimeout = 10000;
                req.Headers["X-Coordination-Token"] = File.ReadAllText(comfyTokenPath).Trim();
                byte[] body = System.Text.Encoding.UTF8.GetBytes("{\"force\":" + (force ? "true" : "false") + "}");
                req.ContentLength = body.Length;
                using (var stream = req.GetRequestStream()) stream.Write(body, 0, body.Length);
                HttpWebResponse response;
                try { response = (HttpWebResponse)req.GetResponse(); }
                catch (WebException ex)
                {
                    response = ex.Response as HttpWebResponse;
                    if (response == null) throw;
                }
                using (response)
                using (var reader = new StreamReader(response.GetResponseStream()))
                {
                    string content = reader.ReadToEnd();
                    if (response.StatusCode == HttpStatusCode.Conflict && !force)
                    {
                        var state = new JavaScriptSerializer().Deserialize<Dictionary<string, object>>(content);
                        string warning = "ComfyUI: " + state["running"] + " running, " + state["pending"] + " queued.\n\nStop now and cancel these jobs? A local prompt/workflow snapshot will be saved; progress cannot be resumed automatically.";
                        menu.BeginInvoke((MethodInvoker)delegate
                        {
                            if (MessageBox.Show(warning, "Stop ComfyUI", MessageBoxButtons.YesNo,
                                MessageBoxIcon.Warning, MessageBoxDefaultButton.Button2) == DialogResult.Yes)
                                ThreadPool.QueueUserWorkItem(delegate { StopComfy(true); });
                        });
                        return;
                    }
                    if (response.StatusCode != HttpStatusCode.OK) throw new InvalidOperationException(content);
                    var result = new JavaScriptSerializer().Deserialize<Dictionary<string, object>>(content);
                    string backup = result.ContainsKey("backup") ? Convert.ToString(result["backup"]) : "";
                    StopComfyLauncher();
                    Balloon("ComfyUI is stopping." + (backup.Length > 0 ? " Queue snapshot: " + backup : ""));
                }
            }
            catch (Exception ex) { Balloon("Stop ComfyUI: " + ex.Message); }
        }

        void OnToggleCoordination(object s, EventArgs e)
        {
            bool enable = !coordinationEnabled;
            ThreadPool.QueueUserWorkItem(delegate
            {
                try
                {
                    var req = (HttpWebRequest)WebRequest.Create(ApiUrl + "/coordination/settings");
                    req.Method = "POST";
                    req.ContentType = "application/json";
                    req.Timeout = 5000;
                    byte[] body = System.Text.Encoding.UTF8.GetBytes("{\"enabled\":" + (enable ? "true" : "false") + "}");
                    req.ContentLength = body.Length;
                    using (var stream = req.GetRequestStream()) stream.Write(body, 0, body.Length);
                    using (var response = req.GetResponse()) { }
                    Balloon(enable ? "ComfyUI coordination enabled." : "Coordination disabled after any active handover finishes.");
                }
                catch (WebException ex)
                {
                    string message = ex.Message;
                    if (ex.Response != null)
                        using (var reader = new StreamReader(ex.Response.GetResponseStream())) message = reader.ReadToEnd();
                    Balloon(message);
                }
                catch (Exception ex) { Balloon(ex.Message); }
            });
        }

        void Balloon(string msg)
        {
            if (closing) return;
            if (dispatcher.InvokeRequired)
            {
                try { dispatcher.BeginInvoke((MethodInvoker)delegate { Balloon(msg); }); }
                catch (InvalidOperationException) { }
                return;
            }
            icon.ShowBalloonTip(2000, "llama-swap", msg, ToolTipIcon.Info);
        }

        // try schtasks without admin rights first (permissions come from setup\03),
        // only fall back to a UAC prompt when that fails
        static bool RunSchtasks(string args)
        {
            try
            {
                var psi = new ProcessStartInfo("schtasks", args);
                psi.UseShellExecute = false;
                psi.CreateNoWindow = true;
                using (var p = Process.Start(psi))
                {
                    p.WaitForExit(15000);
                    if (p.HasExited && p.ExitCode == 0) return true;
                }
            }
            catch { }
            try
            {
                var psi = new ProcessStartInfo("schtasks", args);
                psi.UseShellExecute = true;
                psi.Verb = "runas"; // UAC fallback
                psi.WindowStyle = ProcessWindowStyle.Hidden;
                using (var p = Process.Start(psi)) { p.WaitForExit(30000); }
                return true;
            }
            catch { return false; } // UAC cancelled
        }

        static bool RunTask(string verb)
        {
            return RunSchtasks(verb + " /TN \"" + ActiveTaskName + "\"");
        }

        static bool? IsAutostartEnabled()
        {
            try
            {
                var psi = new ProcessStartInfo("schtasks", "/Query /TN \"" + ActiveTaskName + "\" /XML");
                psi.UseShellExecute = false;
                psi.CreateNoWindow = true;
                psi.RedirectStandardOutput = true;
                using (var p = Process.Start(psi))
                {
                    var output = p.StandardOutput.ReadToEndAsync();
                    if (!p.WaitForExit(2000)) { p.Kill(); return null; }
                    if (p.ExitCode != 0 || !output.Wait(1000)) return null;
                    return output.Result.IndexOf("<Enabled>false</Enabled>", StringComparison.OrdinalIgnoreCase) < 0;
                }
            }
            catch { return null; }
        }

        void OnToggleAutostart(object s, EventArgs e)
        {
            if (!autostartEnabled.HasValue) { RefreshState(); return; }
            bool enabled = autostartEnabled.Value;
            string args = "/Change /TN \"" + ActiveTaskName + "\"" + (enabled ? " /DISABLE" : " /ENABLE");
            string msg = enabled
                ? "Autostart disabled - llama-swap stays off even after a reboot."
                : "Autostart enabled - llama-swap starts automatically at boot again.";
            ThreadPool.QueueUserWorkItem(delegate
            {
                if (RunSchtasks(args)) Balloon(msg);
                else Balloon("Change failed or was cancelled.");
                RefreshState();
            });
        }

        void OnUnload(object s, EventArgs e)
        {
            Balloon("Unloading models...");
            ThreadPool.QueueUserWorkItem(delegate
            {
                try { HttpGet(ApiUrl + "/unload", 120000); } catch { }
            });
        }

        void OnStop(object s, EventArgs e)
        {
            Balloon("Unloading models and stopping...");
            ThreadPool.QueueUserWorkItem(delegate
            {
                try { HttpGet(ApiUrl + "/unload", 30000); } catch { }
                RunTask("/End");
                RefreshState();
            });
        }

        void OnStart(object s, EventArgs e)
        {
            Balloon("Starting llama-swap...");
            ThreadPool.QueueUserWorkItem(delegate { RunTask("/Run"); });
        }
    }
}
