---
name: g2g-crawl-dev
description: 维护本项目 G2G/ELD 数据库任务爬虫的配置、分片、店铺加强、数据快照、PHP 改价通知和 Linux 启动脚本；修改相关源码或排查抓取停滞、价格错误、进程重叠时使用。
---

# 数据库任务爬虫规范

## 入口与职责

- `tools/crawl_from_db.py` 是生产入口，其中 `CrawlSession` 按需管理 worker 的请求和浏览器资源；`g2g/db.py` 负责数据库读写，`g2g/crawl_filter.py` 决定空加强名单时的 Top3 对象。
- Python 抓取 `crawl_target`，保存 `crawl_data`，发送 `crawl_notify`；实际改价由 PHP 后台的 `price:strategy:consume` 执行。Python 不执行竞价或改价。
- 关联后台项目为 `/Users/qiaoyuan/www/php/base_admin`。后台旧手工 `CrawlService` 写 `competitor_product`，不是此生产链路。
- 独立抓取、登录与导出工具另见项目 `.codebuddy/skills/g2g-scraper/SKILL.md`，不要把其 JSON 输出规则套到数据库入口。

## 配置与抓取范围

`g2g/config.py` 根据 `APP_ENV` 读取 `.env` 或 `.env.prod`，已存在的系统环境变量优先。模板是 `.env.example` 和 `.env.prod.example`，模板更新不代表服务器配置已更新。

| 配置 | 默认 | 作用 |
| --- | --- | --- |
| `crawl_server` | 1 | 本机只读取所属服务器 1/2 的任务 |
| `CRAWL_OFFER_LIMIT` | 10 | G2G 当前列表按展示顺序取前 N 条，正整数 |
| `ELD_OFFER_PAGE_SIZE` | 8 | ELD 游戏币第一页的条数上限，正整数 |
| `CRAWL_EXCLUDE_STORES` | 空字符串 | 全局排除多个店铺名/卖家标识，排除价格处理、加强和入库 |
| `CRAWL_WORKER_COUNT` | 1 | 启动脚本的 worker 数，可在 `.env.prod` 中配置，系统环境变量优先 |

`CRAWL_WORKER_COUNT` 由 shell 启动脚本读取：系统环境变量已设置时优先使用，否则只解析生产目录 `.env.prod` 的同名字段；文件或字段缺失时默认 1。支持空白、单/双引号、行尾注释、CRLF 和 `export` 前缀，同名字段取最后一项；配置内容不作为 shell 执行，也不加载其他字段。空值、非整数或小于 1 时在启动前报错。修改配置对下一次启动生效，不调整运行中的 worker；cron 中设置的环境变量同样会覆盖文件。抓取条数控制每个目标的竞品数量，不控制领取的任务数量。

