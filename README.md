# gewe-backend

GeWe 多微信账号自动回复服务。FastAPI + Tortoise ORM + PostgreSQL。

回调先进 `inbox`，独立 worker 生成回复、写入 `outbox`，再按账号串行经 GeWe 发出。数据库就是队列，没有 Celery / Redis broker。一期无网页端。

```bash
uv sync --group dev
cp .env.example .env
make migrate
make dev
curl http://localhost:8000/api/v1/health
```

回复管道另开进程：

```bash
make worker
```

## 常用命令

```bash
make dev            # API（热重载）
make worker         # 回复管道 worker（必须单实例）
make test           # pytest，覆盖率门槛 85%
make lint           # ruff check + format check
make typecheck      # mypy
make audit          # pip-audit 依赖漏洞审计
make bandit         # bandit SAST
make migrate        # aerich upgrade
```

## 容器与部署

同镜像三个角色，用 command 区分：`migrate`（一次性）/ `app`（gunicorn + UvicornWorker）/ `worker`。

```bash
make docker-build PLATFORM=linux/amd64
docker compose up -d migrate app worker
```

机器上没有现成 PostgreSQL 时：

```bash
docker compose --profile with-db up -d db
docker compose --profile with-db up -d migrate app worker
```

CI（`.github/workflows/ci.yml`）在 push 到 `main` / `dev` 及 PR 时跑 lint / 类型检查 / 测试 / 依赖与代码扫描，构建镜像并用 Trivy 扫 HIGH/CRITICAL；**镜像只在 `main` 推送时发布**到 GHCR。

完整环境变量与部署说明见 [docs/environments.md](docs/environments.md)。

⚠️ **worker 必须单实例**：进程内有内存限速桶，多副本会让实际发送速率翻倍。不要 `--scale worker=2`。详见 [docs/architecture.md](docs/architecture.md)。

## License

[MIT](LICENSE)
