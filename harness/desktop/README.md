# Desktop dependencies

`scripts/Build-Desktop.ps1` restores the pinned Microsoft.Web.WebView2 1.0.4191.47 SDK from the official NuGet registry and checks the package SHA-256 before extracting its two .NET assemblies, x64 loader and license here. Keep these files beside `OIF.exe` as part of the application folder. The installed Evergreen WebView2 Runtime supplies the browser engine.

References: [Microsoft WinForms setup](https://learn.microsoft.com/en-us/microsoft-edge/webview2/get-started/winforms), [SDK package](https://www.nuget.org/packages/Microsoft.Web.WebView2/1.0.4191.47), [Windows application identity](https://learn.microsoft.com/en-us/windows/win32/shell/appids).

The desktop app uses the original frontend and service. Its browser profile is in `%LOCALAPPDATA%\OIF\Desktop\<installation-id>\WebView2`. It does not read or copy an existing browser's cookies or credentials.
