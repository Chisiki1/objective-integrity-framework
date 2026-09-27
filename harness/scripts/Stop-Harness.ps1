[CmdletBinding()]
param([string]$DataDirectory)
$ErrorActionPreference = 'Stop'
$harnessRoot = [IO.Path]::GetFullPath((Join-Path $PSScriptRoot '..'))
$env:PYTHONPATH = Join-Path $harnessRoot 'src'
$portablePython = Join-Path $harnessRoot 'python\python.exe'
$harnessData = if ($DataDirectory) { [IO.Path]::GetFullPath($DataDirectory) } else { Join-Path $harnessRoot '.runtime' }
$processFile = Join-Path $harnessData 'server-process.json'
if (-not (Test-Path -LiteralPath $processFile -PathType Leaf)) { Write-Output 'No owned server process record exists.'; return }
$lock = [IO.File]::Open((Join-Path $harnessData 'server-process.lock'), [IO.FileMode]::OpenOrCreate, [IO.FileAccess]::ReadWrite, [IO.FileShare]::None)
try {
    $saved = Get-Content -LiteralPath $processFile -Raw | ConvertFrom-Json
    $process = Get-Process -Id $saved.pid -ErrorAction SilentlyContinue
    if (-not $process) { Write-Output 'The recorded process has exited. Its identity and task records were preserved.'; return }
    $expectedPython = if (Test-Path -LiteralPath $portablePython) { $portablePython } else { Join-Path $harnessRoot '.venv\Scripts\python.exe' }
    if ($saved.data_dir -ne $harnessData -or $saved.executable -ne $expectedPython -or $process.Path -ne $expectedPython -or $process.StartTime.ToUniversalTime().Ticks -ne [long]$saved.start_ticks -or $saved.launch_id -notmatch '^[a-f0-9]{32}$') {
        throw 'Process ownership is not established. No process was stopped.'
    }
    # Keep the exact OS process handle before requesting exit; a Get-Process
    # object without this retained handle may lose its exit-code observation.
    $ownedProcessHandle = $process.Handle
    & $expectedPython -B -m policy_harness.cli shutdown --url "http://127.0.0.1:$($saved.port)" --launch-id $saved.launch_id
    if ($LASTEXITCODE -ne 0) { throw 'Graceful shutdown was not confirmed. No forced termination was attempted; preserve the process and records.' }
    # Stable supervisor identity spans approved worker replacement. Normal worker
    # shutdown exits the supervisor; no periodic polling or unrelated kill.
    $process.WaitForExit()
    if ($null -eq $process.ExitCode -or $process.ExitCode -ne 0) {
        throw "The owned process exited with code $($process.ExitCode). This is not confirmed normal completion. Task state, effects and first-fault logs were preserved."
    }
    Write-Output 'The owned server exited with code 0. Task state, operation effects and logs were preserved.'
} finally { $lock.Dispose() }
