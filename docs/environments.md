# 环境隔离与部署

运行时只读 `.env` 和进程环境变量；`APP_ENV` 仅 `dev` | `test` | `prod`。

## 原则

1. **代码不选环境文件。** `Settings` 固定 `env_file=".env"`。进程环境变量优先于文件。
2. **`.env.dev` / `.env.test` / `.env.prod` 只是存档。** 程序不会读它们。切换：`cp .env.dev .env`，或由平台注入变量。
3. **密钥不入库。** `.env`、`.env.dev`、`.env.test`、`.env.prod` 均被 gitignore。仓库里只有 `*.example`。
4. **只用 PostgreSQL，库按环境分开。** 本地虚拟机 `gewe_dev`；测试 `gewe_test`；生产 `gewe_prod`。不要 SQLite。
5. **prod 启动 fail-fast。** `DEBUG=true`、或缺 `DASHSCOPE_API_KEY` / `GEWE_TOKEN` / `WEBHOOK_SECRET` / `DB_PASSWORD` 都会拒绝启动。节点不配 env 名单。
6. **test 不连开发/生产库。** pytest 用 `APP_ENV=test` 和独立 `DB_NAME`（如 `gewe_test`），不要设 `DATABASE_URL`。

## 文件

| 文件 | git | 谁读 |
|------|-----|------|
| `.env.example` | 提交 | 人；字段清单 |
| `.env.dev.example` / `.env.test.example` / `.env.prod.example` | 提交 | 人；各环境骨架 |
| `.env` | 忽略 | **进程唯一文件源** |
| `.env.dev` / `.env.test` / `.env.prod` | 忽略 | 人；`cp` 到 `.env` |

## 本地开发

```bash
cp .env.example .env          # 或 cp .env.dev.example .env.dev && cp .env.dev .env
# 填 DASHSCOPE_* / GEWE_* / DB_*
make dev                      # APP_ENV=dev，连 gewe_dev
```

当前本机 `.env` 应对准虚拟机 Postgres `gewe_dev`。

## 单测

```bash
make test                     # APP_ENV=test，连独立测试库，不连 gewe_dev
```

不要把 pytest 指到 `gewe_dev`。

## 生产

优先平台注入（Docker/K8s/云托管的 Environment），至少：

```
APP_ENV=prod
DEBUG=false
DB_HOST=...
DB_NAME=gewe_prod
DB_USER=...
DB_PASSWORD=...
DASHSCOPE_API_KEY=...
DASHSCOPE_BASE_URL=...      # 生产业务空间自己的兼容地址，不要复用开发 Key
# 模型 id 与生成参数是 app/agno/models/ 常量，不进 env
# 已注册 agent_id：reply（默认）。未列出的号回落 DEFAULT_AGENT_ID；拼错的 id 会让 worker 启动即失败（fail fast）
NODE_AGENTS=
GEWE_BASE_URL=...
GEWE_TOKEN=...
GEWE_ALLOWLIST=...             # 空=白名单关；非空=本号wxid:好友|好友
WEBHOOK_SECRET=...             # 回调鉴权密钥，须与控制台「Header Secret」一致；空=全部 401
```

开发 Key / 开发业务空间 / `gewe_dev` **不要**进生产。百炼 Key 与地域绑定；GeWe Token 与回调 URL 一对一。

## 镜像与部署

同镜像三个角色，靠 command 区分（`docker-entrypoint.sh` 分发）：

| 角色 | 命令 | 副本数 | 说明 |
|------|------|--------|------|
| `migrate` | `aerich upgrade` | 一次性 | 跑完即退；app / worker 等它成功后再起 |
| `app` | gunicorn + `UvicornWorker` | 可水平扩 | `WEB_CONCURRENCY` 默认 2 |
| `worker` | `python -m app.workers.reply` | **必须 1** | 见下方红线 |

**迁移不放进应用启动流程。** app 与 worker 各自启动都跑一遍 `aerich upgrade` 会并发抢跑，也让 schema 变更和进程启动耦合。改用独立 `migrate` 角色，由编排层先跑一次。

**worker 必须单实例。** 进程内有内存限速桶，两个 worker = 两套桶 = 速率翻倍，直接踩风控。`app/workers/reply.py` 启动时抢 PG advisory lock（`88440101, 1`），抢不到即 `exit(1)`。因此：

- 禁止 `docker compose up --scale worker=2`；
- 禁止滚动更新（旧实例未退、新实例已起时新实例拿不到锁）；
- 升级 worker 用「先 stop 再 up」：`docker compose stop worker && docker compose up -d worker`。

