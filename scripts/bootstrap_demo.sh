#!/usr/bin/env bash
# OpsPilot Next 演示环境一键初始化（Linux / macOS / Windows Git Bash）
#
# 做的事：核心栈启动 → 等 API 就绪 → 幂等播种（连接器/资产/演示账号）
#         → 可选：演示故障环境（demo profile）+ 知识库（RAG 演示）
#
# 用法：
#   ./scripts/bootstrap_demo.sh                 # 核心栈 + 资产/账号
#   ./scripts/bootstrap_demo.sh --with-demo     # 额外启动演示故障环境（demo profile）
#   ./scripts/bootstrap_demo.sh --with-knowledge # 额外播种 RAG 知识集（8 场景）
#   ./scripts/bootstrap_demo.sh --all           # 以上全部
#
# 全流程不需要宿主机 Python / Node：种子脚本在 api 容器内执行。

set -euo pipefail

WITH_DEMO=0
WITH_KNOWLEDGE=0
for arg in "$@"; do
  case "$arg" in
    --with-demo) WITH_DEMO=1 ;;
    --with-knowledge) WITH_KNOWLEDGE=1 ;;
    --all) WITH_DEMO=1; WITH_KNOWLEDGE=1 ;;
    -h|--help) sed -n '2,20p' "$0"; exit 0 ;;
    *) echo "未知参数：$arg（可用：--with-demo / --with-knowledge / --all）" >&2; exit 2 ;;
  esac
done

cd "$(dirname "$0")/.."

if [ ! -f .env ]; then
  echo "[bootstrap] 未找到 .env，从 .env.example 复制（演示默认值，生产请替换密钥）"
  cp .env.example .env
fi

echo "[bootstrap] 1/4 构建并启动核心栈（postgres/api/web/worker/rabbitmq/ops-action/docker-ro-proxy）"
docker compose up -d --build

echo "[bootstrap] 2/4 等待 API ready（首次需等迁移完成）"
# --noproxy '*'：宿主机若配置了 HTTP_PROXY 且 NO_PROXY 不含 127.0.0.1，
# curl 会把本地探测也交给代理并拿到 502，导致就绪判断永远失败
for i in $(seq 1 90); do
  if curl -sf --noproxy '*' http://localhost:8000/api/v1/health/ready >/dev/null 2>&1; then
    echo "[bootstrap] API ready（${i} 次探测）"
    break
  fi
  if [ "$i" -eq 90 ]; then
    echo "[bootstrap] API 180s 内未就绪，请查看：docker compose logs api" >&2
    exit 1
  fi
  sleep 2
done

echo "[bootstrap] 3/4 播种连接器 / 演示资产 / 演示账号（幂等）"
docker compose exec -T api python scripts/seed_demo.py
docker compose exec -T api python scripts/seed_e2e_users.py

if [ "$WITH_DEMO" -eq 1 ]; then
  echo "[bootstrap] 启动演示故障环境（demo profile：demo-app / demo-redis / demo-gateway / demo-java / prometheus / jaeger / demo-mysql）"
  docker compose --profile demo up -d
fi

if [ "$WITH_KNOWLEDGE" -eq 1 ]; then
  echo "[bootstrap] 播种 RAG 知识集（8 场景；含 embedding 计算，首次约 1 分钟）"
  docker compose exec -T api python scripts/demo/seed_knowledge.py
fi

echo "[bootstrap] 4/4 完成"
cat <<'EOF'

  控制台：   http://localhost:8080
  API 文档： http://localhost:8000/docs
  演示账号（.env 未改时）：
    e2e_admin    / E2ePassw0rd!2026   （admin，可复核发布、审批执行）
    e2e_operator / E2ePassw0rd!2026   （operator，可建事件/发起诊断/建方案）
    e2e_approver / E2ePassw0rd!2026   （approver，四眼审批的另一方）
    e2e_viewer   / E2ePassw0rd!2026   （viewer，只读）
  初始管理员：.env 的 BOOTSTRAP_ADMIN_USERNAME / BOOTSTRAP_ADMIN_PASSWORD（首登强制改密）

  下一步：按 docs/DEMO_SCRIPT.md 走三条演示路线。
EOF
