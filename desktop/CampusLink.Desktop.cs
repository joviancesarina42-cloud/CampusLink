using System;
using System.Collections.Generic;
using System.Drawing;
using System.Drawing.Drawing2D;
using System.IO;
using System.Net;
using System.Net.Http;
using System.Runtime.InteropServices;
using System.Threading.Tasks;
using System.Web.Script.Serialization;
using System.Windows.Forms;
using Microsoft.Web.WebView2.Core;
using Microsoft.Web.WebView2.WinForms;

namespace CampusLinkDesktop
{
    internal static class Program
    {
        [STAThread]
        private static void Main(string[] args)
        {
            Application.EnableVisualStyles();
            Application.SetCompatibleTextRenderingDefault(false);
            int port;
            if (args.Length != 1 || !Int32.TryParse(args[0], out port)) return;
            Application.Run(new MainWindow(port));
        }
    }

    internal sealed class MainWindow : Form
    {
        private readonly int port;
        private readonly string root;
        private readonly HttpClient client;
        private readonly JavaScriptSerializer json = new JavaScriptSerializer();
        private readonly WebView2 view;
        private readonly NotifyIcon tray;
        private readonly ToolStripMenuItem exitItem;
        private readonly Label loading;
        private readonly uint showMessage;
        private readonly uint exitMessage;
        // Test automation invokes the same method as the actual tray-menu callback.
        private readonly uint trayExitMessage;
        private bool allowExit;
        private bool exitPending;
        private bool hideHintShown;
        private bool background;
        private bool suspending;

        [DllImport("user32.dll", CharSet = CharSet.Unicode)]
        private static extern uint RegisterWindowMessage(string message);
        [DllImport("user32.dll")]
        private static extern bool SetForegroundWindow(IntPtr window);
        [DllImport("user32.dll")]
        private static extern bool DestroyIcon(IntPtr icon);

        internal MainWindow(int port)
        {
            this.port = port;
            root = Directory.GetParent(AppDomain.CurrentDomain.BaseDirectory.TrimEnd(Path.DirectorySeparatorChar)).FullName;
            Text = "CampusLink · IPv6 链路工具";
            StartPosition = FormStartPosition.CenterScreen;
            Size = new Size(1380, 940);
            MinimumSize = new Size(900, 640);
            BackColor = Color.FromArgb(243, 246, 248);
            AutoScaleMode = AutoScaleMode.Dpi;
            Icon = MakeIcon();

            client = new HttpClient(new HttpClientHandler { UseProxy = false });
            client.Timeout = TimeSpan.FromSeconds(45);
            client.BaseAddress = new Uri("http://127.0.0.1:" + port + "/");
            loading = new Label { Dock = DockStyle.Fill, TextAlign = ContentAlignment.MiddleCenter,
                Text = "正在加载 CampusLink…", ForeColor = Color.FromArgb(0, 139, 112),
                Font = new Font("Microsoft YaHei UI", 12) };
            Controls.Add(loading);
            view = new WebView2 { Dock = DockStyle.Fill, Visible = false,
                DefaultBackgroundColor = BackColor, AllowExternalDrop = false };
            Controls.Add(view);

            var menu = new ContextMenuStrip();
            menu.Items.Add("打开主界面", null, delegate { RestoreWindow(); });
            menu.Items.Add(new ToolStripSeparator());
            exitItem = new ToolStripMenuItem("退出并恢复网络");
            exitItem.Click += async delegate { await RequestExitAsync(); };
            menu.Items.Add(exitItem);
            tray = new NotifyIcon { Icon = Icon, Text = "CampusLink · IPv6 链路工具",
                ContextMenuStrip = menu, Visible = true };
            tray.DoubleClick += delegate { RestoreWindow(); };
            showMessage = RegisterWindowMessage("CampusLink.Desktop.Show.v1");
            exitMessage = RegisterWindowMessage("CampusLink.Desktop.Exit.v1");
            trayExitMessage = RegisterWindowMessage("CampusLink.Desktop.TrayExit.v1");
            FormClosing += OnClosing;
            Resize += delegate { UpdateBackground(); };
            Shown += async delegate { await InitializeViewAsync(); };
            WriteStatus("visible", null);
        }

