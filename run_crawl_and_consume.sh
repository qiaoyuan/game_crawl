#!/bin/bash

LOCK_FILE="/tmp/game_crawl.lock"

LOG_DIR="/www/wwwroot/game_crawl/logs"
LOG_FILE="$LOG_DIR/crawl.log"
WORKER_COUNT=2

mkdir -p "$LOG_DIR" || exit 1

log() {
    echo "$(date '+%Y-%m-%d %H:%M:%S') $*" >> "$LOG_FILE"
}

# 整个脚本流程共用同一把锁
exec 9>"$LOCK_FILE"
if ! /usr/bin/flock -n 9; then
    log "[WARN] 上一轮竞品任务仍在执行，跳过本轮，避免重复启动爬取进程"
    exit 0
fi

# 两个 worker 继承锁描述符。意外退出时也要等存活的 worker 退出后释放锁。
trap 'exec 9>&-' EXIT

log "开始执行爬虫任务，进程数: $WORKER_COUNT"

cd /www/wwwroot/game_crawl || {
    log "进入爬虫目录失败"
    exit 1
}

crawl_started_at=$SECONDS
worker_pids=()
for ((worker_index=0; worker_index<WORKER_COUNT; worker_index++)); do
    worker_log="$LOG_DIR/crawl_worker_${worker_index}.log"
    /usr/bin/xvfb-run -a -s "-screen 0 1280x800x24" \
    /usr/bin/env \
    APP_ENV=prod \
    BROWSER_CHANNEL="" \
    BROWSER_HEADLESS=0 \
    /www/wwwroot/game_crawl/venv/bin/python -u \
    -m tools.crawl_from_db \
    --worker-count "$WORKER_COUNT" --worker-index "$worker_index" \
    >> "$worker_log" 2>&1 &
    worker_pids+=("$!")
    log "启动 worker=${worker_index}，PID=$!，日志: $worker_log"
done

# 即使一个 worker 失败，也必须等待另一个结束，避免下一轮重叠。
crawl_exit=0
for ((worker_index=0; worker_index<WORKER_COUNT; worker_index++)); do
    wait "${worker_pids[$worker_index]}"
    worker_exit=$?
    log "worker=$worker_index 完成，退出码: $worker_exit"
    if [ "$worker_exit" -ne 0 ]; then
        crawl_exit=$worker_exit
    fi
done

elapsed=$((SECONDS - crawl_started_at))

if [ "$crawl_exit" -ne 0 ]; then
    log "爬虫任务失败，退出码: ${crawl_exit}，本轮耗时: ${elapsed} 秒"
    exit "$crawl_exit"
fi

log "爬虫任务完成，本轮耗时: ${elapsed} 秒"
exit 0
