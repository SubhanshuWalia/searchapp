$ErrorActionPreference = "Stop"

# Search_app deployment configuration
$AppDir       = "E:\Search_app"
$ServiceName  = "Searchapp"
$Branch       = "main"
$VenvPip      = "E:\Search_app\venv\Scripts\pip.exe"
$HealthUrl    = "http://127.0.0.1:8080/health"
$ErrorLog     = "E:\Search_app\logs\err.log"
$HealthRetries = 12
$HealthDelaySeconds = 5

$before = $null
$after = $null
$repoChanged = $false
$serviceStoppedByScript = $false
$deploymentSucceeded = $false

function Invoke-Git {
    param([Parameter(Mandatory = $true)][string[]]$GitArgs)

    $output = & git @GitArgs
    if ($LASTEXITCODE -ne 0) {
        throw "Git command failed: git $($GitArgs -join ' ')"
    }
    return $output
}

function Get-ServiceStatus {
    $output = & nssm status $ServiceName 2>&1
    if ($LASTEXITCODE -ne 0) {
        throw "Could not query NSSM service '$ServiceName': $($output -join ' ')"
    }
    return (($output | Out-String).Trim())
}

function Wait-ServiceStatus {
    param(
        [Parameter(Mandatory = $true)][string]$Expected,
        [int]$TimeoutSeconds = 30
    )

    $timer = [Diagnostics.Stopwatch]::StartNew()
    do {
        $status = Get-ServiceStatus
        if ($status -eq $Expected) {
            return $true
        }
        Start-Sleep -Seconds 1
    } while ($timer.Elapsed.TotalSeconds -lt $TimeoutSeconds)

    return $false
}

function Stop-AppService {
    $status = Get-ServiceStatus
    if ($status -eq "SERVICE_STOPPED") {
        return
    }

    $output = & nssm stop $ServiceName 2>&1
    if ($LASTEXITCODE -ne 0) {
        throw "Could not stop service '$ServiceName': $($output -join ' ')"
    }

    if (-not (Wait-ServiceStatus -Expected "SERVICE_STOPPED" -TimeoutSeconds 30)) {
        throw "Service '$ServiceName' did not stop within 30 seconds."
    }
    $script:serviceStoppedByScript = $true
}

function Start-AppService {
    $output = & nssm start $ServiceName 2>&1
    if ($LASTEXITCODE -ne 0) {
        throw "Could not start service '$ServiceName': $($output -join ' ')"
    }

    if (-not (Wait-ServiceStatus -Expected "SERVICE_RUNNING" -TimeoutSeconds 30)) {
        throw "Service '$ServiceName' did not reach SERVICE_RUNNING within 30 seconds."
    }
}

function Test-AppHealth {
    for ($i = 1; $i -le $HealthRetries; $i++) {
        try {
            $response = Invoke-WebRequest -Uri $HealthUrl -Method Get -TimeoutSec 5 -UseBasicParsing
            if ($response.StatusCode -ge 200 -and $response.StatusCode -lt 300) {
                Write-Host "Health check passed ($HealthUrl)." -ForegroundColor Green
                return $true
            }
        }
        catch {
            Write-Host "Health check $i/$HealthRetries failed: $($_.Exception.Message)"
        }

        if ($i -lt $HealthRetries) {
            Start-Sleep -Seconds $HealthDelaySeconds
        }
    }

    return $false
}

function Show-ErrorLog {
    if (Test-Path -LiteralPath $ErrorLog) {
        Write-Host "`nLast 30 lines from $ErrorLog:" -ForegroundColor Yellow
        Get-Content -LiteralPath $ErrorLog -Tail 30
    }
    else {
        Write-Host "Error log not found: $ErrorLog" -ForegroundColor Yellow
    }
}

