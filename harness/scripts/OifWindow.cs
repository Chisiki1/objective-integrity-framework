using System;
using System.Collections.Generic;
using System.Diagnostics;
using System.Drawing;
using System.IO;
using System.Runtime.InteropServices;
using System.Threading.Tasks;
using System.Web.Script.Serialization;
using System.Windows.Forms;
using Microsoft.Web.WebView2.Core;
using Microsoft.Web.WebView2.WinForms;

// The web application remains the single UI implementation. No tool execution
// or general host-object bridge is exposed to documents rendered here.
internal sealed class OifWindow : Form {
    internal WebView2 Browser { get; private set; }
    internal Uri Origin { get; private set; }
    readonly string profile;
    readonly Func<Task<string>> service;
    readonly Panel notice = new Panel();
    readonly Label message = new Label();
    readonly Button retry = new Button();
    bool initializing, ready, closing;
    static bool loaderConfigured;
    string lastFragment = "";
    readonly JavaScriptSerializer json = new JavaScriptSerializer();
    [DllImport("dwmapi.dll")] static extern int DwmSetWindowAttribute(IntPtr hwnd, int attribute, ref int value, int size);

    internal OifWindow(string profilePath, Func<Task<string>> serviceFactory = null) {
        profile = profilePath; service = serviceFactory ?? (() => Task.Run(() => OifLauncher.StartService()));
        Text = "OIF"; Icon = Icon.ExtractAssociatedIcon(Application.ExecutablePath);
        AutoScaleMode = AutoScaleMode.Dpi; AutoScaleDimensions = new SizeF(96, 96);
        ClientSize = new Size(1200, 820); MinimumSize = new Size(640, 480);
        StartPosition = FormStartPosition.CenterScreen;
        BackColor = Color.FromArgb(25, 25, 24); ForeColor = Color.Gainsboro;
        Font = new Font("Yu Gothic UI", 11); KeyPreview = true;
        notice.Dock = DockStyle.Fill; notice.BackColor = BackColor;
        message.Text = "Opening OIF…"; message.AutoSize = false;
        message.Dock = DockStyle.Fill; message.TextAlign = ContentAlignment.MiddleCenter; message.Padding = new Padding(32);
        retry.Text = "Reload window"; retry.Dock = DockStyle.Bottom; retry.Height = 48; retry.Visible = false;
        retry.FlatStyle = FlatStyle.Flat; retry.Click += async (s, e) => await InitializeAsync();
        notice.Controls.Add(message); notice.Controls.Add(retry); Controls.Add(notice);
        RestoreWindow();
        Shown += async (s, e) => {
            try { OifShell.SetWindowIdentity(Handle, OifLauncher.AppId, Application.ExecutablePath); await InitializeAsync(); }
            catch (Exception error) { OifLauncher.Record(error.ToString()); if (!closing) ShowNotice(error.Message, true); }
        };
        FormClosing += (s, e) => { closing = true; SaveWindow(); };
        FormClosed += (s, e) => { if (Browser != null) Browser.Dispose(); };
    }

    internal void ActivateWindow() {
        if (WindowState == FormWindowState.Minimized) WindowState = FormWindowState.Normal;
        Show(); Activate(); BringToFront();
    }

    void ShowNotice(string text, bool canRetry) {
        message.Text = text; retry.Visible = canRetry; notice.Visible = true; notice.BringToFront();
    }

