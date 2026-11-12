# 爬虫机性能监控

Linux 独立监控进程读取 /proc 和系统负载，默认每10秒通过内网 MySQL 短连接写 server_monitor 和 server_monitor_history。使用 APP_ENV=prod 加载 .env.prod，系统环境变量优先。DB_HOST、DB_PORT、DB_USER、DB_PASSWORD、DB_NAME 复用爬虫配置。默认机器编号 crawler-<crawl_server>、名称爬虫<编号>，可用 SERVER_MONITOR_CODE、SERVER_MONITOR_NAME 显式覆盖，已有机器保留数据库名称。采集间隔和磁盘路径分别使用 SERVER_MONITOR_INTERVAL 和 SERVER_MONITOR_DISK_PATH。

每个样本在同一事务中登记/锁定机器、去重、更新分钟计数/和/峰值及最新快照。last_report_at 使用数据库时间；重复或落后的采样跳过，过早/未来时间校验与 PHP 查询语义见后台 docs/server-monitor.md。失败时关闭连接，等下一轮新样本；日志记录错误类型和数字错误码。--dry-run 在 Linux 上只采一次，不连接数据库。

首次部署由后台执行 sql/server_monitor.sql，已有旧表可直接使用。检查数据库账号对两表具备 SELECT、INSERT、UPDATE 权限。

### 用 crontab 常驻运行

在爬虫2和爬虫3上分别用爬虫运行账号执行 `crontab -e`，添加一行（默认安装路径，依赖 Linux util-linux 的 flock 和系统 logger）：

```cron
* * * * * cd /www/wwwroot/game_crawl && APP_ENV=prod flock -n -E 0 . ./venv/bin/python -u -m tools.server_monitor 2>&1 | logger -t game-server-monitor
```

cron 每分钟尝试启动一次。flock 对当前项目目录加独占锁，已有进程运行时直接退出；监控进程按 .env.prod 的间隔持续上报，退出或机器重启后在下个 cron 周期恢复。用 `journalctl -t game-server-monitor -n 30` 或系统 syslog 查看日志，后台「服务监控」的上报时间应持续更新。如有旧监控进程，停止后由 cron 拉起新版。修改 .env.prod 后需重启监控进程。

查看「系统管理 → 服务监控」确认最后上报时间持续更新。