- G2G 金币/游戏币走 `scrape_other_offer_page`；其他分类走通用 `scrape_page`。两条 G2G 卡片路径都应用 G2G 上限，提取后再选择加强对象。达到上限停止滚动等待；未达到时沿用数量稳定后结束的加载逻辑，不向后补齐加强店铺。
- `ELD游戏币` 由 `CrawlSession.scrape_eldorado` 优先调用 `scrape_eldorado_api`，每个 worker 复用独立 Playwright APIRequestContext，带原有英文 User-Agent、语言与 USD/英文偏好 cookie；API 成功不创建 Chromium、BrowserContext 或 Page。无法解析 gameId、请求/JSON 解析失败、非成功响应或无可用结果时，才按需启动 Chromium 并调用 `scrape_eldorado_dom`；每个 DOM 目标使用独立 context 并在结束时关闭，不带 G2G cookie。APIResponse 解析后及时 dispose，worker 结束释放请求、浏览器和 Playwright driver。`scrape_eldorado_page(page, url)` 保留页面调用的 API→DOM 兼容入口，生产轮次不依赖预先创建的页面。
- ELD API 固定 `pageIndex=1`，`pageSize` 使用 ELD 上限；保留商品属性与排序，URL 的分页参数不能覆盖配置。API 返回超过上限也截断；DOM 兜底用同一上限，不滚动加载更多。两个平台的条数独立。全局排除导致的有效空列表仍成功返回，不触发 DOM。
- 不要把 ELD 游戏币的 API 规则描述成所有独立工具或所有分类均已支持。
- `CRAWL_EXCLUDE_STORES` 与其他 Python 配置一样由 `.env`/`.env.prod` 加载，系统环境变量优先；未配置或空值不排除。支持英文/中文逗号、换行及 JSON 数组，复用 identifiers 归一化大小写、HTML 实体和空白，对 seller_id/seller_name 完整匹配，不按子串命中。生产两台服务器需各自配置，上线模板不代表真实环境已同步。
- 先按 G2G/ELD 上限取得当前列表范围，再排除全局名单；G2G 两条卡片路径与 ELD DOM 在 Python 价格/好评率处理之前过滤，ELD API 在识别 user/offer 卖家身份后、报价换算之前跳过。被排除行不进入 Top3/绑定店铺加强，也不进入 crawl_data；加强名单与策略白名单不能覆盖全局排除。过滤后不向上限之外补抓，实际保存和通知条数可能减少。ELD API 本范围全部被排除时成功返回空列表，不因为排除而回退 DOM 或翻页。

## 任务、分片与快照

- `get_pending_targets` 查询启用、未删除、`crawl_server` 相同的目标，按 `MOD(id, worker_count)=worker_index` 分片并按 id 排序。worker index 从 0 开始，必须小于正数 worker count。该分片不保证任务数量或耗时均衡；每个 worker 内仍串行抓取，`iter_scheduled_targets` 调整本轮执行顺序，不增加目标并发或改变分片 SQL。
- 跳过无有效 `game_product_id`、无 URL 的任务。`crawl_interval` 是两次开始的最小间隔（秒），30 表示开始间隔 30 秒，不额外叠加基础 30 秒；0/缺失表示每轮调度执行，首次抓取不等待。Python 在版本递增前记录本轮开始时间，完整保存后将该时间写入 `last_crawl_at`，下次到期时间为上次成功轮次的开始时间加间隔，抓取耗时不追加到间隔中。
- 先排除无产品或 URL 的任务，再依据本轮任务快照计算到期时间。每个目标结束后重新扫描未执行队列，优先选择已经到期的目标；同时到期的目标保持原 id 顺序。未到期目标暂留队列，不在原位置等待并阻塞后面的已到期目标。
- 仅当队列没有已到期目标时，选择最近到期者：剩余不超过 30 秒则异步 sleep，醒来后重新检查到期时间，避免提前执行；最近目标仍超过 30 秒则记录并跳过所有剩余目标。初始剩余超过 30 秒的目标可能在其他目标抓取期间到期，因此不在首次扫描时直接删除。每个目标本轮最多尝试一次，失败不在本轮重新排队。日志区分到期优先、等待和跳过，格式错误的历史时间仍告警后继续抓取。该等待阈值不是整轮耗时上限，仍可能多次等待不同目标。旧版本保存的完成时间无需迁移，首个成功轮次后自然换成开始时间。
- `crawl_type`：0 默认，1 店铺加强；兼容数字字符串、历史 `default/top3`，缺失/空值按 0，未知类型报错。
- 每个目标抓取前递增 `crawl_target.version`，本轮所有 `crawl_data` 带同一 `target_id`、`game_product_id` 和 version。加强失败仍可能已递增版本。
- 成功顺序：保存完整候选 → 更新 `last_crawl_at` → 插入 `crawl_notify(status=0)`，通知带目标、准确 version、实际保存条数。成功零条也可发零条通知。PHP 按通知 version 读取快照，不能只读最新版本。
- 这些数据库步骤当前分别提交，不能宣称数据保存、时间更新和通知是一个原子事务。目标异常会记录失败并继续后续目标；有失败时本轮返回非零。

