$ErrorActionPreference = "Stop"
Set-Location E:\searchapp

$service = "Searchapp"
$before  = (git rev-parse HEAD).Trim()

git fetch origin
$after = (git rev-parse origin/main).Trim()

if ($before -eq $after) {
    Write-Host "Already up to date ($($before.Substring(0,7)))."
    exit
}

Write-Host "Changes to deploy:"
git log --oneline "$before..$after"

nssm stop $service
git pull --ff-only

# Install packages only if requirements.txt changed
if (git diff --name-only $before $after | Select-String "requirements.txt") {
    Write-Host "requirements.txt changed, installing packages..."
    & E:\searchapp\venv\Scripts\pip.exe install -r requirements.txt
}

nssm start $service
Start-Sleep -Seconds 5
$status = (nssm status $service).Trim()
Write-Host "Service status: $status"

if ($status -ne "SERVICE_RUNNING") {
    Write-Host "Service did not start. Rolling back to $($before.Substring(0,7))..." -ForegroundColor Red
    git reset --hard $before
    nssm start $service
    Start-Sleep -Seconds 5
    Write-Host "After rollback: $((nssm status $service).Trim())"
    Get-Content E:\searchapp\logs\err.log -Tail 20
    exit 1
}

Write-Host "Deployed $($after.Substring(0,7)). Previous: $($before.Substring(0,7))" -ForegroundColor Green
