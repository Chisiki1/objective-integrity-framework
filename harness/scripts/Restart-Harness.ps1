[CmdletBinding()]
param([Parameter(Mandatory=$true)][string]$DataDirectory, [Parameter(Mandatory=$true)][string]$LaunchId)
$ErrorActionPreference = 'Stop'
$harnessRoot = [IO.Path]::GetFullPath((Join-Path $PSScriptRoot '..'))
$harnessData = [IO.Path]::GetFullPath($DataDirectory)
$recordPath = Join-Path $harnessData 'server-process.json'
$saved = Get-Content -LiteralPath $recordPath -Raw | ConvertFrom-Json
if ($saved.launch_id -ne $LaunchId -or $saved.data_dir -ne $harnessData) { throw 'Restart ownership changed.' }
$owned = Get-Process -Id $saved.pid -ErrorAction SilentlyContinue
if ($owned) {
    if ($owned.Path -ne $saved.executable -or $owned.StartTime.ToUniversalTime().Ticks -ne [long]$saved.start_ticks) { throw 'Restart process identity changed.' }
    $ownedHandle = $owned.Handle
    if (-not $owned.WaitForExit(45000)) { throw 'The service is still stopping. No process was forced to exit.' }
    if ($owned.ExitCode -ne 0) { throw 'The service did not exit normally; records were preserved.' }
}
$current = Get-Content -LiteralPath $recordPath -Raw | ConvertFrom-Json
if ($current.launch_id -ne $LaunchId) { throw 'Another launch already replaced the service.' }
& (Join-Path $PSScriptRoot 'Start-Harness.ps1') -DataDirectory $harnessData -Port ([int]$saved.port)
