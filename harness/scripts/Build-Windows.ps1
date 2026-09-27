[CmdletBinding()]
param([Parameter(Mandatory=$true)][string]$Destination, [string]$Uv = 'uv')
$ErrorActionPreference = 'Stop'
$oifSource = [IO.Path]::GetFullPath((Join-Path $PSScriptRoot '..'))
$oifTarget = [IO.Path]::GetFullPath($Destination)
if (Test-Path -LiteralPath $oifTarget) { throw 'Destination must not exist. Retain any previous build for inspection.' }
if ($oifTarget.StartsWith($oifSource.TrimEnd('\') + '\', [StringComparison]::OrdinalIgnoreCase)) { throw 'Build outside the source directory.' }
if (-not (Test-Path -LiteralPath ([IO.Path]::GetDirectoryName($oifTarget)) -PathType Container)) { throw 'Destination parent must exist.' }
New-Item -ItemType Directory -Path $oifTarget | Out-Null
foreach ($oifName in @('src','policy','scripts','tests','docs','examples')) {
    Copy-Item -LiteralPath (Join-Path $oifSource $oifName) -Destination $oifTarget -Recurse
}
foreach ($oifName in @('pyproject.toml','uv.lock','requirements-windows.txt','README.md','LICENSE','NOTICE','THIRD-PARTY-NOTICES.md')) {
    Copy-Item -LiteralPath (Join-Path $oifSource $oifName) -Destination $oifTarget
}
New-Item -ItemType Directory -Path (Join-Path $oifTarget 'desktop') | Out-Null
Copy-Item -LiteralPath (Join-Path $oifSource 'desktop\README.md') -Destination (Join-Path $oifTarget 'desktop\README.md')
# The cache is inside this new build only. No registry, PATH or global Python change.
$oifCache = Join-Path $oifTarget '.build-python'
& $Uv python install 3.12.14 --install-dir $oifCache --no-bin --no-registry
if ($LASTEXITCODE -ne 0) { throw 'Python download failed; partial build retained.' }
$oifRuntime = Join-Path $oifCache 'cpython-3.12.14-windows-x86_64-none'
Copy-Item -LiteralPath $oifRuntime -Destination (Join-Path $oifTarget 'python') -Recurse
$oifPython = Join-Path $oifTarget 'python\python.exe'
# This is the fresh application-owned copy, not a system interpreter.
& $Uv pip install --python $oifPython --break-system-packages --require-hashes --no-deps --link-mode copy -r (Join-Path $oifTarget 'requirements-windows.txt')
if ($LASTEXITCODE -ne 0) { throw 'Pinned dependency installation failed; partial build retained.' }
& (Join-Path $oifTarget 'scripts\Build-Desktop.ps1')
if ($LASTEXITCODE -ne 0) { throw 'Desktop build failed; partial build retained.' }
# Only generated caches under the verified new build are removed; never user data.
foreach ($oifCacheName in @('.build-python','.work')) {
    $oifCachePath = [IO.Path]::GetFullPath((Join-Path $oifTarget $oifCacheName))
    if (-not $oifCachePath.StartsWith($oifTarget.TrimEnd('\') + '\', [StringComparison]::OrdinalIgnoreCase)) { throw 'Invalid cache location.' }
    if (Test-Path -LiteralPath $oifCachePath) { Remove-Item -LiteralPath $oifCachePath -Recurse -Force }
}
Write-Output "Windows app built at $oifTarget. Open OIF.exe. Keep the complete folder together."
