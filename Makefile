# OpsPilot Next 常用命令（Windows/PowerShell 用户可直接使用等价命令，见 README）

.PHONY: help up down logs test lint typecheck seed alert migrate

help:
	@echo "make up        - 启动全栈（postgres + api + web）"
	@echo "make down      - 停止全栈"
	@echo "make logs      - 跟随 api 日志"
	@echo "make test      - 运行全部测试（需本地 postgres 容器）"
	@echo "make lint      - Ruff 检查"
	@echo "make typecheck - mypy 检查"
	@echo "make seed      - 写入演示资产"
	@echo "make alert     - 模拟外部告警（ASSET_ID=as_xxx）"
	@echo "make migrate   - 对本地库执行 alembic upgrade head"

up:
	docker compose up -d --build

down:
	docker compose down

logs:
	docker compose logs -f api

test:
	.venv/Scripts/python.exe -m pytest -q

lint:
	.venv/Scripts/ruff.exe check .

typecheck:
	.venv/Scripts/mypy.exe app

seed:
	.venv/Scripts/python.exe scripts/seed.py --password "$(PASSWORD)" --new-password "$(NEW_PASSWORD)"

alert:
	.venv/Scripts/python.exe scripts/simulate_alert.py --asset-id "$(ASSET_ID)" --repeat $(REPEAT)

migrate:
	.venv/Scripts/alembic.exe upgrade head