    internal async Task InitializeAsync() {
        if (initializing || closing) return;
        initializing = true; ready = false;
        ShowNotice("Opening OIF…", false);
        try {
            if (!loaderConfigured) {
                CoreWebView2Environment.SetLoaderDllFolderPath(Path.Combine(AppDomain.CurrentDomain.BaseDirectory, "desktop"));
                loaderConfigured = true;
            }
            string version;
            try { version = CoreWebView2Environment.GetAvailableBrowserVersionString(); }
            catch (WebView2RuntimeNotFoundException) { throw new InvalidOperationException("Microsoft Edge WebView2 Runtime is required. Install the official Microsoft runtime, then open OIF again."); }
            var url = await service();
            if (closing) return;
            Origin = new Uri(url);
            if (Origin.Scheme != "http" || Origin.Host != "127.0.0.1" || Origin.AbsolutePath != "/")
                throw new InvalidOperationException("The local OIF address could not be verified.");
            if (Browser != null) { Controls.Remove(Browser); Browser.Dispose(); }
            Browser = new WebView2 { Dock = DockStyle.Fill, DefaultBackgroundColor = BackColor };
            Controls.Add(Browser); notice.BringToFront();
            Directory.CreateDirectory(profile);
            var environment = await CoreWebView2Environment.CreateAsync(null, Path.Combine(profile, "WebView2"));
            if (closing) return;
            await Browser.EnsureCoreWebView2Async(environment);
            if (closing) return;
            var core = Browser.CoreWebView2;
            var blockedNavigations = new HashSet<ulong>();
            core.Settings.AreHostObjectsAllowed = false;
            core.Settings.IsStatusBarEnabled = false;
            core.Settings.IsBuiltInErrorPageEnabled = false;
            // Native browser controls retain normal file-picker, paste, select,
            // accessibility and download behavior. No page-wide script rewrite.
            core.NavigationStarting += (s, e) => {
                Uri destination;
                if (!Uri.TryCreate(e.Uri, UriKind.Absolute, out destination) || !IsLocal(destination)) {
                    blockedNavigations.Add(e.NavigationId);
                    e.Cancel = true;
                    if (e.IsUserInitiated) OpenExternal(e.Uri);
                }
            };
            core.NewWindowRequested += (s, e) => {
                e.Handled = true;
                Uri destination;
                if (Uri.TryCreate(e.Uri, UriKind.Absolute, out destination) && IsLocal(destination)) core.Navigate(destination.AbsoluteUri);
                else if (e.IsUserInitiated) OpenExternal(e.Uri);
            };
            core.PermissionRequested += (s, e) => {
                Uri requesting;
                if (!Uri.TryCreate(e.Uri, UriKind.Absolute, out requesting) || !IsLocal(requesting)) e.State = CoreWebView2PermissionState.Deny;
                // Clipboard access is restricted to the trusted local origin and
                // a real user gesture; other permissions keep the runtime prompt.
                else if (e.PermissionKind == CoreWebView2PermissionKind.ClipboardRead && e.IsUserInitiated)
                    e.State = CoreWebView2PermissionState.Allow;
            };
            core.SourceChanged += (s, e) => {
                Uri current;
                if (Uri.TryCreate(core.Source, UriKind.Absolute, out current) && IsLocal(current) && current.AbsolutePath == "/") {
                    lastFragment = SafeFragment(current.Fragment); SaveWindow();
                }
            };
            core.WebMessageReceived += (s, e) => {
                Uri from;
                if (!Uri.TryCreate(e.Source, UriKind.Absolute, out from) || !IsLocal(from)) return;
                string value;
                try { value = e.TryGetWebMessageAsString(); } catch (ArgumentException) { return; }
                if (value == "oif-update-close") { Close(); return; }
                if (value != "oif-theme:dark" && value != "oif-theme:light") return;
                int dark = value.EndsWith(":dark") ? 1 : 0;
                DwmSetWindowAttribute(Handle, 20, ref dark, sizeof(int));
            };
            core.ProcessFailed += (s, e) => {
                if (e.ProcessFailedKind == CoreWebView2ProcessFailedKind.BrowserProcessExited ||
                    e.ProcessFailedKind == CoreWebView2ProcessFailedKind.RenderProcessExited ||
                    e.ProcessFailedKind == CoreWebView2ProcessFailedKind.RenderProcessUnresponsive) {
                    ready = false;
                    OifLauncher.Record("Desktop renderer: " + e.ProcessFailedKind);
                    ShowNotice("The display stopped. Task records are saved. You can reload this window.", true);
                }
            };
            core.NavigationCompleted += async (s, e) => {
                if (closing || Browser.IsDisposed) return;
                if (blockedNavigations.Remove(e.NavigationId) && e.WebErrorStatus == CoreWebView2WebErrorStatus.OperationCanceled) return;
                if (!e.IsSuccess) {
                    ShowNotice("Could not connect to OIF. You can reload this window.", true);
                    OifLauncher.Record("Desktop navigation: " + e.WebErrorStatus); return;
                }
                ready = true; notice.Visible = false;
                try {
                    await core.ExecuteScriptAsync("(()=>{const send=()=>window.chrome.webview.postMessage('oif-theme:'+(document.documentElement.dataset.theme==='light'?'light':'dark'));send();new MutationObserver(send).observe(document.documentElement,{attributes:true,attributeFilter:['data-theme']});})()");
                } catch (Exception error) { OifLauncher.Record("Desktop theme: " + error.Message); }
                OifLauncher.Record("Desktop ready; WebView2 " + version);
            };
            core.Navigate(Origin.AbsoluteUri + lastFragment);
        } catch (Exception error) {
            OifLauncher.Record(error.ToString());
            if (!closing) ShowNotice(error.Message, true);
        } finally { initializing = false; }
    }

