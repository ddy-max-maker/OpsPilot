# OpsPilot Next D2/D3 demo fault injection/recovery (real container/service operations, no mocks)
#
# Usage (PowerShell on host with docker compose):
#   .\scripts\demo\faults.ps1 -Action inject  -Scenario S1   # Redis unreachable
#   .\scripts\demo\faults.ps1 -Action recover -Scenario S1
#   .\scripts\demo\faults.ps1 -Action inject  -Scenario S2   # container OOM exit
#   .\scripts\demo\faults.ps1 -Action recover -Scenario S2
#   .\scripts\demo\faults.ps1 -Action inject  -Scenario S4   # gateway upstream down (observed 504)
#   .\scripts\demo\faults.ps1 -Action recover -Scenario S4
#   .\scripts\demo\faults.ps1 -Action inject  -Scenario J1   # Java: business endpoint latency (real sleep)
#   .\scripts\demo\faults.ps1 -Action recover -Scenario J1
#   .\scripts\demo\faults.ps1 -Action inject  -Scenario J2   # Java: 500 error responses
#   .\scripts\demo\faults.ps1 -Action recover -Scenario J2
#   .\scripts\demo\faults.ps1 -Action inject  -Scenario J3   # Java: dependency down -> 503
#   .\scripts\demo\faults.ps1 -Action recover -Scenario J3
#   .\scripts\demo\faults.ps1 -Action inject  -Scenario M1   # MySQL: connection load + slow queries
#   .\scripts\demo\faults.ps1 -Action recover -Scenario M1
#   .\scripts\demo\faults.ps1 -Action status
param(
    [Parameter(Mandatory = $false)]
    [ValidateSet("inject", "recover", "status")]
    [string]$Action,

    [Parameter(Mandatory = $false)]
    [ValidateSet("S1", "S2", "S4", "J1", "J2", "J3", "M1")]
    [string]$Scenario
)

$ErrorActionPreference = "Stop"
$projectRoot = Split-Path -Parent (Split-Path -Parent $PSScriptRoot)

function Invoke-Status {
    Write-Host "== demo environment =="
    Write-Host "-- demo-gateway /health (expect 5xx after S4; observed 504):"
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
                Write-Host "[S4] injecting: stop demo-app (gateway upstream down -> real 5xx, observed 504)..."
                docker stop opspilot-demo-app | Out-Null
                Start-Sleep -Seconds 1
                Write-Host "[S4] done. GET http://localhost:8081/health should return 5xx (observed 504)."
            }
            "J1" {
                Write-Host "[J1] injecting: Java business endpoints sleep 8s (real latency) + traffic loop..."
                Invoke-RestMethod -Method Post -Uri "http://localhost:8082/__faults" `
                    -ContentType "application/json" -Body '{"delayMs": 8000, "errorRate": 0, "dependencyUrl": ""}' | Out-Null
                docker exec -d opspilot-demo-java sh -c 'while true; do wget -qO- http://localhost:8080/api/orders/$((RANDOM % 10)) >/dev/null 2>&1; sleep 1; done # TRAFFIC'
                Start-Sleep -Seconds 3
                Write-Host "[J1] done. /api/slow takes ~8s; traffic loop generating real requests."
            }
            "J2" {
                Write-Host "[J2] injecting: Java /api/orders returns real 500s (error_rate=100) + traffic loop..."
                Invoke-RestMethod -Method Post -Uri "http://localhost:8082/__faults" `
                    -ContentType "application/json" -Body '{"delayMs": 0, "errorRate": 100, "dependencyUrl": ""}' | Out-Null
                docker exec -d opspilot-demo-java sh -c 'while true; do wget -qO- http://localhost:8080/api/orders/$((RANDOM % 10)) >/dev/null 2>&1; sleep 1; done # TRAFFIC'
                Start-Sleep -Seconds 8
                Write-Host "[J2] done. /api/orders returns 500 continuously; metrics/logs capture real failures."
            }
            "J3" {
                Write-Host "[J3] injecting: Java dependency = demo-app, stop demo-app (real 503) + traffic loop..."
                Invoke-RestMethod -Method Post -Uri "http://localhost:8082/__faults" `
                    -ContentType "application/json" -Body '{"delayMs": 0, "errorRate": 0, "dependencyUrl": "http://demo-app:8080/health"}' | Out-Null
                docker stop opspilot-demo-app | Out-Null
                docker exec -d opspilot-demo-java sh -c 'while true; do wget -qO- http://localhost:8080/api/orders/$((RANDOM % 10)) >/dev/null 2>&1; sleep 2; done # TRAFFIC'
                Start-Sleep -Seconds 8
                Write-Host "[J3] done. /api/orders returns 503 dependency_unavailable continuously."
            }
            "M1" {
                Write-Host "[M1] injecting: MySQL connection load (40 sleeping sessions) + slow digest queries..."
                docker cp demo/mysql_load.sh opspilot-demo-mysql:/tmp/mysql_load.sh | Out-Null
                docker exec -d opspilot-demo-mysql sh /tmp/mysql_load.sh inject 40
                Start-Sleep -Seconds 6
                Write-Host "[M1] done. Threads_connected ~ 40+, slow digest populated."
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
            "J1" {
                Write-Host "[J1] recovering: reset Java fault config, stop traffic loop..."
                docker exec opspilot-demo-java sh -c "pkill -f TRAFFIC" 2>$null | Out-Null
                Invoke-RestMethod -Method Post -Uri "http://localhost:8082/__faults" `
                    -ContentType "application/json" -Body '{"delayMs": 0}' | Out-Null
            }
            "J2" {
                Write-Host "[J2] recovering: reset Java fault config, stop traffic loop..."
                docker exec opspilot-demo-java sh -c "pkill -f TRAFFIC" 2>$null | Out-Null
                Invoke-RestMethod -Method Post -Uri "http://localhost:8082/__faults" `
                    -ContentType "application/json" -Body '{"errorRate": 0}' | Out-Null
            }
            "J3" {
                Write-Host "[J3] recovering: start demo-app, reset dependency, stop traffic loop..."
                docker start opspilot-demo-app | Out-Null
                docker exec opspilot-demo-java sh -c "pkill -f TRAFFIC" 2>$null | Out-Null
                Invoke-RestMethod -Method Post -Uri "http://localhost:8082/__faults" `
                    -ContentType "application/json" -Body '{"dependencyUrl": ""}' | Out-Null
            }
            "M1" {
                Write-Host "[M1] recovering: kill MySQL sleeping sessions..."
                docker exec opspilot-demo-mysql sh /tmp/mysql_load.sh recover 2>$null | Out-Null
            }
        }
        Start-Sleep -Seconds 2
        Write-Host ""
        Invoke-Status
    }
}
