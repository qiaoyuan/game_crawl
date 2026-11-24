---
name: g2g-scraper
description: |
  G2G 游戏交易平台数据爬取工具集。This skill should be used when the user wants to:
  1. 爬取 G2G 产品卡片列表（价格、卖家、库存、已售等）
  2. 爬取 G2G 分类页商户数据
  3. 导出卖家 Offers (bulk_export)
  4. 爬取 Eldorado.gg 商户数据
  5. 管理 G2G 登录会话（含 OTP 验证）
  6. 监控 Eldorado 售出订单列表、订单详情与聊天记录
  Trigger keywords: G2G, 爬数据, scrape, 导出, export, 商户, seller, offers, 产品, product, eldorado
---

# G2G 数据爬取工具集

## 项目结构

```
g2g/
├── g2g/
│   ├── config.py、db.py          # 项目配置与数据库访问
│   ├── crawl/                   # 数据库生产爬虫
│   ├── scraper/                 # 独立抓取、登录、导出与汇率更新
│   └── eldorado/
│       ├── manual/              # 按次采集一笔订单
│       ├── monitor/             # 持续监控与新单状态记录
│       └── shared/              # 浏览器连接、售出列表、详情与聊天读取
├── tools/                       # 命令入口及独立部署的服务器监控脚本
├── test/g2g_scraper/            # 本 skill 的测试
├── downloads/                  # 输出目录
├── session.json                # 登录会话（自动生成）
├── main.py                     # 主入口
└── requirements.txt            # 依赖: playwright, requests
```

## 使用方式

```bash
cd /Users/qiaoyuan/CodeBuddy/g2g
source venv/bin/activate
```

### 爬取 G2G 产品卡片（无需登录，仅输出 JSON）
```bash
# 默认 URL
python -m tools.scrape_g2g_products

# 自定义 URL 和滚动次数
python -m tools.scrape_g2g_products "https://www.g2g.com/cn/categories/..." --scroll 15
```

提取字段：seller_id, seller_name, seller_level, sold_count, stock, price, currency, min_order, delivery_time, product_title, offer_url, avatar, is_online

### 爬取 G2G 分类页商户（无需登录）
```bash
python -m tools.scrape_category "https://www.g2g.com/cn/categories/..."
```

### 爬取 Eldorado.gg 商户（无需登录）
```bash
python -m tools.scrape_eldorado "https://www.eldorado.gg/..."
```

### 监控 Eldorado 订单详情与聊天

用户指定订单号或详情链接，要求按次记录一笔订单或读取该单聊天时，使用 [Eldorado 单笔订单采集规范](../../../.agents/skills/eldorado-manual-order/SKILL.md)。

```bash
mkdir -p downloads
python -m tools.monitor_eldorado --chrome-auto-connect --output downloads/eldorado_orders.jsonl
```
先在已启用远程调试的 Chrome 中人工登录并打开订单详情。`--chrome-auto-connect` 通过 macOS Chrome 的 DevToolsActivePort 发现已有浏览器，连接时按提示允许；也可通过 `--cdp-url` 指定其他 Chromium 调试地址。每 3 秒读取首个匹配的 `/order/` 页面，可通过 `--interval` 和 CSS 选择器参数调整。此手动详情模式不自动刷新或遍历订单，首次读取和内容变化时向终端输出 JSON Lines，可选文件追加保存。聊天位于 TalkJS iframe，提取可见消息文本、绝对时间、方向及附件名称；结构不匹配时回退到可见文本。页面结构变化、虚拟滚动或尚未加载的内容可能影响结果。文件及终端输出含订单和对话信息，按敏感数据保管。

### 每 30 秒检查 Eldorado 售出列表最新一笔

在已登录的 Chrome 中打开售出订单列表，按 Recent 排序，然后从项目根目录运行：

```bash
venv/bin/python -m tools.monitor_eldorado --sold-orders --chrome-auto-connect
```

- 首轮立即刷新，以列表第一笔建立起点。之后每 30 秒只判断列表第一笔，它是新的 Aion 2 订单才入队。下单时间取可见 Ordered date，按浏览器当地时区转为 UTC。显示的排序或时间无效时，本轮不推进起点。
- 最新时间之后的订单，或与最新时间相同且尚未见过的订单号，均纳入新单判断。所有游戏参与更新时间，只对 Aion 2 打开临时详情页。已经发现的订单持久排队，详情未就绪会重试，即使后来已离开首位也可完成。如果监控使用的列表标签页进入订单详情，程序会在同一会话另开售出列表继续刷新，方便查看原详情。
- 详情按可见订单号与链接核对身份，从 Order details 和 Payment details 提取游戏、地区、阵营、服务器、数量、买家、订单金额、佣金、实收，保留页面单位与符号。必需字段齐全时，连同订单号及 UTC 下单时间写成一行 JSON；不读取聊天来补字段。
- 默认输出为 `downloads/eldorado_new_orders.jsonl`（`--output` 可指定独立文件）。同路径加 `.sqlite3` 的状态库保存起点、待处理订单及已记录 ID，须与记录文件配套保留。单进程独占写入，重启恢复待处理单及未写完的行；同一订单号只写一次。启动前先人工登录，关闭浏览器或监控所在的列表标签页会结束监控。验收及启动方式见 [采集规格](../../../docs/eldorado-sold-orders.md)。