## 店铺加强选择

两种类型都保存全局排除后剩余的全部列表候选，不按策略裁剪入库。只有 G2G 金币/游戏币的类型 1 读取店铺详情。

- `enhance_stores` 为 `varchar(2048) NOT NULL DEFAULT ''`，支持多个名字按英文/中文逗号、换行等分隔；Python 兼容数组和 JSON 数组，将大小写、HTML 实体及空白归一化，对 seller_id 或 seller_name 完整匹配。
- 非空名单直接选择本轮候选中命中的店铺，不查询或考虑改价策略、黑白名单、库存、好评率、价格门槛，也不限制三家。非空名单未命中时不加强，不回退自动 Top3，不向列表上限之外搜索。
- 名单归一化后为空时，读取 `crawl_target.id=price_strategy.crawl_target_id` 的启用未删除策略，通过 `price_strategy_product`、未删除 `game_product` 获取绑定币种。每策略/币种在列表价格上过滤后选最低三条，多策略取同一原始行的去重合集，保持原始顺序。
- 空名单策略使用首维度 `dimensions[0]`，支持 `lowest` 及旧平铺 config。黑名单优先；白名单仅跳过库存、好评率。报价须为对应币种有效正数且严格大于门槛，门槛优先级 `filter_price → price → minimum_price → floor_price`；库存过滤支持 K/M/G/B。竞价幅度、舍入精度不参与加强选择。
- 无启用策略或无合格候选时不加强，仍保存全部列表行；无效策略 JSON 或不支持的首维度在版本递增前报错。加强对象提前确定，详情刷新后不重新排序补抓。

## 详情单价与失败边界

- 逐店点击 `#pcOtherOffer .other-seller--gradient` 的“查看/View”，确认 `#pcMain` 店铺身份，并确认价格节点替换或内容刷新；已选中店铺的无操作查看允许使用已加载详情。
- 优先读取 `.vue-portal-target .pricing-container` 内“单价/Unit Price”，保留价格精度、币种，更新 price/unit_price/currency，标记 `unit_price_source=offer_detail`。不能使用列表最低/from 价格替代。
- G2G 数量为 1 时可能隐藏单价节点。仅在可见详情容器唯一、数量输入唯一且明确为 1、金额标签为总金额/总价/Total amount、币种可确认，并通过店铺与金额刷新校验时，将这一件的详情金额作为单价，标记 `offer_detail_single_unit`；多件总价不作为单价。
- 每家定位、DOM 读取、点击、详情等待及清理统一受默认 15 秒期限约束；点击使用 `no_wait_after=True`，随后单独等待详情。日志记录店铺、阶段、价格和耗时。
- 候选详情失败时终止该目标，不保存该轮、不推进 `last_crawl_at`、不发完成通知，不静默回退旧列表价。其他未加强行保持列表数据。

## 启动、日志与部署

