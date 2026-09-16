.PHONY: dev worker lint format test typecheck audit bandit migrate migrate-make precommit \
	env-dev env-test env-prod docker-build docker-up docker-down image-save image-load

# 启动本地开发服务（端口与 .env 的 PORT 对齐，默认 8000）
dev:
	uv run uvicorn app.main:app --reload --host 0.0.0.0 --port 8000

worker:
	uv run python -m app.workers.reply

# 代码检查
lint:
	uv run ruff check .
	uv run ruff format --check .

# 代码格式化
format:
	uv run ruff format .

# 运行测试（覆盖率门槛 85% 写在 pyproject 的 addopts，与 CI 同源）
test:
	uv run pytest -v

# 静态类型检查
typecheck:
	uv run mypy app

# 依赖漏洞审计（与 CI security job 一致）
audit:
	uv export --format requirements.txt -o /tmp/gewe-requirements.txt
	uv run pip-audit --disable-pip --no-deps -r /tmp/gewe-requirements.txt

# Python 代码安全扫描（与 CI security job 一致）
bandit:
	uv run bandit -r app -q -c pyproject.toml

# 数据库迁移
migrate:
	uv run aerich upgrade

# 生成迁移脚本（修改模型后执行）
migrate-make:
	uv run aerich migrate

# 安装 pre-commit 钩子
precommit:
	uv run pre-commit install

# 本机构建镜像（调试用；生产镜像由 CI 构建推送）
# ⚠️ 在 Apple Silicon（arm64）上构建的镜像**无法**在 x86_64 服务器运行，反之亦然。
# 给服务器做离线交付时必须指定目标架构：make docker-build PLATFORM=linux/amd64
# CI 在 GitHub 的 amd64 runner 上构建，默认就是 linux/amd64。
PLATFORM ?=
docker-build:
	docker buildx build $(if $(PLATFORM),--platform $(PLATFORM),) -t gewe-backend:local --load .

# 拉起 app + worker（会先跑一次性 migrate）
docker-up:
	docker compose up -d migrate app worker

# 停止全部服务
docker-down:
	docker compose down

# 离线交付：导出镜像为 tar.gz / 在服务器上导回。
# 用于「服务器访问不了 registry」的场景 —— 全程不依赖服务器联网拉镜像。
#   make docker-build PLATFORM=linux/amd64   # 必须匹配服务器架构
#   make image-save                          # 导出到 dist/gewe-backend-local.tar.gz
#   scp dist/gewe-backend-local.tar.gz <server>:/opt/gewe-backend/
#   服务器：make image-load && IMAGE_NAME=gewe-backend IMAGE_TAG=local \
#           docker compose up -d --pull never migrate app worker
image-save:
	@mkdir -p dist
	docker save gewe-backend:local | gzip -1 > dist/gewe-backend-local.tar.gz
	@echo "架构: $$(docker image inspect gewe-backend:local --format '{{.Architecture}}/{{.Os}}')"
	@ls -lh dist/gewe-backend-local.tar.gz

image-load:
	gunzip -c dist/gewe-backend-local.tar.gz | docker load

# 从存档切换运行中的 .env（不会覆盖已有 .env.dev / .env.test / .env.prod）
env-dev:
	@test -f .env.dev || cp .env.dev.example .env.dev
	cp .env.dev .env
	@echo "now using .env.dev -> .env (APP_ENV should be dev)"

env-test:
	@test -f .env.test || cp .env.test.example .env.test
	cp .env.test .env
	@echo "now using .env.test -> .env (APP_ENV should be test; DB_NAME should be gewe_test)"

env-prod:
	@test -f .env.prod || cp .env.prod.example .env.prod
	cp .env.prod .env
	@echo "now using .env.prod -> .env (APP_ENV should be prod; fill secrets first)"
