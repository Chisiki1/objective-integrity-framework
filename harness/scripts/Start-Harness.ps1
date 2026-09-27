[CmdletBinding()]
param([ValidateRange(0,65535)][int]$Port = 0, [switch]$OpenBrowser, [string]$DataDirectory)
$ErrorActionPreference = 'Stop'
$harnessRoot = [IO.Path]::GetFullPath((Join-Path $PSScriptRoot '..'))
$env:PYTHONPATH = Join-Path $harnessRoot 'src'
$portablePython = Join-Path $harnessRoot 'python\python.exe'
$harnessPython = if (Test-Path -LiteralPath $portablePython) { $portablePython } else { Join-Path $harnessRoot '.venv\Scripts\python.exe' }
$harnessData = if ($DataDirectory) { [IO.Path]::GetFullPath($DataDirectory) } else { Join-Path $harnessRoot '.runtime' }
if (-not (Test-Path -LiteralPath $harnessPython -PathType Leaf)) { throw 'Python is missing. Extract the complete Windows package or install the source dependencies.' }
[IO.Directory]::CreateDirectory($harnessData) | Out-Null
$lockPath = Join-Path $harnessData 'server-process.lock'
$processFile = Join-Path $harnessData 'server-process.json'
$lock = [IO.File]::Open($lockPath, [IO.FileMode]::OpenOrCreate, [IO.FileAccess]::ReadWrite, [IO.FileShare]::None)
try {
    if (Test-Path -LiteralPath $processFile) {
        $saved = Get-Content -LiteralPath $processFile -Raw | ConvertFrom-Json
        $existing = Get-Process -Id $saved.pid -ErrorAction SilentlyContinue
        if ($existing) {
            if ($existing.StartTime.ToUniversalTime().Ticks -ne [long]$saved.start_ticks -or $existing.Path -ne $saved.executable) {
                throw 'The saved process identity no longer matches. No process was changed. Preserve and inspect server-process.json.'
            }
            Write-Output "An owned server process already exists: PID $($saved.pid), http://127.0.0.1:$($saved.port)/. Use status to confirm readiness."
            if ($OpenBrowser) { Start-Process "http://127.0.0.1:$($saved.port)/" }
            return
        }
        # Preserve the previous identity, including an unexpected prior exit.
        $oldRecord = Join-Path $harnessData ('server-process.previous-' + [guid]::NewGuid().ToString('N') + '.json')
        [IO.File]::Copy($processFile, $oldRecord, $false)
    }
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
