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
        // must match the port configured in setup\03-server-task.ps1
        const string Api = "http://localhost:9292";
        const string TaskName = "llama-swap\\server";

        NotifyIcon icon;
        Icon icoIdle, icoLoaded, icoOff;
        ToolStripMenuItem miStatus, miUnload, miStop, miStart, miAuto;
        System.Windows.Forms.Timer timer;
        bool up;
        List<string> models = new List<string>();

        public TrayContext()
        {
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
            var miExit = new ToolStripMenuItem("Quit tray", null, delegate { icon.Visible = false; ExitThread(); });

            var menu = new ContextMenuStrip();
            menu.Items.AddRange(new ToolStripItem[] {
                miStatus, new ToolStripSeparator(),
                miDash, miConfig, miUnload, miStop, miStart,
                new ToolStripSeparator(), miAuto,
                new ToolStripSeparator(), miExit });
            menu.Opening += delegate { ApplyMenuState(); };

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
            RefreshState();
        }

        void OpenDashboard()
        {
            try { Process.Start(Api + "/ui"); } catch { }
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
            try
            {
                var json = HttpGet(Api + "/running", 1500);
                var root = new JavaScriptSerializer().Deserialize<Dictionary<string, object>>(json);
                var list = new List<string>();
                var arr = root.ContainsKey("running") ? root["running"] as ArrayList : null;
                if (arr != null)
                    foreach (object o in arr)
                    {
                        var d = o as Dictionary<string, object>;
                        if (d != null && d.ContainsKey("model")) list.Add(Convert.ToString(d["model"]));
                    }
                up = true;
                models = list;
            }
            catch
            {
                up = false;
                models = new List<string>();
            }

            if (!up) { icon.Icon = icoOff; SetText("llama-swap: stopped"); }
            else if (models.Count > 0) { icon.Icon = icoLoaded; SetText("llama-swap: " + string.Join(", ", models.ToArray())); }
            else { icon.Icon = icoIdle; SetText("llama-swap: ready (VRAM free)"); }
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
            miAuto.Text = IsAutostartEnabled()
                ? "Disable autostart (persistent)"
                : "Re-enable autostart (currently OFF)";
        }

        void Balloon(string msg)
        {
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
            return RunSchtasks(verb + " /TN " + TaskName);
        }

        static bool IsAutostartEnabled()
        {
            try
            {
                var psi = new ProcessStartInfo("schtasks", "/Query /TN " + TaskName + " /XML");
                psi.UseShellExecute = false;
                psi.CreateNoWindow = true;
                psi.RedirectStandardOutput = true;
                using (var p = Process.Start(psi))
                {
                    string xml = p.StandardOutput.ReadToEnd();
                    p.WaitForExit(5000);
                    return xml.IndexOf("<Enabled>false</Enabled>", StringComparison.OrdinalIgnoreCase) < 0;
                }
            }
            catch { return true; }
        }

        void OnToggleAutostart(object s, EventArgs e)
        {
            bool enabled = IsAutostartEnabled();
            string args = "/Change /TN " + TaskName + (enabled ? " /DISABLE" : " /ENABLE");
            string msg = enabled
                ? "Autostart disabled - llama-swap stays off even after a reboot."
                : "Autostart enabled - llama-swap starts automatically at boot again.";
            ThreadPool.QueueUserWorkItem(delegate
            {
                if (RunSchtasks(args)) Balloon(msg);
                else Balloon("Change failed or was cancelled.");
            });
        }

        void OnUnload(object s, EventArgs e)
        {
            Balloon("Unloading models...");
            ThreadPool.QueueUserWorkItem(delegate
            {
                try { HttpGet(Api + "/unload", 120000); } catch { }
            });
        }

        void OnStop(object s, EventArgs e)
        {
            Balloon("Unloading models and stopping...");
            // unload synchronously first: it usually takes <5 s and lets llama-swap
            // stop managed process trees cleanly (via their cmdStop commands)
            try { HttpGet(Api + "/unload", 30000); } catch { }
            RunTask("/End");
            RefreshState();
        }

        void OnStart(object s, EventArgs e)
        {
            Balloon("Starting llama-swap...");
            ThreadPool.QueueUserWorkItem(delegate { RunTask("/Run"); });
        }
    }
}
