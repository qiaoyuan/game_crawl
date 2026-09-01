#!/bin/bash

LOCK_FILE="/tmp/game_crawl.lock"

LOG_DIR="/www/wwwroot/game_crawl/logs"
LOG_FILE="$LOG_DIR/crawl.log"

mkdir -p "$LOG_DIR" || exit 1

log() {
    echo "$(date '+%Y-%m-%d %H:%M:%S') $*" >> "$LOG_FILE"
}

# 整个脚本流程共用同一把锁
exec 9>"$LOCK_FILE"
if ! /usr/bin/flock -n 9; then
    log "上一轮任务仍在执行，跳过本轮"
    exit 0
fi

# Shell 退出时释放锁
trap '/usr/bin/flock -u 9; exec 9>&-' EXIT

log "开始执行爬虫任务"

cd /www/wwwroot/game_crawl || {
    log "进入爬虫目录失败"
    exit 1
}

/usr/bin/xvfb-run -a -s "-screen 0 1280x800x24" \
/usr/bin/env \
APP_ENV=prod \
BROWSER_CHANNEL="" \
BROWSER_HEADLESS=0 \
/www/wwwroot/game_crawl/venv/bin/python \
-m tools.crawl_from_db

crawl_exit=$?
if [ "$crawl_exit" -ne 0 ]; then
    log "爬虫任务失败，退出码: $crawl_exit"
    exit "$crawl_exit"
fi

log "爬虫任务完成"
exit 0
