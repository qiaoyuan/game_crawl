---
name: g2g-crawl-dev
description: 维护本项目 G2G/ELD 数据库任务爬虫的配置、分片、店铺加强、数据快照、PHP 改价通知和 Linux 启动脚本；修改相关源码或排查抓取停滞、价格错误、进程重叠时使用。
---

# 数据库任务爬虫规范

## 入口与职责

- `tools/crawl_from_db.py` 是生产入口，`g2g/db.py` 负责数据库读写，`g2g/crawl_filter.py` 决定空加强名单时的 Top3 对象。
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
| `CRAWL_WORKER_COUNT` | 1 | 启动脚本的 worker 数，正整数系统环境变量 |

`CRAWL_WORKER_COUNT` 由 shell 启动脚本读取，不会自动从 Python 的 dotenv 配置加载。以上条数控制每个目标的竞品数量，不控制领取的任务数量。

- G2G 金币/游戏币走 `scrape_other_offer_page`；其他分类走通用 `scrape_page`。两条 G2G 卡片路径都应用 G2G 上限，提取后再选择加强对象。达到上限停止滚动等待；未达到时沿用数量稳定后结束的加载逻辑，不向后补齐加强店铺。
- `ELD游戏币` 走 `scrape_eldorado_page`，优先 offers API，失败时 DOM 兜底。API 固定 `pageIndex=1`，`pageSize` 使用 ELD 上限；保留商品属性与排序，URL 的分页参数不能覆盖配置。API 返回超过上限也截断；DOM 兜底用同一上限，不滚动加载更多。两个平台的条数独立。
- 不要把 ELD 游戏币的 API 规则描述成所有独立工具或所有分类均已支持。

## 任务、分片与快照

- `get_pending_targets` 查询启用、未删除、`crawl_server` 相同的目标，按 `MOD(id, worker_count)=worker_index` 分片并按 id 排序。worker index 从 0 开始，必须小于正数 worker count。
- 跳过无有效 `game_product_id`、无 URL 的任务。`crawl_interval` 按秒，与 `last_crawl_at` 决定是否到期；不是分片数量。
- `crawl_type`：0 默认，1 店铺加强；兼容数字字符串、历史 `default/top3`，缺失/空值按 0，未知类型报错。
- 每个目标抓取前递增 `crawl_target.version`，本轮所有 `crawl_data` 带同一 `target_id`、`game_product_id` 和 version。加强失败仍可能已递增版本。
- 成功顺序：保存完整候选 → 更新 `last_crawl_at` → 插入 `crawl_notify(status=0)`，通知带目标、准确 version、实际保存条数。成功零条也可发零条通知。PHP 按通知 version 读取快照，不能只读最新版本。
- 这些数据库步骤当前分别提交，不能宣称数据保存、时间更新和通知是一个原子事务。目标异常会记录失败并继续后续目标；有失败时本轮返回非零。

## 店铺加强选择

两种类型都保存本轮提取的全部列表候选，不按策略裁剪入库。只有 G2G 金币/游戏币的类型 1 读取店铺详情。

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
- 候选详情失败时终止该目标，不保存该轮、不更新完成时间、不发完成通知，不静默回退旧列表价。其他未加强行保持列表数据。

## 启动、日志与部署

- Linux 入口为 `bash /www/wwwroot/game_crawl/run_crawl_and_consume.sh`，默认单 worker。临时双 worker 可用 `CRAWL_WORKER_COUNT=2 bash /www/wwwroot/game_crawl/run_crawl_and_consume.sh`。
- 脚本用 `logs/crawl.lock` 的非阻塞 flock 防止同一脚本重叠；上一轮未结束时本次跳过。此锁不覆盖绕过脚本直接执行 Python 的进程，也不是跨服务器数据库任务锁。
- `xvfb-run -a -n` 从 90+worker index 开始选择空闲显示号。脚本显式设置 APP_ENV=prod、自带 Chromium、headed 模式，须有可用 Xvfb。`Missing X server or $DISPLAY` 是浏览器启动错误，不能当作详情选择器错误。
- 日志使用固定 `crawl.log`、`crawl_worker_N.log`、`xvfb_worker_N.log`；不为每轮生成时间戳日志文件。当前脚本未做日志轮转，不能宣称大小已受限。
- `deploy_prod.sh` 会 rsync、安装依赖和验证环境，属于真实部署；其帮助文字可能滞后，以启动脚本为准。维护规范不等于运行部署。迁移文件存在也不代表已执行；有字段变更时同步后台模型、验证、UI 和迁移。

## 验证与规范维护

按改动选择相关检查，真实平台测试与本地 DOM 夹具区分，不能把夹具通过说成线上已恢复。

```bash
rtk proxy venv/bin/python -m unittest discover -s tests -p 'test_*.py'
rtk proxy bash -n run_crawl_and_consume.sh
rtk git diff --check
```

`test_other_offer_prices.py` 覆盖加强选择、异步价格归属和列表上限；`test_eldorado_limit.py` 覆盖独立 ELD 上限与第一页；`test_crawl_filter.py` 覆盖策略、完整保存和通知；`test_crawl_workers.py` 覆盖分片、默认进程数、撞锁及失败汇总。

每次相关改动完成后按根目录 AGENTS.md 对照最终源码更新本 skill；独立工具变化另同步既有 CodeBuddy skill。替换过时约定，不写改动流水账、凭证或未经验证的部署状态。
