using System;
using System.Collections.Generic;
using System.Diagnostics;
using System.Drawing;
using System.IO;
using System.Net;
using System.Text;
using System.Threading;
using System.Threading.Tasks;
using System.Web.Script.Serialization;
using System.Windows.Forms;
using System.Runtime.InteropServices;

[assembly: System.Reflection.AssemblyTitle("OIF")]
[assembly: System.Reflection.AssemblyDescription("OIF desktop application")]
[assembly: System.Reflection.AssemblyVersion("0.3.0.0")]
[assembly: System.Reflection.AssemblyInformationalVersion("0.3.0-beta.5")]

internal static class OifLauncher {
    internal static readonly string Root = AppDomain.CurrentDomain.BaseDirectory;
    static readonly string Log = Path.Combine(Root, ".runtime", "desktop-launch.log");
    static readonly JavaScriptSerializer Json = new JavaScriptSerializer();
    static string Quote(string value) { return "\"" + value.Replace("\"", "") + "\""; }
    static readonly object LogLock = new object();
    internal static void Record(string text) {
        try { lock (LogLock) {
            Directory.CreateDirectory(Path.GetDirectoryName(Log));
            File.AppendAllText(Log, DateTime.Now.ToString("s") + " " + text + Environment.NewLine, new UTF8Encoding(false));
        } } catch (IOException) { } catch (UnauthorizedAccessException) { }
    }
    static Dictionary<string, object> ReadRecord() {
        return Json.Deserialize<Dictionary<string, object>>(File.ReadAllText(Path.Combine(Root, ".runtime", "server-process.json")));
    }
    internal static string StartService(int requiredPort = 0) {
        if (requiredPort < 0 || requiredPort > 65535) throw new ArgumentOutOfRangeException("requiredPort");
        Record("Desktop launch requested");
        string script = Path.Combine(Root, "scripts", "Start-Harness.ps1");
        if (!File.Exists(script) || (!File.Exists(Path.Combine(Root, "python", "python.exe")) && !File.Exists(Path.Combine(Root, ".venv", "Scripts", "python.exe"))))
            throw new InvalidOperationException("OIF files are missing. Keep OIF.exe inside the complete application folder.");
        // Same install shares a startup mutex. A second click reuses the service.
        string mutexName;
        using (var hash = System.Security.Cryptography.SHA256.Create())
            mutexName = "Local\\OIF-" + BitConverter.ToString(hash.ComputeHash(Encoding.UTF8.GetBytes(Root.ToLowerInvariant()))).Replace("-", "");
        using (var mutex = new Mutex(false, mutexName)) {
            bool acquired = false;
            try {
                try { acquired = mutex.WaitOne(TimeSpan.FromSeconds(45)); }
                catch (AbandonedMutexException) { acquired = true; }
                if (!acquired) throw new InvalidOperationException("Another launch is in progress. Please wait, then open OIF again.");
                var start = new ProcessStartInfo(Path.Combine(Environment.GetFolderPath(Environment.SpecialFolder.System), "WindowsPowerShell", "v1.0", "powershell.exe"),
                    "-NoProfile -NonInteractive -ExecutionPolicy Bypass -File " + Quote(script) +
                    (requiredPort == 0 ? "" : " -Port " + requiredPort.ToString(System.Globalization.CultureInfo.InvariantCulture)));
                start.WorkingDirectory = Root; start.UseShellExecute = false; start.CreateNoWindow = true;
                start.WindowStyle = ProcessWindowStyle.Hidden; start.RedirectStandardOutput = true; start.RedirectStandardError = true;
                using (var process = Process.Start(start)) {
                    var output = process.StandardOutput.ReadToEndAsync(); var error = process.StandardError.ReadToEndAsync();
                    if (!process.WaitForExit(45000)) throw new TimeoutException("Startup is taking longer than expected. The process was retained; inspect the launch log.");
                    // PowerShell's detached child can retain inherited pipe handles.
                    // Read readiness independently; never wait indefinitely for pipe EOF.
                    if (Task.WaitAll(new Task[] { output, error }, 1000)) Record(output.Result + error.Result);
                    else Record("Startup command exited; detached output handles remain open. Checking owned service readiness.");
                    if (process.ExitCode != 0) throw new InvalidOperationException("The service could not start. Details were saved in the launch log.");
                }
                var owned = ReadRecord();
                string url = "http://127.0.0.1:" + Convert.ToInt32(owned["port"]) + "/";
                string launch = Convert.ToString(owned["launch_id"]);
                var cookies = new CookieContainer();
                var deadline = DateTime.UtcNow.AddSeconds(45);
                string last = "Waiting for service";
                while (DateTime.UtcNow < deadline) {
                    try {
                        var request = (HttpWebRequest)WebRequest.Create(url); request.Proxy = null; request.Timeout = 2500; request.CookieContainer = cookies;
                        using (var response = request.GetResponse()) { }
                        request = (HttpWebRequest)WebRequest.Create(url + "api/status"); request.Proxy = null; request.Timeout = 5000; request.CookieContainer = cookies;
                        using (var response = request.GetResponse()) using (var reader = new StreamReader(response.GetResponseStream())) {
                            var status = Json.Deserialize<Dictionary<string, object>>(reader.ReadToEnd());
                            if (Convert.ToString(status["service"]) == "available" && Convert.ToString(status["launch_id"]) == launch) {
                                Record("Ready: " + url + " owned launch confirmed"); return url;
                            }
                            last = "The responding service does not match this launch.";
                        }
                    } catch (Exception exception) { last = exception.Message; }
                    Thread.Sleep(350);
                }
                throw new TimeoutException("OIF readiness could not be confirmed. Records were preserved. " + last);
            } finally { if (acquired) mutex.ReleaseMutex(); }
        }
    }
    internal static string InstallKey {
        get { using (var hash = System.Security.Cryptography.SHA256.Create())
            return BitConverter.ToString(hash.ComputeHash(Encoding.UTF8.GetBytes(Path.GetFullPath(Root).TrimEnd('\\').ToLowerInvariant()))).Replace("-", "").Substring(0, 24); }
    }
    internal static string AppId { get { return "OIF.Desktop." + InstallKey; } }
    internal static string ProfileRoot { get { return Path.Combine(Environment.GetFolderPath(Environment.SpecialFolder.LocalApplicationData), "OIF", "Desktop", InstallKey); } }
    [DllImport("shell32.dll", CharSet = CharSet.Unicode)] static extern int SetCurrentProcessExplicitAppUserModelID(string appID);
    [DllImport("user32.dll")] static extern bool AllowSetForegroundWindow(int processId);
    static int? HandlePendingUpdate() {
        try {
            string pending = Path.Combine(Root, ".runtime", "product-update-pending.json");
            if (!File.Exists(pending)) return null;
            var record = Json.Deserialize<Dictionary<string, object>>(File.ReadAllText(pending));
            string id = Convert.ToString(record["work_id"]);
            if (Convert.ToString(record["schema"]) != "oif-product-update-pending-v1" || !System.Text.RegularExpressions.Regex.IsMatch(id, "^[a-f0-9]{32}$"))
                throw new InvalidOperationException("The pending update record needs recovery.");
            string work = Path.Combine(Root, ".runtime", "product-updates", id);
            string path = Path.Combine(work, "helper.lock");
            if (File.Exists(path)) {
                try { using (File.Open(path, FileMode.Open, FileAccess.ReadWrite, FileShare.None)) { } }
                catch (IOException) { Record("The selected update is in progress; the app will reopen."); return 0; }
            }
            string staged = Path.Combine(work, "package");
            string runner = Path.Combine(staged, "src", "policy_harness", "product_install.py");
            string actual;
            using (var sha = System.Security.Cryptography.SHA256.Create())
                actual = BitConverter.ToString(sha.ComputeHash(File.ReadAllBytes(runner))).Replace("-", "");
            if (!String.Equals(actual, Convert.ToString(record["helper_sha256"]), StringComparison.OrdinalIgnoreCase))
                throw new InvalidOperationException("The pending updater changed. Its backup was retained.");
            var start = new ProcessStartInfo(Path.Combine(staged, "python", "python.exe"),
                "-B -I " + Quote(runner) + " --work " + Quote(work) + " --data-dir " + Quote(Path.Combine(Root, ".runtime")) + " --recover");
            start.WorkingDirectory = staged; start.UseShellExecute = false; start.CreateNoWindow = true; start.WindowStyle = ProcessWindowStyle.Hidden;
            using (var helper = Process.Start(start)) { Record("Pending update recovery handed to staged helper PID " + helper.Id); }
            // Exit before rollback replaces this executable. The helper waits
            // for owner/file locks and reopens the recovered app itself.
            return 0;
        } catch (Exception error) {
            Record("Update recovery: " + error);
            MessageBox.Show("OIF update recovery needs attention. Records and backups were retained.\n" + error.Message, "OIF", MessageBoxButtons.OK, MessageBoxIcon.Error);
            return 1;
        }
    }
    [STAThread]
    static int Main(string[] args) {
        bool serviceOnly = Array.IndexOf(args, "--service-only") >= 0;
        int? pendingExit = HandlePendingUpdate();
        if (pendingExit.HasValue) return pendingExit.Value;
        if (serviceOnly) {
            try { StartService(); return 0; }
            catch (Exception error) { Record(error.ToString()); return 1; }
        }
        if (args.Length == 2 && args[0] == "--register-shortcut") {
            try { OifShell.SetShortcutIdentity(args[1], AppId); return 0; }
            catch (Exception error) { Record(error.ToString()); return 1; }
        }
        SetCurrentProcessExplicitAppUserModelID(AppId);
        Application.EnableVisualStyles(); Application.SetCompatibleTextRenderingDefault(false);
        bool created;
        using (var activate = new EventWaitHandle(false, EventResetMode.AutoReset, "Local\\OIF-Activate-" + InstallKey))
        using (var instance = new Mutex(true, "Local\\OIF-Window-" + InstallKey, out created)) {
            if (!created) { AllowSetForegroundWindow(-1); activate.Set(); return 0; }
            try {
                using (var form = new OifWindow(ProfileRoot)) {
                    var wait = ThreadPool.RegisterWaitForSingleObject(activate, (state, timedOut) => {
                        if (form.IsHandleCreated && !form.IsDisposed) {
                            try { form.BeginInvoke((Action)(() => form.ActivateWindow())); }
                            catch (InvalidOperationException) { }
                        }
                    }, null, Timeout.Infinite, false);
                    try { Application.Run(form); } finally { wait.Unregister(null); }
                }
                return 0;
            } catch (Exception error) {
                Record(error.ToString());
                MessageBox.Show("OIF could not open.\n" + error.Message, "OIF", MessageBoxButtons.OK, MessageBoxIcon.Error);
                return 1;
            } finally { instance.ReleaseMutex(); }
        }
    }
}
