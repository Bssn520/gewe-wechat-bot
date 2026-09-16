#!/bin/sh
# 容器入口：按角色分发命令。
#
# 迁移**不**在这里无条件执行。app 与 worker 各自启动时都跑一遍 `aerich upgrade`
# 是并发竞态，也把「进程启动」和「schema 变更」耦合在一起。改用独立的 migrate
# 角色，由编排层先跑一次、成功后再拉起 app / worker（见 docker-compose.yml）。
#
# 用法：
#   ./docker-entrypoint.sh migrate   # 应用迁移后退出
#   ./docker-entrypoint.sh app       # gunicorn + UvicornWorker（默认）
#   ./docker-entrypoint.sh worker    # 回复管道 worker（单实例，见下）
set -e

case "${1:-app}" in
  migrate)
    echo "running migrations (aerich upgrade)..."
    exec aerich upgrade
    ;;

  app)
    # --timeout 是 worker 心跳：event loop 冻住才杀进程，不是单请求时长上限。
    exec gunicorn app.main:app \
      --worker-class uvicorn.workers.UvicornWorker \
      --workers "${WEB_CONCURRENCY:-2}" \
      --bind "0.0.0.0:${PORT:-8000}" \
      --timeout "${GUNICORN_TIMEOUT:-60}" \
      --graceful-timeout 30 \
      --keep-alive 5 \
      --access-logfile /dev/null
    ;;

  worker)
    # 单实例角色。app/workers/reply.py 启动时抢 PG advisory lock，抢不到即 exit(1)。
    # 禁止多副本、禁止滚动更新（旧实例未退时新实例拿不到锁）。
    exec python -m app.workers.reply
    ;;

  *)
    # 逃生舱：允许直接跑任意命令（如 `docker run <image> python -c ...`）
    exec "$@"
    ;;
esac
