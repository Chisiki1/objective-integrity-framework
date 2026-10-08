[CmdletBinding()]
param([ValidateRange(0,65535)][int]$Port = 0, [switch]$OpenBrowser, [string]$DataDirectory)
$ErrorActionPreference = 'Stop'
Import-Module Microsoft.PowerShell.Utility -ErrorAction Stop
$harnessRoot = [IO.Path]::GetFullPath((Join-Path $PSScriptRoot '..'))
$env:PYTHONPATH = Join-Path $harnessRoot 'src'
$portablePython = Join-Path $harnessRoot 'python\python.exe'
$harnessPython = if (Test-Path -LiteralPath $portablePython) { $portablePython } else { Join-Path $harnessRoot '.venv\Scripts\python.exe' }
$harnessData = if ($DataDirectory) { [IO.Path]::GetFullPath($DataDirectory) } else { Join-Path $harnessRoot '.runtime' }
[IO.Directory]::CreateDirectory($harnessData) | Out-Null
$lockPath = Join-Path $harnessData 'server-process.lock'
$processFile = Join-Path $harnessData 'server-process.json'
$lock = [IO.File]::Open($lockPath, [IO.FileMode]::OpenOrCreate, [IO.FileAccess]::ReadWrite, [IO.FileShare]::None)
try {
    if (Test-Path -LiteralPath $processFile) {
        $saved = Get-Content -LiteralPath $processFile -Raw | ConvertFrom-Json
        if ($saved.data_dir -ne $harnessData -or $saved.executable -ne $harnessPython -or $saved.launch_id -notmatch '^[a-f0-9]{32}$' -or -not $saved.start_ticks) {
            throw 'The saved process record does not belong to this installation. No process was changed. Preserve and inspect server-process.json.'
        }
        $existing = Get-Process -Id $saved.pid -ErrorAction SilentlyContinue
        if ($existing -and $existing.StartTime.ToUniversalTime().Ticks -ne [long]$saved.start_ticks) {
            # Windows can reuse the PID after OIF exits. This is a different
            # process, so never send it shutdown or use it as the service.
            $existing.Dispose()
            $existing = $null
        }
        if ($existing) {
            if ($existing.Path -ne $saved.executable) {
                throw 'The saved process identity no longer matches. No process was changed. Preserve and inspect server-process.json.'
            }
            $ownedHandle = $existing.Handle
            $freshness = & $harnessPython -B -m policy_harness.cli shutdown --url "http://127.0.0.1:$($saved.port)" --launch-id $saved.launch_id --if-idle --if-source-changed 2>&1
            $refresh = $null
            if ($LASTEXITCODE -eq 0) { try { $refresh = ($freshness -join "`n") | ConvertFrom-Json } catch { } }
            if ($refresh -and $refresh.status -eq 'shutdown_requested') {
                if (-not $existing.WaitForExit(20000)) { throw 'The owned service is still shutting down. Its records were preserved; open OIF again after it exits.' }
                if ($existing.ExitCode -ne 0) { throw 'The owned service did not exit normally. Its records were preserved.' }
                $Port = [int]$saved.port
                Write-Output 'Updated application files detected. Starting a fresh service with the same task records.'
            } else {
                Write-Output "Reusing owned service PID $($saved.pid), http://127.0.0.1:$($saved.port)/. Running work is preserved."
                if ($OpenBrowser) { Start-Process "http://127.0.0.1:$($saved.port)/" }
                return
            }
        }
        # Preserve the previous identity, including an unexpected prior exit.
        $oldRecord = Join-Path $harnessData ('server-process.previous-' + [guid]::NewGuid().ToString('N') + '.json')
        [IO.File]::Copy($processFile, $oldRecord, $false)
    }
    $pendingUpdate = Join-Path $harnessData 'product-update-pending.json'
    if (Test-Path -LiteralPath $pendingUpdate) {
        $pending = Get-Content -LiteralPath $pendingUpdate -Raw | ConvertFrom-Json
        if ($pending.schema -ne 'oif-product-update-pending-v1' -or $pending.work_id -notmatch '^[a-f0-9]{32}$') { throw 'The pending update record needs recovery.' }
        $updateWork = Join-Path (Join-Path $harnessData 'product-updates') $pending.work_id
        $updateRunner = Join-Path $updateWork 'package\src\policy_harness\product_install.py'
        if ((Get-FileHash -LiteralPath $updateRunner -Algorithm SHA256).Hash -ne $pending.helper_sha256) { throw 'The pending updater changed. Its backup was retained.' }
        $updatePython = Join-Path $updateWork 'package\python\python.exe'
        & $updatePython -B -I $updateRunner --work $updateWork --data-dir $harnessData --recover --no-reopen
        if ($LASTEXITCODE -ne 0 -or (Test-Path -LiteralPath $pendingUpdate)) { throw 'OIF is still updating or recovering. Wait, then open OIF again. No task was restarted.' }
    }
    $harnessPython = if (Test-Path -LiteralPath $portablePython) { $portablePython } else { Join-Path $harnessRoot '.venv\Scripts\python.exe' }
    if (-not (Test-Path -LiteralPath $harnessPython -PathType Leaf)) { throw 'Python is missing. Extract the complete Windows package or install the source dependencies.' }
    $probe = [Net.Sockets.TcpListener]::new([Net.IPAddress]::Loopback, $Port)
    try { $probe.Start(); $Port = $probe.LocalEndpoint.Port } finally { $probe.Stop() }
    $launchId = [guid]::NewGuid().ToString('N')
    $bytecodePrefix = Join-Path $harnessData ('.controller-restarts\bytecode-start-' + $launchId)
    if (Test-Path -LiteralPath $bytecodePrefix) { throw 'The new source-loading identity already exists. No process was started.' }
    $arguments = @('-B','-X',('"pycache_prefix=' + $bytecodePrefix + '"'),'-m','policy_harness.cli','supervise','--host','127.0.0.1','--port',"$Port",'--data-dir',('"' + $harnessData + '"'),'--launch-id',$launchId)
    $stdoutPath = Join-Path $harnessData ("server-$launchId.stdout.log")
    $stderrPath = Join-Path $harnessData ("server-$launchId.stderr.log")
    $started = Start-Process -FilePath $harnessPython -ArgumentList $arguments -WorkingDirectory $harnessRoot -WindowStyle Hidden -PassThru -RedirectStandardOutput $stdoutPath -RedirectStandardError $stderrPath
    $record = [ordered]@{schema='harness-process-v1';mode='supervise';pid=$started.Id;start_ticks=$started.StartTime.ToUniversalTime().Ticks;executable=$harnessPython;port=$Port;launch_id=$launchId;data_dir=$harnessData;stdout=$stdoutPath;stderr=$stderrPath}
    $temporary = Join-Path $harnessData ('server-process.' + $launchId + '.tmp')
    [IO.File]::WriteAllText($temporary, ($record | ConvertTo-Json), [Text.UTF8Encoding]::new($false))
    Move-Item -LiteralPath $temporary -Destination $processFile -Force
    Write-Output "Started owned process PID $($started.Id). Open http://127.0.0.1:$Port/ . This launch message is not a readiness or task-success check."
    if ($OpenBrowser) { Start-Process "http://127.0.0.1:$Port/" }
} finally { $lock.Dispose() }