        private static Icon MakeIcon()
        {
            using (var bitmap = new Bitmap(64, 64))
            using (var g = Graphics.FromImage(bitmap))
            using (var background = new SolidBrush(Color.FromArgb(44, 200, 159)))
            using (var ink = new SolidBrush(Color.FromArgb(19, 44, 58)))
            using (var font = new Font("Segoe UI", 41, FontStyle.Bold, GraphicsUnit.Pixel))
            {
                g.SmoothingMode = SmoothingMode.AntiAlias;
                g.Clear(Color.Transparent);
                g.FillEllipse(background, 2, 2, 60, 60);
                g.DrawString("6", font, ink, new RectangleF(0, 0, 64, 64),
                    new StringFormat { Alignment = StringAlignment.Center, LineAlignment = StringAlignment.Center });
                IntPtr handle = bitmap.GetHicon();
                try { return (Icon)Icon.FromHandle(handle).Clone(); }
                finally { DestroyIcon(handle); }
            }
        }

        private async Task InitializeViewAsync()
        {
            try
            {
                var options = new CoreWebView2EnvironmentOptions();
                // Local proxy bypass keeps the desktop UI accessible during link changes.
                options.AdditionalBrowserArguments = "--no-proxy-server";
                var environment = await CoreWebView2Environment.CreateAsync(null,
                    Path.Combine(root, "data", "webview2"), options);
                await view.EnsureCoreWebView2Async(environment);
                view.CoreWebView2.Settings.AreDefaultContextMenusEnabled = false;
                view.CoreWebView2.Settings.AreDevToolsEnabled = false;
                view.CoreWebView2.Settings.IsStatusBarEnabled = false;
                view.CoreWebView2.Settings.AreBrowserAcceleratorKeysEnabled = false;
                view.CoreWebView2.NewWindowRequested += delegate(object sender, CoreWebView2NewWindowRequestedEventArgs e)
                { e.Handled = true; };
                view.CoreWebView2.DownloadStarting += OnDownloadStarting;
                view.CoreWebView2.NavigationCompleted += delegate(object sender, CoreWebView2NavigationCompletedEventArgs e)
                {
                    if (e.IsSuccess)
                    {
                        view.Visible = true;
                        loading.Visible = false;
                        WriteStatus(Visible ? "visible" : "hidden", null);
                        UpdateBackground();
                    }
                    else
                    {
                        loading.Text = "界面尚未连接到本机服务。可通过托盘退出或重新打开工具。";
                        WriteStatus("error", e.WebErrorStatus.ToString());
                    }
                };
                view.CoreWebView2.Navigate(client.BaseAddress.ToString());
            }
            catch (Exception error)
            {
                loading.Text = "无法加载桌面界面：" + error.Message + "\n请检查 Microsoft Edge WebView2 运行环境。";
                WriteStatus("error", error.ToString());
            }
        }

        private void OnDownloadStarting(object sender, CoreWebView2DownloadStartingEventArgs e)
        {
            var deferral = e.GetDeferral();
            BeginInvoke(new Action(delegate
            {
                try
                {
                    using (var dialog = new SaveFileDialog { Title = "导出流量增量记录", Filter = "CSV 文件 (*.csv)|*.csv",
                        FileName = "CampusLink-流量明细-" + DateTime.Now.ToString("yyyyMMdd") + ".csv" })
                    {
                        e.Handled = true;
                        if (dialog.ShowDialog(this) == DialogResult.OK) e.ResultFilePath = dialog.FileName;
                        else e.Cancel = true;
                    }
                }
                finally { deferral.Complete(); }
            }));
        }

        private void RestoreWindow()
        {
            Show();
            if (WindowState == FormWindowState.Minimized) WindowState = FormWindowState.Normal;
            Activate();
            SetForegroundWindow(Handle);
            UpdateBackground();
            WriteStatus("visible", null);
        }

        private void OnClosing(object sender, FormClosingEventArgs e)
        {
            if (allowExit) return;
            e.Cancel = true;
            Hide();
            UpdateBackground();
            WriteStatus("hidden", null);
            if (!hideHintShown)
            {
                hideHintShown = true;
                tray.ShowBalloonTip(3000, "CampusLink 已在后台运行",
                    "关闭窗口不会停止链路。右键托盘图标，选择“退出并恢复网络”可结束工具。", ToolTipIcon.Info);
            }
        }

        private async void UpdateBackground()
        {
            background = !Visible || WindowState == FormWindowState.Minimized;
            if (view.CoreWebView2 == null || allowExit) return;
            try
            {
                view.CoreWebView2.PostWebMessageAsJson("{\"type\":\"visibility\",\"hidden\":" + (background ? "true" : "false") + "}");
                if (!background)
                {
                    view.CoreWebView2.Resume();
                    view.Visible = true;
                    return;
                }
                view.Visible = false;
                if (suspending) return;
                suspending = true;
                try { await view.CoreWebView2.TrySuspendAsync(); }
                finally
                {
                    suspending = false;
                    // A tray click can arrive while suspension is in flight.
                    if (!background && !allowExit)
                    {
                        view.CoreWebView2.Resume();
                        view.Visible = true;
                    }
                }
            }
            catch (Exception) { /* Visibility polling remains the fallback. */ }
        }