    internal bool IsLocal(Uri uri) {
        return Origin != null && uri.Scheme == Origin.Scheme && uri.Host == Origin.Host && uri.Port == Origin.Port && String.IsNullOrEmpty(uri.UserInfo);
    }
    internal static bool IsExternalWeb(Uri uri) { return (uri.Scheme == "https" || uri.Scheme == "http") && String.IsNullOrEmpty(uri.UserInfo); }
    void OpenExternal(string address) {
        Uri uri;
        if (!Uri.TryCreate(address, UriKind.Absolute, out uri) || !IsExternalWeb(uri)) return;
        try { Process.Start(new ProcessStartInfo(uri.AbsoluteUri) { UseShellExecute = true }); }
        catch (Exception error) { OifLauncher.Record("External link: " + error.Message); }
    }
    static string SafeFragment(string fragment) {
        if (fragment.StartsWith("#task=", StringComparison.Ordinal) && fragment.Length == 38) {
            Guid id; if (Guid.TryParseExact(fragment.Substring(6), "N", out id)) return fragment;
        }
        return "";
    }
    protected override bool ProcessCmdKey(ref Message msg, Keys keyData) {
        if (keyData == (Keys.Alt | Keys.Left) && ready && Browser.CoreWebView2.CanGoBack) { Browser.GoBack(); return true; }
        if (keyData == (Keys.Control | Keys.R) || keyData == Keys.F5) {
            if (ready && Browser != null) Browser.Reload();
            else { var ignored = InitializeAsync(); }
            return true;
        }
        return base.ProcessCmdKey(ref msg, keyData);
    }
    void RestoreWindow() {
        try {
            string path = Path.Combine(profile, "window.json"); if (!File.Exists(path)) return;
            var data = json.Deserialize<Dictionary<string, object>>(File.ReadAllText(path));
            var bounds = new Rectangle(Convert.ToInt32(data["x"]), Convert.ToInt32(data["y"]), Convert.ToInt32(data["width"]), Convert.ToInt32(data["height"]));
            foreach (var screen in Screen.AllScreens) {
                if (Rectangle.Intersect(screen.WorkingArea, bounds).Width >= 160 && Rectangle.Intersect(screen.WorkingArea, bounds).Height >= 100) {
                    bounds.Width = Math.Max(MinimumSize.Width, Math.Min(bounds.Width, screen.WorkingArea.Width));
                    bounds.Height = Math.Max(MinimumSize.Height, Math.Min(bounds.Height, screen.WorkingArea.Height));
                    bounds.X = Math.Max(screen.WorkingArea.Left, Math.Min(bounds.X, screen.WorkingArea.Right - bounds.Width));
                    bounds.Y = Math.Max(screen.WorkingArea.Top, Math.Min(bounds.Y, screen.WorkingArea.Bottom - bounds.Height));
                    StartPosition = FormStartPosition.Manual; Bounds = bounds; break;
                }
            }
            if (data.ContainsKey("maximized") && Convert.ToBoolean(data["maximized"])) WindowState = FormWindowState.Maximized;
            if (data.ContainsKey("fragment")) lastFragment = SafeFragment(Convert.ToString(data["fragment"]));
        } catch (Exception error) { OifLauncher.Record("Desktop window preference: " + error.Message); }
    }
    void SaveWindow() {
        try {
            Directory.CreateDirectory(profile);
            var bounds = WindowState == FormWindowState.Normal ? Bounds : RestoreBounds;
            var data = new { x = bounds.X, y = bounds.Y, width = bounds.Width, height = bounds.Height, maximized = WindowState == FormWindowState.Maximized, fragment = lastFragment };
            var path = Path.Combine(profile, "window.json"); var temporary = path + ".tmp";
            File.WriteAllText(temporary, json.Serialize(data), new System.Text.UTF8Encoding(false));
            if (File.Exists(path)) File.Replace(temporary, path, null); else File.Move(temporary, path);
        } catch (Exception error) { OifLauncher.Record("Desktop window save: " + error.Message); }
    }
}
