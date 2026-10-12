#!/bin/bash

LOG_DIR="/www/wwwroot/game_crawl/logs"
LOG_FILE="$LOG_DIR/crawl.log"
ENV_FILE="/www/wwwroot/game_crawl/.env.prod"

# 系统环境变量优先；未设置时只解析 .env.prod 的 worker 数，不执行配置内容。
WORKER_COUNT=${CRAWL_WORKER_COUNT-}
if [ "${CRAWL_WORKER_COUNT+x}" != x ]; then
    WORKER_COUNT=1
    if [ -f "$ENV_FILE" ]; then
        WORKER_COUNT=$(awk '
            /^[[:space:]]*(export[[:space:]]+)?CRAWL_WORKER_COUNT[[:space:]]*=/ {
                value = $0
                sub(/^[^=]*=[[:space:]]*/, "", value)
                sub(/\r$/, "", value)
                sub(/[[:space:]]+#.*$/, "", value)
                sub(/[[:space:]]*$/, "", value)
                quote = substr(value, 1, 1)
                if ((quote == "\"" || quote == sprintf("%c", 39)) &&
                    substr(value, length(value), 1) == quote) {
                    value = substr(value, 2, length(value) - 2)
                }
                worker_count = value
                found = 1
            }
            END { print found ? worker_count : 1 }
        ' "$ENV_FILE") || exit 2
    fi
fi
if ! [[ "$WORKER_COUNT" =~ ^[1-9][0-9]*$ ]]; then
    echo "CRAWL_WORKER_COUNT 必须为正整数" >&2
    exit 2
fi

mkdir -p "$LOG_DIR" || exit 1

log() {
    echo "$(date '+%Y-%m-%d %H:%M:%S') $*" >> "$LOG_FILE"
}

# 宝塔重复触发时跳过仍在运行的上一轮，避免 worker、日志及显示号冲突。
exec 9>"$LOG_DIR/crawl.lock" || exit 1
/usr/bin/flock -n 9
lock_exit=$?
if [ "$lock_exit" -eq 1 ]; then
    exit 0
elif [ "$lock_exit" -ne 0 ]; then
    log "获取爬虫运行锁失败，退出码: $lock_exit"
    exit "$lock_exit"
fi
log "开始执行爬虫任务，进程数: $WORKER_COUNT"

cd /www/wwwroot/game_crawl || {
    log "进入爬虫目录失败"
    exit 1
}

crawl_started_at=$SECONDS
worker_pids=()
for ((worker_index=0; worker_index<WORKER_COUNT; worker_index++)); do
    worker_log="$LOG_DIR/crawl_worker_${worker_index}.log"
    display_number=$((90 + worker_index))
    xvfb_log="$LOG_DIR/xvfb_worker_${worker_index}.log"
    /usr/bin/xvfb-run -a -n "$display_number" -e "$xvfb_log" -s "-screen 0 1280x800x24" \
    /usr/bin/env \
    APP_ENV=prod \
    BROWSER_CHANNEL="" \
    BROWSER_HEADLESS=0 \
    /www/wwwroot/game_crawl/venv/bin/python -u \
    -m tools.crawl_from_db \
    --worker-count "$WORKER_COUNT" --worker-index "$worker_index" \
    >> "$worker_log" 2>&1 &
    worker_pids+=("$!")
    log "启动 worker=${worker_index}，DISPLAY=自动分配（起始:${display_number}），PID=$!，日志: ${worker_log}，Xvfb日志: ${xvfb_log}"
done

# 即使一个 worker 失败，也等待本轮另一个结束，再汇总退出状态。
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