查看汇总请打开 `downloads/eldorado_new_orders.json`。该文件在启动恢复和每次录入后更新，包含所有已录入订单的 JSON 数组。自定义输出以 `.jsonl` 结尾时，汇总换成 `.json` 后缀，否则追加 `.json`。

### 每 30 秒刷新 Eldorado 售出列表并读取 Aion 2 聊天

在 Chrome 登录 Eldorado 并打开售出订单页，然后在项目根目录运行：

```bash
mkdir -p downloads
venv/bin/python -m tools.monitor_eldorado --sold-chats --chrome-auto-connect --output downloads/eldorado_aion2_chats.jsonl
```

- 首轮立即刷新，之后默认以 30 秒为开始间隔刷新当前售出列表页。可用 `--interval` 覆盖。若未打开列表则在对应浏览器会话新建标签页。登录跳转后需在该标签页返回售出列表。
- 从订单行的游戏名完整匹配 Aion 2 / Aion2，忽略大小写，包含各状态，仅取当前页。独立的详情读取任务按列表顺序在临时标签中逐笔进入同站订单，排队及正在处理的同一链接合并。详情耗时不延迟列表刷新。
- 使用详情中的 TalkJS 聊天窗口，从最新位置向上滚动并合并重叠的消息，保留会话顺序和相同内容的独立发送。按页面标记区分收发，文件消息记录附件名，无文件名时记为「[附件]」。到顶且内容稳定约 1.2 秒结束。遇到明确的空会话提示可保存空列表。读取限时 20 秒，未就绪、无法拼接或超时会在之后发现该订单时重试，并保留上次成功记录。可读范围由网页实际加载的历史决定。
- 终端与可选 JSONL 文件按订单在首次读取或聊天有变化时输出完整快照，captured_at 为 UTC 采集时间，重启后重新输出首份。详情 URL 去掉查询参数和片段。快照和聊天保存在本机，勿提交到版本库。详见 [采集规格](../../../docs/eldorado-sold-orders.md)。

### 手动更新中行美元汇率

在项目根目录执行 `python -m tools.update_boc_usd_rate`，生产使用 `APP_ENV=prod venv/bin/python -m tools.update_boc_usd_rate`。每次通过 HTTPS 请求[中行外汇牌价](https://www.boc.cn/sourcedb/whpj/)，读取首条美元行的“现汇买入价”，用 Decimal 除以 100。复用项目数据库连接，以纯十进制文本写入 `admin_config.value`，配置名为 `usd_cny_rate`。依靠 `name` 唯一索引首次创建，后续只更新 `value` 和 `updated_at`。请求失败、无法定位报价或报价不是正数时，报错退出，保留原值。

### 导出 Offers（需登录会话）
```bash
python main.py export
```

### 会话管理
```bash
python main.py login    # 登录并保存会话
python main.py reset    # 清除会话
```

## 关键注意事项

- **输出格式**: 商品和商户抓取工具输出 JSON；订单监控输出 JSON Lines；中行汇率工具写数据库并打印结果。
- **会话持久化**: `session.json` 保存登录态，后续运行无需重新登录
- **OTP 验证**: 登录时需邮箱 OTP，脚本提示用户手动输入
- **限流**: G2G 有请求频率限制，避免短时间内多次登录
- **反检测**: 使用 `channel="chrome"` 启动真实 Chrome
- **公开页面爬取**: 无需登录，直接 Playwright 打开页面提取数据

## 本机浏览器配置

`g2g/config.py` 从当前环境文件读取 `CRAWL_USER_AGENT`、`CRAWL_VIEWPORT_WIDTH`、`CRAWL_VIEWPORT_HEIGHT`，系统环境变量优先。UA 未配置或空白时保留原平台默认值；非空时用于共享 G2G 配置及数据库入口 ELD 的 API 与 DOM。尺寸默认 1280×800，必须为正整数，只影响使用共享 VIEWPORT 的浏览器页面，不影响 API。配置在进程启动时读取，不自动轮换，不修改语言、计价、分片或调度，也不保证不同机器不能被关联。独立 ELD 工具不因此自动支持该 UA 配置。