- Linux 入口为 `bash /www/wwwroot/game_crawl/run_crawl_and_consume.sh`，未配置时单 worker，`.env.prod` 写 `CRAWL_WORKER_COUNT=2` 后普通启动即为双 worker。临时覆盖可用 `CRAWL_WORKER_COUNT=2 bash /www/wwwroot/game_crawl/run_crawl_and_consume.sh`。
- 脚本用 `logs/crawl.lock` 的 flock 防止同一脚本重叠，先非阻塞尝试；上一轮仍运行时使用 `flock -w 30` 最多等锁 30 秒，上一轮退出后本次立即执行。等锁超时记日志并返回 0；锁本身报错记录并返回非零。额外用 `logs/crawl_wait.lock` 的非阻塞锁限制最多一个等锁触发，重复触发已有等待者时记日志后退出；取得运行锁后立即关闭等待锁 fd，再启动 workers，子进程仅继承运行锁。两种锁文件均不可在运行/等待期间删除，否则不同 inode 会破坏互斥。
- cron 可在每分钟的 0/30 秒各触发一次，配合上述开始间隔、临近到期等待和有界等锁，避免 30 秒边界上的上一轮退出延迟直接漏掉本次。最多一轮执行及一个触发等锁，不累计排队；持续长于调度周期时实际频率仍受抓取耗时限制，等待名额已占或等锁超过 30 秒时触发仍会跳过，不能承诺硬实时。锁不覆盖绕过脚本直接执行 Python 的进程，也不是跨服务器数据库任务锁。
- `xvfb-run -a -n` 从 90+worker index 开始选择空闲显示号。脚本显式设置 APP_ENV=prod、自带 Chromium、headed 模式，须有可用 Xvfb。`Missing X server or $DISPLAY` 是浏览器启动错误，不能当作详情选择器错误。
- Python 在首个有效且本轮需执行的目标才创建 Playwright driver；全轮跳过时不启动 driver 或 Chromium。G2G 需要页面时才创建浏览器和 G2G context/page，并在同一 worker 内复用；ELD API 失败可复用同一个浏览器进行 DOM 兜底。shell 仍为每个 worker 启动 Xvfb，纯 ELD API 成功轮次省掉的是 Python 中的 Chromium 与页面资源，并未取消 Xvfb 或 Python/Playwright driver 的启动开销。
- 日志使用固定 `crawl.log`、`crawl_worker_N.log`、`xvfb_worker_N.log`；不为每轮生成时间戳日志文件。启动脚本只追加日志；独立维护入口 `bash cleanup_logs.sh` 调用 `tools/cleanup_logs.py`，优先使用项目 venv，否则使用 python3，只依赖标准库，不加载环境配置或访问数据库。
- 清理默认处理项目 `logs/` 的上述固定日志和 `cron.log`：单文件超过 20 MiB 时压缩为 `原名.纳秒时间戳.gz`，压缩成功并落盘后原地截断当前文件，保留 inode。每个日志最多保留 3 个备份，并删除 mtime 超过 7 天的备份；兼容旧数字后缀 `.1`、`.1.gz` 等，旧未压缩备份按同样保留规则处理。超过 7 天的旧 `xvfb_YYYYMMDD_HHMMSS_PID_worker_N.log` 也删除。小日志和空的固定日志保留；仅匹配目录第一层普通文件，不跟随文件符号链接，不清理未知文件、子目录或锁文件。
- 清理复用 `crawl.lock`，最多等待 30 秒；爬虫或另一清理进程持续持锁时输出跳过并返回 0，其他 IO/压缩错误返回非零，压缩未成功时不截断源日志。持锁期间生产 worker 不运行；绕过启动脚本的写入不受保护，启动触发的锁等待/跳过日志仍可能与 `crawl.log` 的复制截断并发，极短窗口内这些行可能丢失。`crawl.lock`、`crawl_wait.lock` 均不删除。`--dry-run` 仅预览日志操作（仍获取运行锁，缺少锁文件时会创建它）。
- 参数 `--max-mb`、`--keep`、`--days` 接受正整数，默认 20/3/7；`--wait-seconds` 接受非负整数，默认 30；`--log-dir` 可覆盖日志目录。`cron/crawl-log-cleanup` 提供 Linux root 的 `/etc/cron.d` 配置，每小时第 17 分钟执行一次，输出交给 cron，不额外写清理日志。上传后以 root 将它复制到 `/etc/cron.d/crawl-log-cleanup` 并设权限 644 才会启用；宝塔可等价配置每小时运行 `bash /www/wwwroot/game_crawl/cleanup_logs.sh`。不要重复安装两种调度。清理按检查时刻执行且忙时可能跳过，不能宣称文件大小始终硬限制在 20 MiB，也不能将本地配置当成线上已安装。
- 日志区分 ELD API 请求模式和按需启动 Chromium 的原因；单目标完成/失败耗时从类型与策略处理开始，包含资源初始化、抓取和数据库操作，不含目标间隔等待。worker 耗时从 run 开始到资源清理结束，包含数据库任务读取与间隔等待，不含 Python 模块导入或 Xvfb 启动。shell 汇总按顺序 wait 的完成日志不能当作各 worker 的真实结束时刻，排查耗时优先看 worker 自身日志。
- `timed_stage` 使用单调计时输出并立即刷新阶段开始、完成或失败日志，原始异常继续向上传递。资源初始化分别记录 Playwright driver、Chromium、G2G context/page，仅在实际创建时记录，复用不重复计为启动。G2G 通用卡片路径（物品等）分别记录页面导航、货币检查、必要时 USD 切换重导航、首批卡片加载、固定 3 秒稳定等待、列表数量检查/滚动、页面货币解析、卡片提取及 Python 数据过滤/价格解析。阶段计时不改变既有等待、选择器、抓取上限或价格来源；金币/游戏币详情加强仍使用既有逐店阶段日志。阶段日志用于定位耗时，不代表已确定真实网站的瓶颈或整轮一定缩短到 30 秒。
- `deploy_prod.sh` 会 rsync、安装依赖和验证环境，属于真实部署；其帮助文字可能滞后，以启动脚本为准。维护规范不等于运行部署。迁移文件存在也不代表已执行；有字段变更时同步后台模型、验证、UI 和迁移。