详见 `docs/architecture.md` §4.3。

### 镜像仓库：GHCR（当前方案）

用 GitHub Container Registry，**零配置、免费**，仓库已在 GitHub 上直接可用。

CI 的 login 步骤在未配置 `REGISTRY_USERNAME` / `REGISTRY_PASSWORD` 时，自动回退到 GHCR + 内置 `GITHUB_TOKEN`，所以**什么都不用配**。

**镜像只在 `main` 推送时发布**（线上只跑 main，dev 没有部署环境，推上去没人消费）。推 `main` 后产出：

```text
ghcr.io/bssn520/gewe-backend:main      # 分支名
ghcr.io/bssn520/gewe-backend:latest
ghcr.io/bssn520/gewe-backend:sha-af0ed6c   # 不可变，回滚用
```

`dev` push 与 PR 都会跑完整 CI（含构建镜像 + Trivy 扫描），但**不推送**——目的是尽早发现坏构建，而不是攒到合 main 才发现。

> 路径必须全小写（GHCR 限制），仓库名 `Company-Gewe-AutoReply` 与镜像名 `gewe-backend` 不必一致。

**包是私有的**：镜像继承仓库可见性。仓库是 private，所以包也是 private，服务器拉取前必须登录。

**服务器首次拉取前要登录 GHCR**。`GITHUB_TOKEN` 只在 Actions 内有效，服务器上需要 **PAT**：

1. GitHub → Settings → Developer settings → Personal access tokens → 生成 token，勾选 **`read:packages`**（够用；不要给它 repo 写权限）。
2. 在 ECS 上登录（`--password-stdin` 避免 token 进 shell history）：

```bash
echo "$GITHUB_PAT" | docker login ghcr.io -u Bssn520 --password-stdin
```

登录凭据会存在 `~/.docker/config.json`，之后 `docker compose pull` 不需要重复登录。

**风险与注意**：

- `ghcr.io` 从国内 ECS 拉取是跨境链路，速度和稳定性不如国内 registry。镜像约 270MB，层缓存命中后的增量拉取很快，但首次或基础镜像层变动时会慢。若明显拖慢发布，见文末「改用其他 registry」。
- `docker-compose.yml` 的 `pull_policy: always` 意味着**每次启动都尝试拉取**。未登录 GHCR 会直接报 `denied`；此时要么先登录，要么临时用 `--pull never`（本地已有镜像）。
- 目前 GHCR 的容器镜像存储与带宽**免费**，GitHub 承诺变更前至少提前一个月通知。若将来开始计费，再评估迁到阿里云 ACR。

### 服务器访问不了 GitHub / ghcr.io 怎么办

国内 ECS 拉 `ghcr.io` 是跨境链路，慢或不稳定很常见。分三种情况处理。

**先分清「访问不了什么」**，三者互相独立：

| 访问不了 | 影响 | 解法 |
|----------|------|------|
| `github.com`（网站/Git） | 服务器上 `git pull` 不行 | 代码本来就不用放服务器；用下面的离线交付 |
| `ghcr.io`（镜像仓库） | `docker compose up` 拉不到镜像 | **离线交付**（推荐） |
| `docker.io`（Docker Hub） | 「服务器本地构建」拉不到基础镜像 | 同上；本地构建也要基础镜像 |

**注意：CI 不受影响。** CI 跑在 GitHub Actions 上，它与 ghcr.io 之间是内网，推送很快。问题只出在服务器**拉取**这一端。

#### 方案 1：离线交付（推荐，完全不依赖服务器联网）

镜像在有网的地方构建（本机或 CI），导出成 tar，传过去 `docker load`。全程不需要服务器访问任何 registry。

```bash
# ① 构建（⚠️ 必须指定服务器架构，见下方「架构必须匹配」）
make docker-build PLATFORM=linux/amd64    # 服务器是 x86_64 时；Apple Silicon 本机默认是 arm64
make image-save                           # 导出 dist/gewe-backend-local.tar.gz（约 79MB）

# ② 传到服务器
scp dist/gewe-backend-local.tar.gz <user>@<ecs>:/opt/gewe-backend/

# ③ 服务器上导回并起服务
cd /opt/gewe-backend       # compose 文件所在目录
make image-load            # 或：gunzip -c gewe-backend-local.tar.gz | docker load
IMAGE_NAME=gewe-backend IMAGE_TAG=local \
  docker compose --profile with-db up -d --pull never migrate app worker
```

