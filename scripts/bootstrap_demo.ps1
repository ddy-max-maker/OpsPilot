# OpsPilot Next 演示环境一键初始化（Windows PowerShell 5.1+ / PowerShell 7）
#
# 做的事：核心栈启动 → 等 API 就绪 → 幂等播种（连接器/资产/演示账号）
#         → 可选：演示故障环境（demo profile）+ 知识库（RAG 演示）
#
# 用法（在仓库根目录）：
#   powershell -ExecutionPolicy Bypass -File scripts/bootstrap_demo.ps1
#   powershell -ExecutionPolicy Bypass -File scripts/bootstrap_demo.ps1 -WithDemo
#   powershell -ExecutionPolicy Bypass -File scripts/bootstrap_demo.ps1 -All
#
# 全流程不需要宿主机 Python / Node：种子脚本在 api 容器内执行。

[CmdletBinding()]
param(
    [switch]$WithDemo,
    [switch]$WithKnowledge,
    [switch]$All
)

$ErrorActionPreference = "Stop"
if ($All) { $WithDemo = $true; $WithKnowledge = $true }

Set-Location (Join-Path $PSScriptRoot "..")

if (-not (Test-Path ".env")) {
    Write-Host "[bootstrap] 未找到 .env，从 .env.example 复制（演示默认值，生产请替换密钥）"
    Copy-Item ".env.example" ".env"
}

Write-Host "[bootstrap] 1/4 构建并启动核心栈（postgres/api/web/worker/rabbitmq/ops-action/docker-ro-proxy）"
docker compose up -d --build
if ($LASTEXITCODE -ne 0) { throw "docker compose up 失败" }

Write-Host "[bootstrap] 2/4 等待 API ready（首次需等迁移完成）"
$ready = $false
for ($i = 1; $i -le 90; $i++) {
    try {
        $resp = Invoke-WebRequest -UseBasicParsing -TimeoutSec 3 `
            -Uri "http://localhost:8000/api/v1/health/ready" -ErrorAction Stop
        if ($resp.StatusCode -eq 200) { $ready = $true; Write-Host "[bootstrap] API ready（$i 次探测）"; break }
    } catch { Start-Sleep -Seconds 2 }
}
if (-not $ready) { throw "API 180s 内未就绪，请查看：docker compose logs api" }

Write-Host "[bootstrap] 3/4 播种连接器 / 演示资产 / 演示账号（幂等）"
docker compose exec -T api python scripts/seed_demo.py
if ($LASTEXITCODE -ne 0) { throw "seed_demo.py 失败" }
docker compose exec -T api python scripts/seed_e2e_users.py
if ($LASTEXITCODE -ne 0) { throw "seed_e2e_users.py 失败" }

if ($WithDemo) {
    Write-Host "[bootstrap] 启动演示故障环境（demo profile）"
    docker compose --profile demo up -d
    if ($LASTEXITCODE -ne 0) { throw "demo profile 启动失败" }
}

if ($WithKnowledge) {
    Write-Host "[bootstrap] 播种 RAG 知识集（8 场景；含 embedding 计算，首次约 1 分钟）"
    docker compose exec -T api python scripts/demo/seed_knowledge.py
    if ($LASTEXITCODE -ne 0) { throw "seed_knowledge.py 失败" }
}

Write-Host "[bootstrap] 4/4 完成"
@"

  控制台：   http://localhost:8080
  API 文档： http://localhost:8000/docs
  演示账号（.env 未改时）：
    e2e_admin    / E2ePassw0rd!2026   （admin，可复核发布、审批执行）
    e2e_operator / E2ePassw0rd!2026   （operator，可建事件/发起诊断/建方案）
    e2e_approver / E2ePassw0rd!2026   （approver，四眼审批的另一方）
    e2e_viewer   / E2ePassw0rd!2026   （viewer，只读）
  初始管理员：.env 的 BOOTSTRAP_ADMIN_USERNAME / BOOTSTRAP_ADMIN_PASSWORD（首登强制改密）

  下一步：按 docs/DEMO_SCRIPT.md 走三条演示路线。
"@ | Write-Host
