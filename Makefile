# OpsPilot Next 常用命令（Windows / Linux / macOS 通用；Windows 也可直接用 scripts/bootstrap_demo.ps1）

VENV_PY := $(shell test -x .venv/bin/python && echo .venv/bin/python || echo .venv/Scripts/python.exe)

.PHONY: help bootstrap up up-demo down ps logs test lint format typecheck eval-39 e2e migrate seed demo-seed knowledge web-build clean

help:
	@echo "=== 交付/演示 ==="
	@echo "make bootstrap   - 一键初始化演示环境（等价 scripts/bootstrap_demo.sh --all）"
	@echo "make up          - 启动核心栈（postgres/api/web/worker/rabbitmq/ops-action/docker-ro-proxy）"
	@echo "make up-demo     - 额外启动演示故障环境（demo profile）"
	@echo "make down        - 停止全部容器（保留数据卷）"
	@echo "=== 数据 ==="
	@echo "make seed        - 播种连接器/演示资产（幂等）"
	@echo "make demo-seed   - 播种演示账号（e2e_admin/operator/approver/viewer）"
	@echo "make knowledge   - 播种 RAG 知识集（8 场景；RAG_MODE!=off 时 Agent 才会用）"
	@echo "=== 质量 ==="
	@echo "make test        - 全量 pytest（需本地 postgres 容器）"
	@echo "make eval-39     - 39 例确定性评估（judge-v2）"
	@echo "make e2e         - 浏览器 E2E（Playwright，需栈已启动）"
	@echo "make lint        - Ruff 检查 + 格式检查"
	@echo "make format      - Ruff 格式化"
	@echo "make typecheck   - mypy"
	@echo "=== 其他 ==="
	@echo "make ps          - 容器状态"
	@echo "make logs        - 跟随 api 日志"
	@echo "make migrate     - 对本机库执行 alembic upgrade head"
	@echo "make clean       - 停止并删除数据卷（彻底重置）"

bootstrap:
	bash scripts/bootstrap_demo.sh --all

up:
	docker compose up -d --build

up-demo:
	docker compose --profile demo up -d

down:
	docker compose down

ps:
	docker compose ps

logs:
	docker compose logs -f api

seed:
	docker compose exec -T api python scripts/seed_demo.py

demo-seed:
	docker compose exec -T api python scripts/seed_e2e_users.py

knowledge:
	docker compose exec -T api python scripts/demo/seed_knowledge.py

test:
	$(VENV_PY) -m pytest -q

eval-39:
	$(VENV_PY) scripts/eval_fake_report.py --out docs/eval/fake_ci_report_judge_v2.json

e2e:
	cd web/console && npx playwright test --reporter=list

lint:
	$(VENV_PY) -m ruff check .
	$(VENV_PY) -m ruff format --check .

format:
	$(VENV_PY) -m ruff format .

typecheck:
	$(VENV_PY) -m mypy app

migrate:
	$(VENV_PY) -m alembic upgrade head

web-build:
	cd web/console && npm ci --no-audit --no-fund && npm run build

clean:
	docker compose down -v
