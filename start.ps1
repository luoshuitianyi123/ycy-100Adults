# Start local service (default 127.0.0.1:8799)
$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $root

# Prefer project .venv, then global python
$py = "python"
$candidates = @(
  "$root\.venv\Scripts\python.exe"
)
foreach ($c in $candidates) {
  if (Test-Path $c) { $py = $c; break }
}

# Kill leftover process holding port 8799 to avoid 10048
$port = 8799
$busy = Get-NetTCPConnection -LocalPort $port -State Listen -ErrorAction SilentlyContinue
if ($busy) {
  foreach ($b in $busy) {
    try {
      Stop-Process -Id $b.OwningProcess -Force
      Write-Host "Killed old process on port $port PID=$($b.OwningProcess)"
    } catch {}
  }
  Start-Sleep -Seconds 1
}

# Open the relay page in default browser after 3s
Start-Process powershell -ArgumentList '-NoProfile', '-Command', 'Start-Sleep -Seconds 3; Start-Process "http://127.0.0.1:8799/"'

Write-Host "Using Python: $py"
& $py -m uvicorn backend.main:app --host 127.0.0.1 --port 8799