> **架构必须匹配**（踩过）。在 Apple Silicon（arm64）上 `docker build` 出来的镜像传到 x86_64 服务器会直接失败：
>
> ```text
> The requested image's platform (linux/arm64) does not match the detected host platform (linux/amd64/v4)
> exec ./docker-entrypoint.sh: exec format error
> ```
>
> 用 `make docker-build PLATFORM=linux/amd64`（等价 `docker buildx build --platform linux/amd64 --load`）。导出前可用
> `docker image inspect gewe-backend:local --format '{{.Architecture}}/{{.Os}}'` 确认。**CI 不受影响**——GitHub 的 runner 本身就是 amd64。

**`--pull never` 是必须的**：`pull_policy: always` 会去 registry 找 `gewe-backend:local` 这个不存在的仓库，直接失败。离线场景一定要显式跳过拉取。

镜像从 CI 产物拿也可以（省去本地构建，且天然是 amd64）：

```bash
docker pull ghcr.io/bssn520/gewe-backend:main   # 在有网的机器上
docker save ghcr.io/bssn520/gewe-backend:main | gzip -1 > gewe-main.tar.gz
```

**代价**：每次发布要手动传一次 tar。若发布频繁，用下面方案 2。

#### 方案 2：转到国内 registry，让服务器就近拉

把镜像放到服务器能高速访问的国内仓库，之后 `docker compose pull` 正常走。CI 与 compose 都按变量取值，**不用改代码**。

- **腾讯云 TCR 个人版**：免费（「不收取费用，可直接开通使用」），只需腾讯云账号实名认证，中国大陆在广州可用、北京等地支持内网访问。`ccr.ccs.tencentyun.com`。
- **阿里云 ACR 个人版**：免费，但要求账号是**个人类型**实名认证且由**主账号**创建（见文末附录）。
- **华为云 SWR**：同类选项。

配法（以 TCR 为例）：在 GitHub 配 `IMAGE_REGISTRY=ccr.ccs.tencentyun.com`、`IMAGE_REPOSITORY=<命名空间>/gewe-backend`，以及 `REGISTRY_USERNAME` / `REGISTRY_PASSWORD` 两个 Secret。CI 会自动推到这里，服务器直接拉。

#### 方案 3：ghcr 第三方加速（不建议用于生产）

社区有南大等机构维护的 ghcr 缓存镜像（如 `ghcr.nju.edu.cn`），把 compose 里的地址替换掉即可用。**但这是第三方公益服务，随时可能停服或变更**，出事时你无法控制。仅适合临时救急，不要写进生产流程。

#### 关于「服务器上本地构建」

用 `docker compose build` 看似绕开 registry，其实**没有解决网络问题**：Dockerfile 要从 `docker.io` 拉 `python:3.13-slim-bookworm`，而国内 2024 年后 Docker Hub 公益镜像大批关停，拉取基础镜像同样可能超时。

只有在服务器能配置到可用的 registry mirror（`/etc/docker/daemon.json` 的 `registry-mirrors`）时，本地构建才可行。要判断，先在服务器上试：

```bash
docker pull python:3.13-slim-bookworm   # 能快速拉下来才考虑本地构建
```

拉不动就走方案 1（离线交付）。

### CI

`.github/workflows/ci.yml` 三个 job：`lint-and-test`（ruff / mypy / pytest，覆盖率门槛 85%）→ `security`（pip-audit / bandit）→ `docker`（构建 → Trivy 扫 HIGH/CRITICAL → **仅 main 推送**）。任一环失败都不出镜像。

触发范围：push 到 `main` / `dev`，以及指向这两个分支的 PR。`dev` 与 PR 跑全部门禁含构建，只是不推镜像。

`lint-and-test` 用 GitHub Actions 的 `postgres:16` service 起真库并先跑 `aerich upgrade`：`tests/conftest.py` 的 `db` fixture 只 `Tortoise.init`、不建表，还要 `TRUNCATE`，因此测试必须跑在已迁移的库上。

### 服务器部署

在**北京 ECS** 上执行：

```bash
# 1) 首次拉取前登录 GHCR（见「镜像仓库」一节，需 read:packages 的 PAT）
echo "$GITHUB_PAT" | docker login ghcr.io -u Bssn520 --password-stdin

# 2) 按「生产」一节注入环境变量；可写 .env，也可由平台注入、不落盘

# 3) 起服务（migrate 先跑，成功后才起 app 与 worker）
docker compose up -d migrate app worker
```

