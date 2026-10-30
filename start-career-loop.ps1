$ErrorActionPreference = 'Stop'
Set-Location -LiteralPath $PSScriptRoot
$url = 'http://127.0.0.1:8000'
$listener = Get-NetTCPConnection -LocalPort 8000 -State Listen -ErrorAction SilentlyContinue
if ($listener) {
    try {
        $status = Invoke-RestMethod -Uri "$url/scan/status" -TimeoutSec 5
        if ($null -eq $status.running) { throw 'Unexpected service on port 8000' }
    } catch {
        throw 'Port 8000 is occupied by another service. Close it before opening Career Loop.'
    }
} else {
    $python = & py -3.11 -c 'import sys; print(sys.executable)'
    if ($LASTEXITCODE -ne 0 -or -not (Test-Path -LiteralPath $python)) {
        throw 'Python 3.11 is required. See README.md for setup.'
    }
    $logDirectory = Join-Path $PSScriptRoot 'data\shivani'
    New-Item -ItemType Directory -Force -Path $logDirectory | Out-Null
    $server = Start-Process -FilePath $python -ArgumentList '-u','-X','utf8','run.py','serve','--no-scan' -WorkingDirectory $PSScriptRoot -WindowStyle Hidden -PassThru -RedirectStandardOutput (Join-Path $logDirectory 'server.log') -RedirectStandardError (Join-Path $logDirectory 'server-error.log')
    $ready = $false
    for ($attempt = 0; $attempt -lt 40; $attempt++) {
        if ($server.HasExited) { throw 'Career Loop could not start. See data\shivani\server-error.log.' }
        try {
            $status = Invoke-RestMethod -Uri "$url/scan/status" -TimeoutSec 2
            $ready = $null -ne $status.running
            if ($ready) { break }
        } catch { }
        Start-Sleep -Milliseconds 500
    }
    if (-not $ready) { throw 'Career Loop is still starting. Open http://127.0.0.1:8000 shortly.' }
}
Start-Process $url