## 验证与规范维护

按改动选择相关检查，真实平台测试与本地 DOM 夹具区分，不能把夹具通过说成线上已恢复。

```bash
rtk proxy venv/bin/python -m unittest discover -s tests -p 'test_*.py'
rtk proxy bash -n run_crawl_and_consume.sh
rtk git diff --check
```

`test_other_offer_prices.py` 覆盖加强选择、异步价格归属、列表上限与各 DOM 路径的全局排除；`test_eldorado_limit.py` 覆盖独立 ELD 上限、第一页、价格解析前排除及全部排除时不回退 DOM；`test_crawl_excluded_stores.py` 覆盖多名字/ID 归一化完整匹配、空配置、默认与加强类型在价格处理前排除、剩余候选入库及通知条数；`test_crawl_filter.py` 覆盖策略、完整保存和通知；`test_crawl_interval.py` 用模拟时钟覆盖连续三轮 ELD 目标、抓取耗时与提前触发、已到期目标绕过队列前部等待、最近到期者优先、目标处理后重新判断到期、提前唤醒不提前抓取、无效任务及历史时间、等待/跳过边界、失败不推进时间及开始时间持久化；`test_crawl_timing.py` 覆盖页面导航/卡片等待/提取的独立计时、阶段失败保留原始异常、driver/browser/page 初始化独立计时及复用不重复计时。`test_crawl_workers.py` 覆盖分片、dotenv 进程数加载、环境变量覆盖、非法值拒绝、配置不执行、等锁成功/超时/错误及失败汇总，并以操作系统 flock 验证一轮运行、一个等待者、额外触发退出和两轮无重叠。`test_crawl_session.py` 覆盖 ELD 纯 API 完整保存与通知、全部排除时零条成功、不需执行时不启动 driver、混合目标复用浏览器与 ELD context 隔离、API 错误/空结果/无法解析 URL 的 DOM 兜底、失败目标不通知且继续后续目标、资源释放；通过本地 HTTP 服务与真实 Playwright 验证独立请求无需 Chromium，不能将其当作真实平台线上验证。

`test_cleanup_logs.py` 覆盖压缩内容、原地截断后已有追加句柄继续写入、备份数量和过期清理、文件范围及符号链接排除、预览无日志修改、压缩落盘失败保留原日志，以及真实跨进程运行锁阻止清理。

每次相关改动完成后按根目录 AGENTS.md 对照最终源码更新本 skill；独立工具变化另同步既有 CodeBuddy skill。替换过时约定，不写改动流水账、凭证或未经验证的部署状态。
