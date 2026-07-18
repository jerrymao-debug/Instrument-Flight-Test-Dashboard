$ErrorActionPreference = "Stop"
Set-Location -LiteralPath $PSScriptRoot

$pythonCandidates = @(
    (Get-Command python.exe -ErrorAction SilentlyContinue | Select-Object -ExpandProperty Source -ErrorAction SilentlyContinue),
    (Join-Path $env:USERPROFILE ".cache\codex-runtimes\codex-primary-runtime\dependencies\python\python.exe")
) | Where-Object { $_ -and (Test-Path -LiteralPath $_) } | Select-Object -Unique

$python = $null
foreach ($candidate in $pythonCandidates) {
    try {
        & $candidate -c "import sys; raise SystemExit(0 if sys.version_info >= (3, 10) else 1)" 2>$null
        if ($LASTEXITCODE -eq 0) {
            $python = $candidate
            break
        }
    }
    catch {
        # Continue to the next candidate when a Windows Store alias is broken.
    }
}

if (-not $python) {
    throw "Python 3 was not found. Install Python 3.11 or newer, then run this script again."
}

$runtime = Join-Path $PSScriptRoot ".runtime"
$flaskMarker = Join-Path $runtime "flask\__init__.py"

if (-not (Test-Path -LiteralPath $flaskMarker)) {
    Write-Host "Installing Flask and the AWS libraries for this dashboard..."
    & $python -m pip install --disable-pip-version-check --target $runtime -r requirements.txt
    if ($LASTEXITCODE -ne 0) { throw "Package installation failed." }
}

$env:PYTHONPATH = if ($env:PYTHONPATH) { "$runtime;$env:PYTHONPATH" } else { $runtime }
if (-not $env:DASHBOARD_HOST) { $env:DASHBOARD_HOST = "127.0.0.1" }
if (-not $env:DASHBOARD_PORT) { $env:DASHBOARD_PORT = "5000" }
if (-not $env:AWS_PROFILE) { $env:AWS_PROFILE = "ncode-sso" }

$url = "http://127.0.0.1:$($env:DASHBOARD_PORT)/"
Write-Host "Starting the Instrument Flight Test Dashboard at $url"
Start-Job -ScriptBlock {
    param($browserUrl)
    Start-Sleep -Seconds 2
    Start-Process $browserUrl
} -ArgumentList $url | Out-Null

& $python app.py
