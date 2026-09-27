// Run against the built app assembly in an isolated browser profile. No model
// task is submitted and no existing task is modified by this component check.
using System;
using System.Collections.Generic;
using System.Diagnostics;
using System.Drawing;
using System.IO;
using System.Reflection;
using System.Threading.Tasks;
using System.Web.Script.Serialization;
using System.Windows.Forms;
using Microsoft.Web.WebView2.Core;
using Microsoft.Web.WebView2.WinForms;

class NativeSmoke {
    static string output;
    static JavaScriptSerializer json = new JavaScriptSerializer();
    static List<object> checks = new List<object>();
    static BindingFlags flags = BindingFlags.Instance | BindingFlags.NonPublic | BindingFlags.Public;
    static int exit = 1;
    static void Check(bool ok, string name, object detail) {
        checks.Add(new { name = name, passed = ok, detail = detail });
        if (!ok) throw new Exception(name + ": " + json.Serialize(detail));
    }
    static async Task<object> Eval(CoreWebView2 core, string expression) {
        string raw = await core.CallDevToolsProtocolMethodAsync("Runtime.evaluate", json.Serialize(new { expression = expression, awaitPromise = true, returnByValue = true }));
        var response = json.Deserialize<Dictionary<string, object>>(raw);
        if (response.ContainsKey("exceptionDetails")) throw new Exception(raw);
        var result = (Dictionary<string, object>)response["result"];
        return result.ContainsKey("value") ? result["value"] : null;
    }
    static async Task Wait(Func<Task<bool>> condition) {
        var until = DateTime.UtcNow.AddSeconds(25);
        do { if (await condition()) return; await Task.Delay(100); } while (DateTime.UtcNow < until);
        throw new TimeoutException("Native component condition did not complete");
    }
    static async Task Capture(CoreWebView2 core, string name) {
        using (var stream = File.Create(Path.Combine(output, name + ".png")))
            await core.CapturePreviewAsync(CoreWebView2CapturePreviewImageFormat.Png, stream);
    }
    [STAThread] static int Main(string[] args) {
        output = Path.GetFullPath(args[1]); Directory.CreateDirectory(output);
        var assembly = Assembly.LoadFrom(Path.GetFullPath(args[0]));
        var type = assembly.GetType("OifWindow", true);
        Application.EnableVisualStyles(); Application.SetCompatibleTextRenderingDefault(false);
        Func<Task<string>> service = () => Task.FromResult(args[2]);
        var form = (Form)Activator.CreateInstance(type, flags, null, new object[] { Path.Combine(output, "profile"), service }, null);
        form.ClientSize = new Size(1200, 772);
        var start = Stopwatch.StartNew();
        var timer = new Timer { Interval = 60000 };
        timer.Tick += (s, e) => { File.WriteAllText(Path.Combine(output, "timeout.txt"), "Native smoke timeout"); form.Close(); };
        form.Shown += async (s, e) => {
            try {
                WebView2 browser = null;
                await Wait(async () => {
                    browser = (WebView2)type.GetProperty("Browser", flags).GetValue(form);
                    return browser != null && browser.CoreWebView2 != null && Convert.ToBoolean(await Eval(browser.CoreWebView2, "document.title==='OIF' && document.querySelectorAll('#task-list > *').length>0"));
                });
                var core = browser.CoreWebView2;
                Check(true, "existing-interface-loaded", new { milliseconds = start.Elapsed.TotalMilliseconds, url = core.Source, title = form.Text, runtime = core.Environment.BrowserVersionString });
                var view = (Dictionary<string, object>)await Eval(core, "({theme:document.documentElement.dataset.theme,bodyFont:getComputedStyle(document.body).fontSize,width:innerWidth,height:innerHeight,tasks:document.querySelectorAll('#task-list > *').length,controls:['new-pick','new-access','submit-task'].map(id=>{const e=document.getElementById(id),r=e.getBoundingClientRect(),s=getComputedStyle(e);return {id,x:r.x,y:r.y,width:r.width,height:r.height,font:s.fontSize,color:s.color,background:s.backgroundColor}})})");
                Check(Convert.ToString(view["bodyFont"]) == "16px" && Convert.ToString(view["theme"]) == "dark", "same-default-font-and-theme", view);
                await Capture(core, "native-dark");
                var timing = await Eval(core, "({navigation:performance.getEntriesByType('navigation').map(n=>({dom:n.domContentLoadedEventEnd,load:n.loadEventEnd})),paint:performance.getEntriesByType('paint').map(n=>({name:n.name,ms:n.startTime}))})");
                checks.Add(new { name = "native-render-timing", detail = timing });
                Check(Convert.ToBoolean(await Eval(core, "(async()=>{const r=await fetch('/api/status');return r.ok&&(await r.json()).service==='available'})()")), "same-service-authentication", null);
                Check(!core.Settings.AreHostObjectsAllowed, "no-general-native-host-bridge", null);
                // Exercise the real login handler without starting a provider login.
                Check(Convert.ToBoolean(await Eval(core, "(async()=>{const previous=api,open=window.open;let redirected=false;try{window.open=()=>({closed:true,opener:null,location:{replace:()=>{redirected=true}},close:()=>{}});api=async path=>{if(path!=='/api/settings/login/openrouter')throw new Error('Unexpected request');return {url:'https://openrouter.ai/auth?component_check=1'}};await document.getElementById('openrouter-login').onclick();return !redirected&&document.querySelector('#settings-error a')?.href==='https://openrouter.ai/auth?component_check=1'}finally{api=previous;window.open=open;document.getElementById('settings-error').replaceChildren()}})()")), "closed-login-popup-has-explicit-link", "Controlled closed-window return; no provider request");
                var attachment = Path.Combine(output, "attachment.txt"); File.WriteAllText(attachment, "native attachment check");
                await core.CallDevToolsProtocolMethodAsync("DOM.enable", "{}");
                var document = json.Deserialize<Dictionary<string, object>>(await core.CallDevToolsProtocolMethodAsync("DOM.getDocument", "{}"));
                int rootId = Convert.ToInt32(((Dictionary<string, object>)document["root"])["nodeId"]);
                var node = json.Deserialize<Dictionary<string, object>>(await core.CallDevToolsProtocolMethodAsync("DOM.querySelector", json.Serialize(new { nodeId = rootId, selector = "#new-file-input" })));
                Check(Convert.ToInt32(node["nodeId"]) != 0, "attachment-input-present", null);
                await core.CallDevToolsProtocolMethodAsync("DOM.setFileInputFiles", json.Serialize(new { nodeId = node["nodeId"], files = new [] { attachment } }));
                Check(Convert.ToBoolean(await Eval(core, "document.body.innerText.includes('attachment.txt')")), "file-selection-consumer", null);
                await Eval(core, "(()=>{const target=document.getElementById('objective'),data=new DataTransfer();data.items.add(new File(['paste check'],'pasted.txt',{type:'text/plain'}));target.dispatchEvent(new ClipboardEvent('paste',{clipboardData:data,bubbles:true,cancelable:true}));return true})()");
                Check(Convert.ToBoolean(await Eval(core, "document.body.innerText.includes('pasted.txt')")), "paste-handler-consumer", "Synthetic clipboard event; no system clipboard changes");
                await Eval(core, "(()=>{document.getElementById('theme-toggle').click();return true})()");
                Check(Convert.ToString(await Eval(core, "document.documentElement.dataset.theme")) == "light", "light-theme", null);
                await Capture(core, "native-light");
                await Eval(core, "(()=>{document.getElementById('theme-toggle').click();return true})()");
                // Read an existing completed chat and preview its recorded artifact.
                await Eval(core, "(()=>{document.querySelector('#task-list button').click();return true})()");
                await Wait(async () => Convert.ToBoolean(await Eval(core, "document.querySelector('#task-artifacts a.artifact-link')!==null")));
                var chat = core.Source;
                Check(chat.Contains("#task="), "existing-chat-navigation", chat);
                await Eval(core, "(()=>{document.querySelector('#task-artifacts a.artifact-link').click();return true})()");
                await Wait(async () => Convert.ToBoolean(await Eval(core, "document.querySelector('#artifact-dialog').open")));
                await Capture(core, "native-preview");
                Check(true, "existing-artifact-preview", await Eval(core, "document.querySelector('#artifact-dialog').innerText.slice(0,500)"));
                var downloaded = new TaskCompletionSource<bool>();
                var downloadPath = Path.Combine(output, "downloaded.txt");
                core.DownloadStarting += (sender, download) => {
                    download.ResultFilePath = downloadPath; download.Handled = true;
                    download.DownloadOperation.StateChanged += (sender2, state2) => {
                        if (download.DownloadOperation.State == CoreWebView2DownloadState.Completed) downloaded.TrySetResult(true);
                        if (download.DownloadOperation.State == CoreWebView2DownloadState.Interrupted) downloaded.TrySetResult(false);
                    };
                };
                await Eval(core, "(()=>{document.getElementById('artifact-download').click();return true})()");
                await Wait(() => Task.FromResult(downloaded.Task.IsCompleted));
                Check(await downloaded.Task && File.ReadAllText(downloadPath) == "hello", "artifact-download-exact-bytes", new FileInfo(downloadPath).Length);
                await Eval(core, "(()=>{document.querySelector('#artifact-dialog').close();return true})()");
                form.ClientSize = new Size(740, 680);
                await Task.Delay(100);
                Check(Convert.ToBoolean(await Eval(core, "document.documentElement.scrollWidth<=innerWidth")), "narrow-no-horizontal-overflow", null);
                form.ClientSize = new Size(1200, 772);
                core.Navigate("file:///C:/Windows/win.ini");
                await Task.Delay(150);
                Check(core.Source == chat, "blocked-file-navigation-keeps-chat", core.Source);
                Check(!((Panel)type.GetField("notice", flags).GetValue(form)).Visible, "blocked-navigation-keeps-chat-visible", null);
                // Recreate only the view with the same profile and URL fragment.
                await (Task)type.GetMethod("InitializeAsync", flags).Invoke(form, null);
                await Wait(async () => {
                    browser = (WebView2)type.GetProperty("Browser", flags).GetValue(form);
                    return browser != null && browser.CoreWebView2 != null && Convert.ToBoolean(await Eval(browser.CoreWebView2, "document.title==='OIF'&&location.hash.startsWith('#task=')"));
                });
                Check(browser.CoreWebView2.Source == chat, "view-recovery-preserves-chat", browser.CoreWebView2.Source);
                Check(Convert.ToBoolean(await Eval(browser.CoreWebView2, "(async()=>{const r=await fetch('/api/status');return r.ok&&(await r.json()).service==='available'})()")), "service-still-available-after-view-recovery", null);
                exit = 0;
            } catch (Exception error) { checks.Add(new { name = "failure", detail = error.ToString() }); }
            finally {
                File.WriteAllText(Path.Combine(output, "result.json"), json.Serialize(new { passed = exit == 0, checks = checks }));
                timer.Stop(); form.Close();
            }
        };
        timer.Start(); Application.Run(form); form.Dispose(); return exit;
    }
}
