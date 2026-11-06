---
name: g2g-scraper
description: |
  G2G 游戏交易平台数据爬取工具集。This skill should be used when the user wants to:
  1. 爬取 G2G 产品卡片列表（价格、卖家、库存、已售等）
  2. 爬取 G2G 分类页商户数据
  3. 导出卖家 Offers (bulk_export)
  4. 爬取 Eldorado.gg 商户数据
  5. 管理 G2G 登录会话（含 OTP 验证）
  Trigger keywords: G2G, 爬数据, scrape, 导出, export, 商户, seller, offers, 产品, product, eldorado
---

# G2G 数据爬取工具集

## 项目结构

```
g2g/
├── g2g/                        # 核心库
│   ├── config.py               # 配置（账号/URL/API/请求头）
│   ├── browser.py              # 浏览器管理（创建/会话/反检测）
│   ├── auth.py                 # 登录认证（自动填表 + 手动OTP + token提取）
│   └── api.py                  # API 调用（bulk_export/卖家数据/通用请求）
├── tools/                      # 工具集
│   ├── scrape_g2g_products.py  # 爬取 G2G 产品卡片（无需登录，输出 JSON）
│   ├── scrape_category.py      # 爬取 G2G 分类页商户（无需登录）
│   ├── scrape_eldorado.py      # 爬取 Eldorado.gg 商户（无需登录）
│   ├── monitor_eldorado.py     # 连接已开启 CDP 的浏览器，监控当前订单详情及聊天
│   ├── export_offers.py        # 导出 Offers（需登录）
│   └── scrape_seller.py        # 爬取商户数据（需登录）
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
```bash
mkdir -p downloads
python -m tools.monitor_eldorado --chrome-auto-connect --output downloads/eldorado_orders.jsonl
```
先在已启用远程调试的 Chrome 中人工登录并打开订单详情。`--chrome-auto-connect` 通过 macOS Chrome 的 DevToolsActivePort 发现已有浏览器，连接时按提示允许；也可通过 `--cdp-url` 指定其他 Chromium 调试地址。每 3 秒读取首个匹配的 `/order/` 页面，可通过 `--interval` 和 CSS 选择器参数调整。程序不自动点击、刷新或遍历订单，首次读取和内容变化时向终端输出 JSON Lines，可选文件追加保存。聊天位于 TalkJS iframe，提取可见消息文本、绝对时间、方向及附件名称；结构不匹配时回退到可见文本。页面结构变化、虚拟滚动或尚未加载的内容可能影响结果。文件及终端输出含订单和对话信息，按敏感数据保管。

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

- **输出格式**: 抓取工具输出 JSON；订单监控输出 JSON Lines 到终端，可选追加到文件
- **会话持久化**: `session.json` 保存登录态，后续运行无需重新登录
- **OTP 验证**: 登录时需邮箱 OTP，脚本提示用户手动输入
- **限流**: G2G 有请求频率限制，避免短时间内多次登录
- **反检测**: 使用 `channel="chrome"` 启动真实 Chrome
- **公开页面爬取**: 无需登录，直接 Playwright 打开页面提取数据