> 服务器拉不到 `ghcr.io` 时（国内常见），改用「服务器访问不了 GitHub / ghcr.io 怎么办」一节的**离线交付**或**国内 registry** 方案。

`docker-compose.yml` 默认镜像即 `ghcr.io/bssn520/gewe-backend:main`，无需传 `IMAGE_NAME`。要临时换标签或镜像：

```bash
IMAGE_TAG=sha-af0ed6c docker compose up -d app worker
```

`docker-compose.yml` 默认的 `migrate` / `app` / `worker` **不含 PostgreSQL**：按本文原则用托管库或独立实例。

#### 单机自建 PostgreSQL（可选）

机器上没有任何现成 PG 时，compose 内置了一个用 **profile 隔离**的 `db` 服务，默认不启动（托管库场景不受影响）：

```bash
# 起库（数据落在 compose 文件同级的 data/pgdata/）
docker compose --profile with-db up -d db

# 之后所有命令都带上 --profile，否则 compose 看不到 db
docker compose --profile with-db up -d migrate app worker
```

`.env` 里对应设 `DB_HOST=db`（compose 内部 DNS）、`DB_USER=gewe`、`DB_NAME=gewe_prod`，`DB_PASSWORD` 与传给 compose 的一致。

**数据用 bind mount 而非具名卷**，统一落在 `data/` 下（`${PGDATA_DIR:-./data/pgdata}`）。具名卷的数据在 `/var/lib/docker/volumes/` 下，名字由 compose 项目名派生，备份和迁移都不好找；绑到已知目录后备份就是打包一个目录。项目名已固定为 `gewe`（compose 顶层 `name:`），避免目录改名导致卷名变化、数据"消失"。

> 应用本身**不写任何本地文件**（日志走 stdout、媒资走对象存储），所以 `data/` 下只有数据库。

小内存机器（如 2G）已在 `db` 的 `command` 里调低 `shared_buffers=64MB`、`max_connections=50`、`work_mem=4MB`。**建议同时加 swap**，否则构建/迁移时容易 OOM：

```bash
fallocate -l 2G /swapfile && chmod 600 /swapfile && mkswap /swapfile && swapon /swapfile
grep -q '^/swapfile' /etc/fstab || echo '/swapfile none swap sw 0 0' >> /etc/fstab
```

**备份**（自建库没有自动备份，单机磁盘故障即丢全部数据）：

```bash
# 逻辑备份（推荐，可跨版本恢复）
docker compose exec -T db pg_dump -U gewe -d gewe_prod | gzip > gewe-$(date +%F).sql.gz
# 或物理备份数据目录（先停库保证一致）
docker compose --profile with-db stop db
tar czf data-$(date +%F).tar.gz data/
docker compose --profile with-db up -d db
```

正式业务建议换托管库（RDS）或至少用 1Panel / cron 配一个每日 `pg_dump`。

#### 服务器上需要哪些文件

**运行期只需要两样东西**，源码不必放在服务器上：

```
/docker/docker-data/gewe/
├── docker-compose.yml     # 编排
├── .env                   # 环境变量（600 权限，含密钥）
├── data/pgdata/           # 数据统一放这里（仅 with-db 自建库时）
└── images/*.tar.gz        # 离线交付时留档的镜像包（导完可删）
```

原因：`app/`、`migrations/`、`docker-entrypoint.sh`、依赖都已打进镜像，`migrate` 角色跑的是镜像内的 `migrations/`。宿主机源码是多余的，且放在服务器上会带来「两边版本不一致」的隐患。查看代码用仓库，改了要重新构建镜像。

> 路径按 `${PGDATA_DIR:-./data/pgdata}` 解析（相对 compose 文件所在目录），所以 `docker compose` 命令要在部署目录下执行。

**升级与回滚**：`main` 标签是可变的，`pull_policy: always` 保证拿到最新构建。回滚用不可变的 SHA 标签：

```bash
IMAGE_TAG=sha-af0ed6c docker compose up -d app worker
```

> 本地镜像 / 离线交付的镜像要加 `--pull never`：`IMAGE_NAME=gewe-backend IMAGE_TAG=local docker compose up -d --pull never app`。

#### 两个 env 陷阱（都踩过）

