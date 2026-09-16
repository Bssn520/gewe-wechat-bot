# ---- Builder ----
# 用 Docker Hub 官方 Python 镜像 + pip 装 uv：构建机不一定能拉 ghcr.io。
FROM python:3.14-slim-bookworm AS builder

# 可选：把 PyPI 指向国内镜像。默认留空 = 走官方源（CI 在境外不受影响）。
# 在 pypi.org 不可达的构建机上构建时传入，例如：
#   docker build --build-arg PIP_INDEX_URL=https://mirrors.aliyun.com/pypi/simple/ \
#                --build-arg UV_INDEX_URL=https://mirrors.aliyun.com/pypi/simple/ .
ARG PIP_INDEX_URL
ARG UV_INDEX_URL
ENV PIP_INDEX_URL=${PIP_INDEX_URL} \
    UV_DEFAULT_INDEX=${UV_INDEX_URL}

WORKDIR /app

RUN pip install --no-cache-dir -U pip uv

# 只先拷依赖清单，命中 Docker 层缓存：改业务代码不会重装依赖
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev --no-install-project

# ---- Runtime ----
FROM python:3.14-slim-bookworm

# 日志不缓冲（docker logs 实时可读）；不写 .pyc（容器内无需字节码缓存）
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

WORKDIR /app

# 只升级已装包（不新增），拿 Debian 安全更新补掉基础镜像 tag 里的已知漏洞。
# 官方 slim tag 相对 Debian 安全源有滞后，不升级会被 Trivy 以 HIGH/CRITICAL 拦下 CI。
# 升级完清掉 apt 索引，避免多出的层留在镜像里。
RUN apt-get update \
    && apt-get -y upgrade \
    && apt-get clean \
    && rm -rf /var/lib/apt/lists/*

# 从 builder 复制已装好的虚拟环境
COPY --from=builder /app/.venv /app/.venv
ENV PATH="/app/.venv/bin:$PATH"

# 运行期只用 .venv。官方镜像自带的 pip/setuptools/wheel 不被任何进程使用，
# 却会被 Trivy 扫出 CVE（setuptools / pip 间接依赖），因此直接删干净。
RUN pip uninstall -y pip setuptools wheel \
    && rm -rf \
        /usr/local/lib/python*/ensurepip \
        /usr/local/lib/python*/site-packages/pip* \
        /usr/local/lib/python*/site-packages/setuptools* \
        /usr/local/lib/python*/site-packages/pkg_resources* \
        /usr/local/lib/python*/site-packages/wheel* \
        /root/.cache/pip

# pyproject.toml 是 aerich 读 [tool.aerich] 配置所必需
COPY pyproject.toml ./
COPY app/ ./app/
COPY migrations/ ./migrations/
COPY docker-entrypoint.sh ./docker-entrypoint.sh
RUN chmod +x ./docker-entrypoint.sh

# 非 root 运行：应用只写 stdout，DB 与上游全走网络，不需要容器内可写目录。
RUN useradd --create-home --uid 10001 appuser \
    && chown -R appuser:appuser /app
USER appuser

EXPOSE 8000

ENTRYPOINT ["./docker-entrypoint.sh"]