        private async Task<Dictionary<string, object>> ReadJsonAsync(string path)
        {
            string body = await client.GetStringAsync(path);
            return json.Deserialize<Dictionary<string, object>>(body);
        }

        private async Task RequestExitAsync()
        {
            if (exitPending) return;
            exitPending = true;
            exitItem.Enabled = false;
            exitItem.Text = "正在恢复网络…";
            tray.Text = "CampusLink · 正在恢复网络";
            WriteStatus("restoring", null);
            try
            {
                var session = await ReadJsonAsync("api/token");
                bool accepted = false;
                for (int attempt = 0; attempt < 120; attempt++)
                {
                    using (var request = new HttpRequestMessage(HttpMethod.Post, "api/exit"))
                    {
                        request.Headers.Add("X-CampusLink", (string)session["token"]);
                        request.Content = new StringContent("{}", System.Text.Encoding.UTF8, "application/json");
                        using (var response = await client.SendAsync(request))
                        {
                            if (response.StatusCode == HttpStatusCode.Conflict)
                            {
                                await Task.Delay(1000);
                                continue;
                            }
                            if (!response.IsSuccessStatusCode)
                                throw new InvalidOperationException("退出请求失败：HTTP " + (int)response.StatusCode);
                            accepted = true;
                            break;
                        }
                    }
                }
                if (!accepted) throw new InvalidOperationException("上一项操作尚未完成，暂时无法恢复网络。");
                for (int attempt = 0; attempt < 180; attempt++)
                {
                    await Task.Delay(500);
                    Dictionary<string, object> status;
                    try { status = await ReadJsonAsync("api/state?view=status"); }
                    catch (HttpRequestException)
                    {
                        if (File.Exists(Path.Combine(root, "data", "recovery.json")))
                            throw new InvalidOperationException("后台暂时无法连接，恢复记录仍然存在，请重新启动工具恢复上次现场。");
                        FinishExit();
                        return;
                    }
                    var state = (Dictionary<string, object>)status["state"];
                    if (state.ContainsKey("job"))
                    {
                        var job = (Dictionary<string, object>)state["job"];
                        if (!(bool)job["running"] && job.ContainsKey("error"))
                            throw new InvalidOperationException((string)job["error"]);
                    }
                }
                throw new TimeoutException("网络恢复尚未确认完成，工具继续保留在托盘中。");
            }
            catch (Exception error)
            {
                exitPending = false;
                exitItem.Enabled = true;
                exitItem.Text = "退出并恢复网络";
                tray.Text = "CampusLink · 恢复尚未完成";
                RestoreWindow();
                WriteStatus("restore-error", error.Message);
                MessageBox.Show(this, "尚未完成网络恢复，工具将继续保留。\n\n" + error.Message,
                    "CampusLink", MessageBoxButtons.OK, MessageBoxIcon.Warning);
            }
        }

        private void FinishExit()
        {
            if (allowExit) return;
            allowExit = true;
            WriteStatus("exited", null);
            tray.Visible = false;
            Close();
        }

        protected override void WndProc(ref Message message)
        {
            if (message.Msg == showMessage) { RestoreWindow(); return; }
            if (message.Msg == exitMessage) { FinishExit(); return; }
            if (message.Msg == trayExitMessage)
            {
                BeginInvoke(new Action(delegate { exitItem.PerformClick(); }));
                return;
            }
            base.WndProc(ref message);
        }

        private void WriteStatus(string state, string error)
        {
            try
            {
                var value = new Dictionary<string, object> {
                    { "pid", System.Diagnostics.Process.GetCurrentProcess().Id },
                    { "hwnd", IsHandleCreated ? Handle.ToInt64() : 0 },
                    { "state", state }, { "port", port }, { "error", error },
                    { "time", DateTime.Now.ToString("O") }
                };
                Directory.CreateDirectory(Path.Combine(root, "data"));
                File.WriteAllText(Path.Combine(root, "data", "desktop.json"), json.Serialize(value), System.Text.Encoding.UTF8);
            }
            catch { /* Diagnostics must not affect window or network lifecycle. */ }
        }

        protected override void Dispose(bool disposing)
        {
            if (disposing)
            {
                tray.Visible = false;
                tray.Dispose();
                client.Dispose();
                view.Dispose();
            }
            base.Dispose(disposing);
        }
    }
}