**① 不要在 env 文件里写行内注释**（`KEY=值  # 注释`）。compose 各版本对行内注释行为不一致：
   - `docker run --env-file` **不剥离**行内注释，值会变成 `值  # 注释`，pydantic 解析直接失败；
   - compose `env_file`（v2.40 实测）把带行内注释的行**整行丢弃**（值为空，可能被当成未设置）；
   - compose v5+（服务器实测）把整行（含注释）当值传入，字符串键如 `GEWE_ALLOWLIST` 会被污染成非空、意外开启白名单，是线上事故根因。
   **唯一安全写法**：值单独一行，注释放在键的上面一行成独立整行，如
   ```bash
   # 空 = 白名单关闭。非空 = 本号wxid:好友|好友,...
   GEWE_ALLOWLIST=
   ```
   模板 `.env*.example` 已按此规范，改模板时请保持一致。

**② `docker compose --env-file X` 不覆盖服务的 `env_file`**。它是给 compose 文件**插值**用的（决定镜像名、端口等），服务容器的环境变量仍来自 compose 里声明的 `env_file: .env`。所以：

```bash
# ❌ 这样起，容器读的还是 .env，不是 test.env
docker compose --env-file test.env up -d app

# ✅ 要改容器环境变量，得改服务定义里的 env_file，或用 -e 显式覆盖
docker compose run --rm -e APP_ENV=test -e DB_NAME=gewe_test migrate
docker run --env-file test.env <image> app
```

**上线前务必确认容器连的是哪个库**：`docker compose exec app env | grep -E 'APP_ENV|DB_NAME'`。误连生产库跑 worker 会真的发消息。

## 附：改用其他 registry

CI 与 compose 都按变量取值，**换 registry 不用改代码**，只改配置。

### 阿里云 ACR（若将来要迁）

**前提**：ACR 个人版要求账号是**个人类型**实名认证，且必须由**主账号**创建。用 RAM 子账号登录控制台时看不到「个人版」是**预期行为**——官方原文：

- 「个人版实例**仅限个人用户使用**。创建个人版实例前，请先进行实名认证，且您的账号**必须实名认证为个人类型**。」
- 「请在**主账户**创建个人版实例，并授予 RAM 用户（子账号）AliyunContainerRegistryFullAccess 或 AliyunContainerRegistryReadOnlyAccess 的权限。」
- 「如果您使用 **RAM 用户**登录容器镜像服务控制台，您需要**先创建个人版实例**，才能看到所有仓库。」

若账号是**企业类型**实名，则个人版不可创建，只能买企业版（经济版约 **45 元/月**、基础版含 Trivy 约 **564 元/月**，以购买页为准）。

**步骤**：

1. 主账号登录 → 容器镜像服务控制台 → 实例列表 → 创建个人版 → 区域选 **华北2（北京）**。地域创建后不能改，一个账号只能有一个个人版实例。
2. 「仓库管理 → 访问凭证」→ 设置固定密码（个人版不支持临时 Token）。
3. 建命名空间（如 `trsb`）与私有仓库（如 `gewe-backend`）。
4. 仓库 Settings → Secrets and variables → Actions 配：

| 类型 | 名称 | 值 |
|------|------|-----|
| Variable | `IMAGE_REGISTRY` | `crpi-xxxx.cn-beijing.personal.cr.aliyuncs.com`（**公网**域名，CI 在境外只能走公网） |
| Variable | `IMAGE_REPOSITORY` | `trsb/gewe-backend` |
| Secret | `REGISTRY_USERNAME` | 主账号全名，或 RAM 用户名去掉 `@xxx.onaliyun.com` |
| Secret | `REGISTRY_PASSWORD` | 固定密码 |

5. 服务器拉取用 **VPC 域名**（`crpi-xxxx-vpc.cn-beijing.personal.cr.aliyuncs.com`，同地域内网免流量费）：

```bash
docker login --username=<登录名> crpi-xxxx-vpc.cn-beijing.personal.cr.aliyuncs.com
IMAGE_NAME=crpi-xxxx-vpc.cn-beijing.personal.cr.aliyuncs.com/trsb/gewe-backend \
IMAGE_TAG=main docker compose up -d migrate app worker
```

**个人版限制（官方原文）**：「**仅限开发测试使用，请勿在生产业务中使用**」「公测限额免费使用，**无 SLA 承诺**」；「**不支持使用公网域名拉取镜像**」（只能 VPC 内网拉，本机不在 VPC 内拉不下来）；共享带宽、并发拉取建议 < 10，否则 `TOOMANYREQUESTS`；**不支持**镜像安全扫描。安全扫描一律在 CI 用 Trivy 做，不依赖 registry 控制台。