try {
    # Preflight checks: do not change the running app unless these pass.
    if (-not (Test-Path -LiteralPath $AppDir -PathType Container)) {
        throw "Application directory not found: $AppDir"
    }
    if (-not (Test-Path -LiteralPath $VenvPip -PathType Leaf)) {
        throw "Virtual-environment pip not found: $VenvPip"
    }
    if (-not (Get-Command git -ErrorAction SilentlyContinue)) {
        throw "Git is not available on PATH."
    }
    if (-not (Get-Command nssm -ErrorAction SilentlyContinue)) {
        throw "NSSM is not available on PATH."
    }

    Set-Location -LiteralPath $AppDir

    $currentBranch = (Invoke-Git -GitArgs @("branch", "--show-current") | Out-String).Trim()
    if ($currentBranch -ne $Branch) {
        throw "Expected branch '$Branch', but repository is on '$currentBranch'. No deployment performed."
    }

    $workingTree = (Invoke-Git -GitArgs @("status", "--porcelain") | Out-String).Trim()
    if ($workingTree) {
        throw "Git working tree is not clean. Commit or remove local changes before deploying:`n$workingTree"
    }

    $null = Invoke-Git -GitArgs @("remote", "get-url", "origin")
    $before = (Invoke-Git -GitArgs @("rev-parse", "HEAD") | Out-String).Trim()

    Write-Host "Fetching origin/$Branch..."
    $null = Invoke-Git -GitArgs @("fetch", "origin", $Branch)
    $after = (Invoke-Git -GitArgs @("rev-parse", "refs/remotes/origin/$Branch") | Out-String).Trim()

    if ($before -eq $after) {
        Write-Host "Already up to date ($($before.Substring(0, 7)))." -ForegroundColor Green
        exit 0
    }

    # Keep the original fast-forward-only behavior: refuse remote rewinds or divergent history.
    & git merge-base --is-ancestor $before $after
    if ($LASTEXITCODE -ne 0) {
        throw "origin/$Branch is not a fast-forward from the current commit. Resolve the history manually; no deployment performed."
    }

    Write-Host "`nChanges to deploy:"
    $null = Invoke-Git -GitArgs @("log", "--oneline", "$before..$after")

    # Confirm the target requirements file exists before stopping the service.
    $requirementsAtTarget = & git cat-file -e "$after`:requirements.txt" 2>$null
    if ($LASTEXITCODE -ne 0) {
        throw "requirements.txt is missing at target commit $($after.Substring(0, 7)). No deployment performed."
    }

    Write-Host "`nStopping service '$ServiceName'..."
    Stop-AppService

    # Move to the exact commit fetched from origin/main, not the configured upstream.
    $null = Invoke-Git -GitArgs @("reset", "--hard", $after)
    $repoChanged = $true

    $requirementsChanged = $false
    & git diff --quiet "$before" "$after" -- requirements.txt
    if ($LASTEXITCODE -eq 1) {
        $requirementsChanged = $true
    }
    elseif ($LASTEXITCODE -ne 0) {
        throw "Could not compare requirements.txt between the old and new commits."
    }

    if ($requirementsChanged) {
        Write-Host "requirements.txt changed; installing dependencies..."
        & $VenvPip install -r (Join-Path $AppDir "requirements.txt")
        if ($LASTEXITCODE -ne 0) {
            throw "Dependency installation failed."
        }
    }
    else {
        Write-Host "requirements.txt unchanged; skipping dependency installation."
    }

    Write-Host "Starting service '$ServiceName'..."
    Start-AppService

    if (-not (Test-AppHealth)) {
        throw "Service started, but the application health check did not pass."
    }

    $deploymentSucceeded = $true
    Write-Host "`nDeployed $($after.Substring(0, 7)). Previous: $($before.Substring(0, 7))." -ForegroundColor Green
}
catch {
    $deployError = $_.Exception.Message
    Write-Host "`nDeployment failed: $deployError" -ForegroundColor Red

    if ($before -and $repoChanged) {
        Write-Host "Attempting rollback to $($before.Substring(0, 7))..." -ForegroundColor Yellow
        try {
            # Stop the new/failed instance before changing its code.
            try {
                Stop-AppService
            }
            catch {
                Write-Host "Warning while stopping service for rollback: $($_.Exception.Message)" -ForegroundColor Yellow
            }

            $null = Invoke-Git -GitArgs @("reset", "--hard", $before)

            # Reinstall the previous requirements if dependency installation may have changed them.
            if ($requirementsChanged) {
                Write-Host "Restoring dependencies from the previous requirements.txt..."
                & $VenvPip install -r (Join-Path $AppDir "requirements.txt")
                if ($LASTEXITCODE -ne 0) {
                    throw "Dependency restoration failed."
                }
            }

            Start-AppService
            if (-not (Test-AppHealth)) {
                throw "Rollback code was restored, but the health check failed."
            }

            Write-Host "Rollback successful; previous release is healthy." -ForegroundColor Green
        }
        catch {
            Write-Host "ROLLBACK FAILED: $($_.Exception.Message)" -ForegroundColor Red
            try {
                Write-Host "Current service status: $(Get-ServiceStatus)"
            }
            catch {
                Write-Host "Could not query service status: $($_.Exception.Message)"
            }
            Show-ErrorLog
        }
    }
    elseif ($serviceStoppedByScript) {
        # Failure before changing the repository: bring the existing release back up.
        try {
            Start-AppService
            if (-not (Test-AppHealth)) {
                Write-Host "Warning: existing release started, but its health check failed." -ForegroundColor Yellow
                Show-ErrorLog
            }
        }
        catch {
            Write-Host "Could not recover the existing service: $($_.Exception.Message)" -ForegroundColor Red
            Show-ErrorLog
        }
    }
    else {
        Show-ErrorLog
    }

    exit 1
}

if (-not $deploymentSucceeded) {
    exit 1
}
