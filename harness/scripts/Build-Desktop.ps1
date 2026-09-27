[CmdletBinding()]
param([switch]$DesktopShortcut, [string]$OutputPath)
$ErrorActionPreference = 'Stop'
Import-Module Microsoft.PowerShell.Utility -ErrorAction Stop
$oifRoot = [IO.Path]::GetFullPath((Join-Path $PSScriptRoot '..'))
$oifCompiler = Join-Path $env:WINDIR 'Microsoft.NET\Framework64\v4.0.30319\csc.exe'
if (-not (Test-Path -LiteralPath $oifCompiler)) { throw '.NET Framework compiler was not found.' }
$oifVersion = '1.0.4191.47'
$oifExpectedHash = 'F492BBF547D0DA329553B6727435B677579B1E9F91CC9E4A1AD029366D5F23D0'
$oifCache = Join-Path $oifRoot '.work\desktop-sdk'
[IO.Directory]::CreateDirectory($oifCache) | Out-Null
$oifPackage = Join-Path $oifCache "microsoft.web.webview2.$oifVersion.nupkg"
if (-not (Test-Path -LiteralPath $oifPackage)) {
    $oifDownload = "$oifPackage.download"
    [Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12
    Invoke-WebRequest -UseBasicParsing -Uri "https://api.nuget.org/v3-flatcontainer/microsoft.web.webview2/$oifVersion/microsoft.web.webview2.$oifVersion.nupkg" -OutFile $oifDownload
    if ((Get-FileHash -LiteralPath $oifDownload -Algorithm SHA256).Hash -ne $oifExpectedHash) { throw 'WebView2 SDK package verification failed; downloaded file retained.' }
    Move-Item -LiteralPath $oifDownload -Destination $oifPackage
}
if ((Get-FileHash -LiteralPath $oifPackage -Algorithm SHA256).Hash -ne $oifExpectedHash) { throw 'Cached WebView2 SDK does not match the pinned package.' }
$oifDependencies = Join-Path $oifRoot 'desktop'
[IO.Directory]::CreateDirectory($oifDependencies) | Out-Null
Add-Type -AssemblyName System.IO.Compression.FileSystem
$oifArchive = [IO.Compression.ZipFile]::OpenRead($oifPackage)
try {
    $oifMembers = @{
        'lib/net462/Microsoft.Web.WebView2.Core.dll' = 'Microsoft.Web.WebView2.Core.dll'
        'lib/net462/Microsoft.Web.WebView2.WinForms.dll' = 'Microsoft.Web.WebView2.WinForms.dll'
        'runtimes/win-x64/native/WebView2Loader.dll' = 'WebView2Loader.dll'
        'LICENSE.txt' = 'WebView2-LICENSE.txt'
    }
    foreach ($oifMember in $oifMembers.Keys) {
        $oifEntry = $oifArchive.GetEntry($oifMember)
        if (-not $oifEntry) { throw "Missing SDK member: $oifMember" }
        $oifDestination = Join-Path $oifDependencies $oifMembers[$oifMember]
        $oifMemory = [IO.MemoryStream]::new()
        $oifInput = $oifEntry.Open()
        try { $oifInput.CopyTo($oifMemory); $oifBytes = $oifMemory.ToArray() } finally { $oifInput.Dispose(); $oifMemory.Dispose() }
        $oifDigest = [Security.Cryptography.SHA256]::Create()
        try { $oifMemberHash = [BitConverter]::ToString($oifDigest.ComputeHash($oifBytes)).Replace('-','') } finally { $oifDigest.Dispose() }
        if ((Test-Path -LiteralPath $oifDestination) -and (Get-FileHash -LiteralPath $oifDestination -Algorithm SHA256).Hash -eq $oifMemberHash) { continue }
        [IO.File]::WriteAllBytes($oifDestination, $oifBytes)
    }
} finally { $oifArchive.Dispose() }
$oifIconPath = Join-Path $PSScriptRoot 'oif-white.ico'
if (-not (Test-Path -LiteralPath $oifIconPath)) { throw 'The existing OIF white icon is missing.' }
$oifExe = if ($OutputPath) { [IO.Path]::GetFullPath($OutputPath) } else { Join-Path $oifRoot 'OIF.exe' }
if ([IO.Path]::GetDirectoryName($oifExe) -ne $oifRoot) { throw 'The executable must stay in the app root beside its service and desktop dependencies.' }
$oifTemporary = Join-Path $oifRoot ('OIF-build-' + [guid]::NewGuid().ToString('N') + '.exe')
& $oifCompiler /nologo /target:winexe /platform:x64 /optimize+ "/out:$oifTemporary" "/win32icon:$oifIconPath" "/win32manifest:$(Join-Path $PSScriptRoot 'OIF.manifest')" /reference:System.Windows.Forms.dll /reference:System.Drawing.dll /reference:System.Web.Extensions.dll "/reference:$(Join-Path $oifDependencies 'Microsoft.Web.WebView2.Core.dll')" "/reference:$(Join-Path $oifDependencies 'Microsoft.Web.WebView2.WinForms.dll')" (Join-Path $PSScriptRoot 'OifLauncher.cs') (Join-Path $PSScriptRoot 'OifWindow.cs') (Join-Path $PSScriptRoot 'OifShell.cs')
if ($LASTEXITCODE -ne 0) { throw 'OIF desktop compilation failed; the existing executable was preserved.' }
if (Test-Path -LiteralPath $oifExe) {
    $oifBackup = Join-Path $oifCache ('before-' + [guid]::NewGuid().ToString('N') + '.exe')
    [IO.File]::Copy($oifExe, $oifBackup, $false)
}
Copy-Item -LiteralPath (Join-Path $PSScriptRoot 'OIF.exe.config') -Destination "$oifExe.config" -Force
Move-Item -LiteralPath $oifTemporary -Destination $oifExe -Force
if ($DesktopShortcut) {
    if ($oifExe -ne (Join-Path $oifRoot 'OIF.exe')) { throw 'The desktop shortcut must target the installed OIF.exe.' }
    $oifShortcutPath = Join-Path ([Environment]::GetFolderPath('Desktop')) 'OIF.lnk'
    $oifShell = New-Object -ComObject WScript.Shell
    $oifShortcut = $oifShell.CreateShortcut($oifShortcutPath)
    if ((Test-Path -LiteralPath $oifShortcutPath) -and $oifShortcut.TargetPath -and $oifShortcut.TargetPath -ne $oifExe) { throw 'A different OIF shortcut exists; it was preserved.' }
    $oifShortcut.TargetPath = $oifExe; $oifShortcut.WorkingDirectory = $oifRoot
    $oifShortcut.IconLocation = "$oifExe,0"; $oifShortcut.Description = 'Open OIF'
    $oifShortcut.Save()
    $oifRegistered = Start-Process -FilePath $oifExe -ArgumentList @('--register-shortcut', ('"' + $oifShortcutPath + '"')) -WindowStyle Hidden -Wait -PassThru
    if ($oifRegistered.ExitCode -ne 0) { throw 'OIF shortcut identity could not be saved.' }
}
Write-Output "Built $oifExe (WebView2 SDK $oifVersion)"
