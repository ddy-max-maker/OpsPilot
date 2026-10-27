# OpsPilot Next D2 demo fault injection/recovery (real container operations, no mocks)
#
# Usage (PowerShell on host with docker compose):
#   .\scripts\demo\faults.ps1 -Action inject  -Scenario S1   # Redis unreachable
#   .\scripts\demo\faults.ps1 -Action recover -Scenario S1
#   .\scripts\demo\faults.ps1 -Action inject  -Scenario S2   # container OOM exit
#   .\scripts\demo\faults.ps1 -Action recover -Scenario S2
#   .\scripts\demo\faults.ps1 -Action inject  -Scenario S4   # gateway upstream 502
#   .\scripts\demo\faults.ps1 -Action recover -Scenario S4
#   .\scripts\demo\faults.ps1 -Action status
param(
    [Parameter(Mandatory = $false)]
    [ValidateSet("inject", "recover", "status")]
    [string]$Action,

    [Parameter(Mandatory = $false)]
    [ValidateSet("S1", "S2", "S4")]
    [string]$Scenario
)

$ErrorActionPreference = "Stop"
$projectRoot = Split-Path -Parent (Split-Path -Parent $PSScriptRoot)

function Invoke-Status {
    Write-Host "== demo environment =="
    Write-Host "-- demo-gateway /health (expect 502 after S4):"
    try {
        $g = Invoke-WebRequest -Uri "http://localhost:8081/health" -UseBasicParsing
        Write-Host "   HTTP $($g.StatusCode) $($g.Content)"
    } catch {
        $code = $null
        if ($_.Exception.Response) { $code = [int]$_.Exception.Response.StatusCode }
        Write-Host "   HTTP $code (expected when upstream is down)"
    }
    Write-Host "-- demo-app container (expect exited after S4):"
    $appState = docker inspect -f "{{.State.Status}}" opspilot-demo-app 2>$null
    Write-Host "   state: $appState"
    Write-Host "-- demo-redis (expect exited after S1):"
    docker ps -a --filter "name=opspilot-demo-redis" --format "   {{.Names}} {{.Status}}"
    Write-Host "-- demo-crasher (expect Exited(137) OOMKilled after S2):"
    $crasher = docker ps -a --filter "name=opspilot-demo-crasher" --format "   OOMKilled check: {{.Names}} {{.Status}}"
    if ($crasher) { Write-Host $crasher } else { Write-Host "   (crasher container not created yet)" }
    Write-Host "-- containers:"
    docker ps -a --filter "name=opspilot-demo" --format "   {{.Names}}  {{.Status}}"
}

switch ($Action) {
    "status" {
        Invoke-Status
    }
    "inject" {
        switch ($Scenario) {
            "S1" {
                Write-Host "[S1] injecting: stop demo-redis (redis unreachable)..."
                docker stop opspilot-demo-redis | Out-Null
                Write-Host "[S1] done."
            }
            "S2" {
                Write-Host "[S2] injecting: start memory-limited crasher, waiting for OOMKilled..."
                Push-Location $projectRoot
                docker compose --profile demo-fault up -d demo-crasher 2>&1 | Out-Null
                Pop-Location
                $state = ""
                for ($i = 0; $i -lt 30; $i++) {
                    Start-Sleep -Seconds 2
                    $state = docker inspect -f "{{.State.Status}} OOMKilled={{.State.OOMKilled}} ExitCode={{.State.ExitCode}}" opspilot-demo-crasher 2>$null
                    if ($state -match "exited") { break }
                }
                Write-Host "[S2] done. crasher: $state"
            }
            "S4" {
                Write-Host "[S4] injecting: stop demo-app (gateway upstream down -> real 502)..."
                docker stop opspilot-demo-app | Out-Null
                Start-Sleep -Seconds 1
                Write-Host "[S4] done. GET http://localhost:8081/health should return 502."
            }
        }
        Write-Host ""
        Invoke-Status
    }
    "recover" {
        switch ($Scenario) {
            "S1" {
                Write-Host "[S1] recovering: start demo-redis..."
                docker start opspilot-demo-redis | Out-Null
            }
            "S2" {
                Write-Host "[S2] recovering: remove crasher container..."
                docker rm -f opspilot-demo-crasher 2>$null | Out-Null
            }
            "S4" {
                Write-Host "[S4] recovering: start demo-app..."
                docker start opspilot-demo-app | Out-Null
            }
        }
        Start-Sleep -Seconds 2
        Write-Host ""
        Invoke-Status
    }
}
