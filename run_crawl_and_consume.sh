#!/bin/bash

# 第一个任务：爬虫
echo "$(date '+%Y-%m-%d %H:%M:%S') 开始执行爬虫任务" >> /www/wwwroot/game_crawl/logs/crawl.log

cd /www/wwwroot/game_crawl || exit 1
mkdir -p /www/wwwroot/game_crawl/logs

/usr/bin/flock -n /tmp/game_crawl.lock \
/usr/bin/xvfb-run -a -s "-screen 0 1280x800x24" \
/usr/bin/env \
APP_ENV=prod \
BROWSER_CHANNEL= \
BROWSER_HEADLESS=0 \
/www/wwwroot/game_crawl/venv/bin/python \
-m tools.crawl_from_db

# 检查爬虫是否执行成功
if [ $? -eq 0 ]; then
    echo "$(date '+%Y-%m-%d %H:%M:%S') 爬虫任务完成，开始执行消费策略" >> /www/wwwroot/game_crawl/logs/crawl.log
    
    # 第二个任务：PHP消费策略
    cd /www/wwwroot/GamePlatform && /www/server/php/82/bin/php think price:strategy:consume
    
    if [ $? -eq 0 ]; then
        echo "$(date '+%Y-%m-%d %H:%M:%S') 消费策略执行完成" >> /www/wwwroot/game_crawl/logs/crawl.log
    else
        echo "$(date '+%Y-%m-%d %H:%M:%S') 消费策略执行失败，退出码: $?" >> /www/wwwroot/game_crawl/logs/crawl.log
    fi
else
    echo "$(date '+%Y-%m-%d %H:%M:%S') 爬虫任务执行失败，退出码: $?，跳过消费策略" >> /www/wwwroot/game_crawl/logs/crawl.log
fi

echo "$(date '+%Y-%m-%d %H:%M:%S') 所有任务执行完毕" >> /www/wwwroot/game_crawl/logs/crawl.log
