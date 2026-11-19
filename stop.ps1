# Stop the local service listening on port 8799
$ErrorActionPreference = "SilentlyContinue"
$port = 8799
$busy = Get-NetTCPConnection -LocalPort $port -State Listen
if ($busy) {
    foreach ($b in $busy) {
        try {
            Stop-Process -Id $b.OwningProcess -Force
            Write-Host "Stopped service PID=$($b.OwningProcess)"
        } catch {}
    }
} else {
    Write-Host "No service running on port 8799."
}
