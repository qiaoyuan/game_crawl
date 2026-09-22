"""
从数据库 crawl_target 表读取 URL 列表，批量爬取并保存到 crawl_data 表
用法: python -m tools.crawl_from_db
双进程: bash run_crawl_and_consume.sh（生产环境）
单个分片: python -m tools.crawl_from_db --worker-count 2 --worker-index 0
"""

import argparse
import asyncio
import json
import math
import re
from playwright.async_api import async_playwright
from g2g import config, db

# 货币符号 → 货币代码映射（页面实际显示的货币）
CURRENCY_SYMBOL_MAP = {
    "$": "USD",        # 默认美元（G2G 仅用 $ 时通常是 USD）
    "S$": "SGD",       # 新加坡元
    "US$": "USD",      # 美元（显式）
    "A$": "AUD",       # 澳元
    "C$": "CAD",       # 加元
    "HK$": "HKD",      # 港币
    "NZ$": "NZD",      # 新西兰元
    "€": "EUR",        # 欧元
    "£": "GBP",        # 英镑
    "¥": "JPY",        # 日元
    "CN¥": "CNY",      # 人民币
    "RM": "MYR",       # 马来西亚令吉
    "₩": "KRW",        # 韩元
    "₹": "INR",        # 印度卢比
    "฿": "THB",        # 泰铢
    "₱": "PHP",        # 菲律宾比索
    "Rp": "IDR",       # 印尼盾
    "R$": "BRL",       # 巴西雷亚尔
    "CHF": "CHF",      # 瑞士法郎
}


def parse_currency_from_price(price_text: str) -> tuple[str | None, str | None]:
    """
    从价格文本中解析货币符号和纯数字价格
    例如: "S$ 12.50" → ("SGD", "12.50")
          "US$ 0.87" → ("USD", "0.87")
          "$5.00"     → ("USD", "5.00")
          "€ 10,99"   → ("EUR", "10.99")
    """
    if not price_text:
        return None, None

    text = price_text.strip()

    # 尝试匹配 "S$ 12.50", "US$ 0.87", "HK$ 100" 等带前缀的价格
    # 按符号长度从长到短匹配，避免 "$" 匹配到 "S$" 中的 "$"
    sorted_symbols = sorted(CURRENCY_SYMBOL_MAP.keys(), key=len, reverse=True)
    for symbol in sorted_symbols:
        if text.startswith(symbol):
            price_str = text[len(symbol):].strip().replace(",", "")
            return CURRENCY_SYMBOL_MAP[symbol], price_str

    # 尝试匹配后缀货币代码，如 "12.50 SGD"
    suffix_match = re.match(r"^([\d.,]+)\s*([A-Z]{3})$", text)
    if suffix_match:
        return suffix_match.group(2).upper(), suffix_match.group(1).replace(",", "")

    return None, text


def parse_rating(text: str | None) -> str | None:
    """解析好评率，去掉 % 号，保留两位小数字符串。
    例如: '96.00%' -> '96.00', '100%' -> '100.00', '0.00%' -> '0.00'
    """
    if not text:
        return None
    t = text.strip().rstrip("%").strip()
    try:
        return f"{float(t):.2f}"
    except ValueError:
        return None


async def scrape_page(page, url: str) -> list:
    """爬取单个页面的产品卡片"""
    print(f"  [*] 打开: {url}")
    await page.goto(url, wait_until="domcontentloaded", timeout=60000)

    # 兜底：如果 g2g_regional cookie 货币不是 USD，用 JS 设 cookie 后刷新
    currency_check = await page.evaluate("""() => {
        const m = document.cookie.match(/g2g_regional=([^;]+)/);
        if (m) {
            try { return JSON.parse(decodeURIComponent(m[1])).currency; } catch(_) {}
        }
        return null;
    }""")
    if currency_check and currency_check != "USD":
        print(f"  [!] 检测到货币 {currency_check}，强制切换为 USD 并刷新...")
        await page.evaluate("""() => {
            document.cookie = 'g2g_regional=%7B%22country%22%3A%22US%22%2C%22language%22%3A%22en%22%2C%22currency%22%3A%22USD%22%7D; path=/; domain=.g2g.com; max-age=86400';
        }""")
        await page.goto(url, wait_until="domcontentloaded", timeout=60000)

    # 等待卡片加载
    for i in range(60):
        cards = await page.query_selector_all('[aria-label="Product Card"]')
        if len(cards) > 0:
            break
        await asyncio.sleep(1)
    await asyncio.sleep(3)

    # 滚动加载
    prev_count = 0
    for s in range(10):
        count = len(await page.query_selector_all('[aria-label="Product Card"]'))
        if count == prev_count:
            break
        prev_count = count
        await page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
        await asyncio.sleep(2)

    # ========== 先提取页面级别货币（全局指示器） ==========
    page_currency = await page.evaluate("""
        (() => {
            // 方法1: G2G 页面顶部货币选择器
            const currencyBtn = document.querySelector('[data-testid="currency-selector"]')
                || document.querySelector('[class*="currency"] button')
                || document.querySelector('button[class*="Currency"]');
            if (currencyBtn) return currencyBtn.innerText.trim().split(/\\s+/)[0];

            // 方法2: 查找页面上的 "All prices in XXX" 提示
            const banners = document.body.innerText.match(/All prices (?:are )?in\\s+([A-Z]{3})/i);
            if (banners) return banners[1].toUpperCase();

            // 方法3: 查找任意货币代码文本
            const currencyMatch = document.body.innerText.match(/(?:Prices? (?:in|are in)|Currency[：:]\\s*)([A-Z]{3})/i);
            if (currencyMatch) return currencyMatch[1].toUpperCase();

            return null;
        })()
    """)
    if page_currency:
        print(f"  [*] 页面货币: {page_currency}")
    else:
        print(f"  [*] 未检测到页面级别货币，将从每个卡片价格文本解析")

    # 提取数据
    items_raw = await page.evaluate("""
        (() => {
            const VALID_CURRENCY_CODES = new Set([
                'USD','SGD','EUR','GBP','AUD','CAD','JPY','CNY','HKD','MYR','KRW','INR','THB','PHP','IDR','CHF','NZD','BRL','SEK','NOK','DKK','PLN','AED','SAR'
            ]);

            const cards = document.querySelectorAll('[aria-label="Product Card"]');
            const results = [];
            cards.forEach(card => {
                const item = {};

                // 卖家链接（不含 /offer/ 的 g2g.com/ 链接）
                const sellerLink = card.querySelector('a[href*="g2g.com/"]:not([href*="offer"]):not([href*="categories"])');
                if (sellerLink) {
                    const href = sellerLink.getAttribute('href') || '';
                    const match = href.match(/g2g\\.com\\/([A-Za-z0-9_-]+)$/);
                    if (match) item.seller_id = match[1];
                    item.seller_url = href;
                }
                // 卖家名
                const sellerNameEl = card.querySelector('.truncate.text-xs.font-medium');
                if (sellerNameEl) item.seller_name = sellerNameEl.innerText.trim();
                // 卖家等级
                let levelEl = card.querySelector('.text-\\\\[10px\\\\].leading-3.font-medium');
                if (!levelEl) levelEl = card.querySelector('[class*="text-\\\\[10px"]');
                if (levelEl) item.seller_level = levelEl.innerText.trim();

                // 产品标题
                const titleEl = card.querySelector('.line-clamp-2');
                if (titleEl) item.product_title = titleEl.innerText.trim();

                // Chips: 已售/库存/最小起订/配送时间
                const chips = card.querySelectorAll('.h-chip__content');
                chips.forEach(chip => {
                    const t = chip.innerText.trim();
                    if (!t) return;
                    // 好评率如 "100%"
                    if (/^\\d{1,3}%$/.test(t)) {
                        item.rating = t;
                    }
                    // 已售出 / Sold
                    else if (/Sold|sold|\\u5df2\\u552e/.test(t)) {
                        item.sold_count = t;
                    }
                    // 库存（纯数字+可选k/m后缀，排除带中文/字母的）
                    else if (/^[\\d.,]+[kKmM]?$/.test(t) && !/[^\\d.,kKmM]/.test(t)) {
                        item.stock = t;
                    }
                    // 最小起订
                    else if (/^Min|^min|^\\u6700\\u5c0f/.test(t)) {
                        item.min_order = t;
                    }
                    // 配送时间
                    else if (/\\d+\\s*(\\u5206\\u949f|\\u5c0f\\u65f6|Mins?|Hours?|Hr|Minute|Hour)/i.test(t)) {
                        item.delivery_time = t;
                    }
                });

                // ====== 价格 & 货币 — 适配新旧两种卡片结构 ======
                // 新结构: <span class="text-base font-bold">0.003317</span> <span class="text-xs font-medium">CNY</span>
                // 旧结构: 价格文本含符号如 "S$ 12.50"
                const priceEl = card.querySelector('.text-base.font-bold');
                if (priceEl) {
                    const priceText = priceEl.innerText.trim();

                    // 先尝试从价格文本本身解析货币（旧结构，如 "S$ 12.50"）
                    const symbolMatch = priceText.match(/^(US\\$|S\\$|A\\$|C\\$|HK\\$|NZ\\$|CN\\u00a5|RM|Rp|R\\$|\\u20ac|\\u00a3|\\u00a5|\\u20a9|\\u20b9|\\u0e3f|\\u20b1|CHF|\\$)?\\s*([\\d.,]+)/);
                    if (symbolMatch && symbolMatch[1] && symbolMatch[1].length > 0) {
                        // 旧结构：价格文本自带货币符号
                        item.price_raw = symbolMatch[2];
                        item.currency_from_price = symbolMatch[1];
                    } else {
                        // 新结构：价格是纯数字，货币在兄弟元素
                        item.price_raw = priceText;
                        // 找价格所在容器的兄弟货币标签
                        const priceContainer = priceEl.closest('.flex.flex-wrap') || priceEl.parentElement;
                        if (priceContainer) {
                            const currencySibling = priceContainer.querySelector('.text-xs.font-medium')
                                || priceContainer.querySelector('[class*="text-xs"][class*="font-medium"]');
                            if (currencySibling) {
                                const curText = currencySibling.innerText.trim();
                                if (curText.length <= 5 && !/^\\d/.test(curText)) {
                                    item.currency_label = curText;
                                }
                            }
                        }
                    }
                }

                const offerLink = card.querySelector('a[href*="/offer/"]');
                if (offerLink) item.offer_url = offerLink.href;
                const avatarEl = card.querySelector('img.h-img__image');
                if (avatarEl) item.avatar = avatarEl.src;
                const onlineEl = card.querySelector('.h-user-avatar__online-indicator');
                item.is_online = !!onlineEl;
                results.push(item);
            });
            return results;
        })()
    """)

    # ========== Python 侧货币解析：优先从页面实际内容提取 ==========
    items = []
    for raw in items_raw:
        item = {k: v for k, v in raw.items()}

        price_raw = raw.get("price_raw", "")
        currency_from_price = raw.get("currency_from_price", "")     # JS 从价格文本解析出的符号
        currency_label = raw.get("currency_label", "")               # JS 从兄弟元素提取的标签

        parsed_currency = None

        # 1. 如果 JS 已经从价格文本解析出货币符号（旧结构），直接映射
        if currency_from_price:
            cs = currency_from_price.strip()
            # 先检查是否为3字母货币代码
            if cs.upper() in {"USD", "SGD", "EUR", "GBP", "AUD", "CAD", "JPY", "CNY", "HKD", "MYR", "KRW", "INR", "THB", "PHP", "IDR", "CHF", "NZD", "BRL"}:
                parsed_currency = cs.upper()
            else:
                # 从符号映射表中查找
                sorted_syms = sorted(CURRENCY_SYMBOL_MAP.keys(), key=len, reverse=True)
                for sym in sorted_syms:
                    if cs.startswith(sym):
                        parsed_currency = CURRENCY_SYMBOL_MAP[sym]
                        break

        # 2. 如果 JS 提取了卡片内的货币标签（新结构），直接用
        if not parsed_currency and currency_label:
            cl = currency_label.strip().upper()
            if cl in {"USD", "SGD", "EUR", "GBP", "AUD", "CAD", "JPY", "CNY", "HKD", "MYR", "KRW", "INR", "THB", "PHP", "IDR", "CHF", "NZD", "BRL"}:
                parsed_currency = cl

        # 3. 回退到 Python 侧从价格文本解析
        if not parsed_currency:
            p_cur, _ = parse_currency_from_price(price_raw)
            parsed_currency = p_cur

        # 4. 回退到页面级别货币
        if not parsed_currency:
            parsed_currency = page_currency

        # 5. 最终回退
        if not parsed_currency:
            parsed_currency = "USD"

        item["currency"] = parsed_currency
        item["price"] = price_raw

        # 好评率：去掉 % 号，统一保留两位小数的字符串，如 "96.00"
        rating_raw = item.get("rating", "")
        item["rating"] = parse_rating(rating_raw)

        # 清理临时字段
        item.pop("price_raw", None)
        item.pop("currency_from_price", None)
        item.pop("currency_label", None)

        items.append(item)

    return items


async def scrape_other_offer_page(page, url: str) -> list:
    """爬取游戏币分类页 #pcOtherOffer 下的竞品商户卡片"""
    print(f"  [*] 打开游戏币页面: {url}")
    await page.goto(url, wait_until="domcontentloaded", timeout=60000)

    # 分类页的竞品列表是异步渲染的，先等待容器和首批卡片出现。
    card_selector = "#pcOtherOffer .other-seller--gradient"
    for i in range(60):
        cards = await page.query_selector_all(card_selector)
        if cards:
            print(f"  -> 检测到 {len(cards)} 个竞品商户 ({i + 1}s)")
            break
        await asyncio.sleep(1)

    # 页面可能在滚动后继续加载竞品，连续两轮数量不变才停止。
    previous_count = -1
    stable_rounds = 0
    for _ in range(10):
        count = len(await page.query_selector_all(card_selector))
        if count == previous_count:
            stable_rounds += 1
        else:
            stable_rounds = 0
        if stable_rounds >= 2:
            break
        previous_count = count
        await page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
        await asyncio.sleep(2)

    items_raw = await page.evaluate(
        """
        () => {
            const container = document.querySelector('#pcOtherOffer');
            if (!container) return [];

            const cards = container.querySelectorAll('.other-seller--gradient');
            const results = [];
            const absoluteUrl = (href) => {
                try { return new URL(href, window.location.href).href; }
                catch (_) { return href; }
            };

            cards.forEach(card => {
                const item = {};
                const text = (selector) => {
                    const element = card.querySelector(selector);
                    return element ? element.innerText.trim() : '';
                };

                const sellerLink = card.querySelector('a[href]');
                if (sellerLink) {
                    const href = sellerLink.getAttribute('href') || '';
                    item.seller_url = absoluteUrl(href);
                    try {
                        const segments = new URL(href, window.location.href).pathname
                            .split('/').filter(Boolean);
                        // 竞品卡片的店铺链接形如 /Sunstriders。
                        if (segments.length === 1) item.seller_id = segments[0];
                    } catch (_) {}
                }

                const sellerName = text('.text-body2.ellipsis.text-weight-medium');
                if (sellerName) item.seller_name = sellerName;

                const sellerLevel = text('.text-caption.text-secondary.text-weight-medium');
                if (sellerLevel) item.seller_level = sellerLevel;

                const productTitle = text('.product-card__bg-text');
                if (productTitle) item.product_title = productTitle;

                const avatar = card.querySelector('img.user-avatar');
                if (avatar) item.avatar = avatar.src;
                item.is_online = !!card.querySelector('.g-round-indicator.bg-positive');

                const rating = text('.text-positive.text-weight-medium.q-ml-xs');
                if (rating) item.rating = rating;

                // 已售出可能位于普通 badge，也可能位于 role=alert 的 badge。
                card.querySelectorAll('.bg-neutral-100-light.text-secondary, [role="alert"]')
                    .forEach(element => {
                        const value = element.innerText.trim();
                        if (/已售出|sold/i.test(value)) item.sold_count = value;
                    });

                card.querySelectorAll('.q-badge__delivery').forEach(badge => {
                    const value = badge.innerText.trim();
                    if (!value) return;
                    if (/^(最低|Min\\.?\\s)/i.test(value)) {
                        item.min_order = value;
                    } else if (/\\d+\\s*(分钟|小时|Mins?|Hours?|Hr|Minute|Hour)/i.test(value)) {
                        item.delivery_time = value;
                    } else if (/^[\\d.,]+[kKmM]?$/.test(value)) {
                        item.stock = value;
                    }
                });

                const discount = card.querySelector('.q-chip__content');
                if (discount) item.has_volume_discount = /折扣|discount/i.test(discount.innerText);

                const price = card.querySelector('.text-primary.text-body.text-weight-bold');
                if (price) item.unit_price_raw = price.innerText.trim();

                const currency = card.querySelector('.text-secondary.text-body2.text-weight-medium');
                if (currency) item.currency_label = currency.innerText.trim();

                const priceLabel = card.querySelector('.text-secondary.text-caption-1');
                if (priceLabel) item.price_label = priceLabel.innerText.trim();

                const offerLink = card.querySelector('a[href*="/offer/"]');
                if (offerLink) item.offer_url = absoluteUrl(offerLink.getAttribute('href') || '');

                results.push(item);
            });
            return results;
        }
        """
    )

    items = []
    valid_currency_codes = {
        "USD", "SGD", "EUR", "GBP", "AUD", "CAD", "JPY", "CNY", "HKD",
        "MYR", "KRW", "INR", "THB", "PHP", "IDR", "CHF", "NZD", "BRL",
    }
    for raw in items_raw:
        item = dict(raw)
        price_raw = raw.get("unit_price_raw", "")
        currency_label = raw.get("currency_label", "").strip().upper()
        parsed_currency, parsed_price = parse_currency_from_price(price_raw)

        if not parsed_currency and currency_label in valid_currency_codes:
            parsed_currency = currency_label
        item["price"] = parsed_price or price_raw
        item["unit_price"] = item["price"]
        item["currency"] = parsed_currency or "USD"
        # 好评率：去掉 % 号，统一保留两位小数的字符串，如 "96.00"
        item["rating"] = parse_rating(raw.get("rating", ""))
        item.pop("unit_price_raw", None)
        item.pop("currency_label", None)
        items.append(item)

    return items


async def scrape_eldorado_page(page, url: str) -> list:
    """爬取 Eldorado 游戏币页面，优先直连官方 offers API，DOM 作为兜底。"""
    from urllib.parse import parse_qs, quote, urlencode, urlparse

    print(f"  [*] 打开 Eldorado 页面: {url}")

    # 从商品页 URL 构造官方 offers API。
    # 路径两种形式：/g/132-0-0（132=gameId，0=Currency）和 /g/220（只有 gameId）。
    # te_v0 对应 tradeEnvironmentValue0；其余未知 query 是属性筛选，需原样透传，
    # 例如 path-of-exile-2-orbs=mirror-of-kalandra。
    parsed_url = urlparse(url)
    legacy_match = re.search(r"/g/(\d+)(?:-(\d+)-(\d+))?", parsed_url.path)
    api_response = None
    if legacy_match:
        category_map = {"0": "Currency", "1": "Account", "2": "CustomItem"}
        query = parse_qs(parsed_url.query)
        params = [
            ("gameId", legacy_match.group(1)),
            ("category", category_map.get(legacy_match.group(2) or "0", "Currency")),
        ]
        # 属性筛选参数：除 te_v* 和 offerSortingCriterion 外全部透传，
        # 否则会抓成该游戏的全量报价而不是当前筛选的道具。
        passthrough = []
        for key, values in sorted(query.items()):
            if not values:
                continue
            env_match = re.fullmatch(r"te_v(\d+)", key)
            if env_match:
                params.append((f"tradeEnvironmentValue{env_match.group(1)}", values[0]))
            elif key != "offerSortingCriterion":
                passthrough.append((key, values[0]))
        params.extend([
            ("pageIndex", "1"),
            ("pageSize", "150"),
            ("offerSortingCriterion", query.get("offerSortingCriterion", ["Cheapest"])[0]),
        ])
        params.extend(passthrough)
        api_url = (
            "https://www.eldorado.gg/api/predefinedOffers/augmentedGame/offers?"
            + urlencode(params)
        )
        try:
            api_response = await page.request.get(
                api_url,
                headers={
                    "Accept": "application/json",
                    "Accept-Language": "en-US,en;q=0.9",
                    "Referer": url,
                },
                timeout=60000,
            )
            print(f"  -> Eldorado API 状态: {api_response.status}")
        except Exception as e:
            print(f"  [!] Eldorado API 直连失败: {e}")
    else:
        print("  [!] 无法从 URL 解析 Eldorado gameId，回退 DOM 解析")

    def format_duration(value: str | None) -> str | None:
        """将 API 的 TimeSpan 转成页面显示格式，如 00:06:28 -> 6 min。"""
        if not value:
            return None
        try:
            first, minutes, _seconds = value.split(":", 2)
            if "." in first:
                days_text, hours_text = first.split(".", 1)
                days = int(days_text)
                hours = int(hours_text)
            else:
                days = 0
                hours = int(first)
            if days:
                return f"{days} d"
            if hours:
                return f"{hours} h"
            return f"{max(1, int(minutes))} min"
        except (TypeError, ValueError):
            return str(value)

    def guaranteed_delivery_text(value: str | None) -> str | None:
        if not value:
            return None
        match = re.match(r"^(Minute|Hour|Day)(\d+)$", value)
        if not match:
            return value
        unit = {"Minute": "min", "Hour": "h", "Day": "d"}[match.group(1)]
        return f"{match.group(2)} {unit}"

    def unit_label(unit_system: str | None) -> str:
        return {
            "Unit1": "",
            "Unit1000": "K",
            "Unit1000000": "M",
            "Unit1000000000": "B",
        }.get(unit_system or "", "")

    try:
        if api_response is not None and api_response.ok:
            payload = await api_response.json()
            api_items = []
            for record in payload.get("results", []):
                offer = record.get("offer") or {}
                user = record.get("user") or {}
                order_info = record.get("userOrderInfo") or {}
                delivery = record.get("deliveryTime") or {}

                # 价格统一按 USD 入库。优先用接口的 pricePerUnitInUSD；
                # 若缺失则用 exchangeRate（该字段是「1 USD = N 展示货币」）换算回 USD。
                price_info = offer.get("pricePerUnitInUSD") or {}
                if price_info.get("amount") is None:
                    local_price = offer.get("pricePerUnit") or {}
                    local_amount = local_price.get("amount")
                    rate = (offer.get("exchangeRate") or {}).get("exchangeRate")
                    if local_amount is not None and str(local_price.get("currency")) == "USD":
                        price_info = {"amount": local_amount, "currency": "USD"}
                    elif local_amount is not None and rate:
                        try:
                            price_info = {
                                "amount": round(float(local_amount) / float(rate), 8),
                                "currency": "USD",
                            }
                        except (TypeError, ValueError, ZeroDivisionError):
                            price_info = {}
                    else:
                        price_info = {}
                unit = unit_label(offer.get("unitSystem"))
                username = str(user.get("username") or "").strip()
                offer_id = str(offer.get("id") or "").strip()
                game_alias = str(offer.get("gameSeoAlias") or "").strip()

                median_text = format_duration(delivery.get("deliveryTimeMedian"))
                expected_text = format_duration(delivery.get("expectedTime"))
                if median_text and expected_text and median_text != expected_text:
                    delivery_text = f"{median_text} - {expected_text}"
                else:
                    delivery_text = (
                        median_text
                        or expected_text
                        or guaranteed_delivery_text(offer.get("guaranteedDeliveryTime"))
                    )

                trade_values = offer.get("tradeEnvironmentValues") or []
                trade_name = " / ".join(
                    str(value.get("value"))
                    for value in trade_values
                    if value.get("value")
                )
                title_parts = [offer.get("gameCategoryTitle"), trade_name]

                score = order_info.get("feedbackScore")
                try:
                    rating = f"{float(score):.2f}" if score is not None else None
                except (TypeError, ValueError):
                    rating = None

                seller_url = (
                    f"https://www.eldorado.gg/users/{quote(username, safe='')}/shop/Currency"
                    if username else None
                )
                offer_url = (
                    f"https://www.eldorado.gg/{game_alias}/og/{offer_id}"
                    if game_alias and offer_id else url
                )
                min_quantity = offer.get("minQuantity")

                api_items.append({
                    "seller_id": user.get("id") or offer.get("userId") or username,
                    "seller_name": username or None,
                    "seller_level": "Verified" if user.get("isVerifiedSeller") else None,
                    "seller_url": seller_url,
                    "is_online": False,
                    "product_title": " - ".join(str(v) for v in title_parts if v),
                    "offer_url": offer_url,
                    "stock": str(offer.get("quantity")) if offer.get("quantity") is not None else None,
                    "price": str(price_info.get("amount")) if price_info.get("amount") is not None else None,
                    "currency": "USD",
                    "min_order": (
                        f"{min_quantity} {unit}".strip()
                        if min_quantity is not None else None
                    ),
                    "delivery_time": delivery_text,
                    "rating": rating,
                    "unit": unit,
                    "review_count": order_info.get("ratingCount"),
                    "offer_id": offer_id or None,
                    "platform": "eldorado",
                })

            if api_items:
                print(
                    f"  -> Eldorado API 返回 {len(api_items)} 条 "
                    f"(价格使用 USD: {sum(i.get('currency') == 'USD' for i in api_items)} 条)"
                )
                return api_items
    except Exception as e:
        print(f"  [!] Eldorado API 数据解析失败，回退 DOM 解析: {e}")

    # API 不可用时再加载页面，解析 #other-sellers 下的 .offer-row。
    await page.set_extra_http_headers({
        "Accept-Language": "en-US,en;q=0.9",
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
    })
    await page.goto(url, wait_until="domcontentloaded", timeout=60000)
    await asyncio.sleep(5)

    # 等待 #other-sellers 容器及首批卡片出现
    card_selector = "#other-sellers .offer-row"
    for i in range(60):
        cards = await page.query_selector_all(card_selector)
        if cards:
            print(f"  -> 检测到 {len(cards)} 个竞品卡片 ({i + 1}s)")
            break
        await asyncio.sleep(1)
    else:
        print("  [!] 超时：未找到 #other-sellers .offer-row")
        return []

    # 滚动加载，连续两轮数量不变才停止
    previous_count = -1
    stable_rounds = 0
    for _ in range(10):
        count = len(await page.query_selector_all(card_selector))
        if count == previous_count:
            stable_rounds += 1
        else:
            stable_rounds = 0
        if stable_rounds >= 2:
            break
        previous_count = count
        await page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
        await asyncio.sleep(2)

    # 等待价格元素渲染完成（eld-offer-price 是异步组件）
    for i in range(30):
        price_count = await page.evaluate(
            "() => document.querySelectorAll('strong[aria-label=\"amount-price\"]').length"
        )
        if price_count > 0:
            print(f"  -> 价格元素已渲染，共 {price_count} 个 ({i + 1}s)")
            break
        await asyncio.sleep(1)
    else:
        # 价格还没出来，dump eld-offer-price 的原始 HTML 看看
        eld_html = await page.evaluate("""
            () => {
                const el = document.querySelector('#other-sellers eld-offer-price');
                return el ? el.outerHTML : 'eld-offer-price NOT FOUND';
            }
        """)
        print(f"  [!] 价格元素未渲染，eld-offer-price HTML: {eld_html[:500]}")

    items_raw = await page.evaluate("""
        () => {
            const rows = document.querySelectorAll('#other-sellers .offer-row');
            const results = [];

            rows.forEach(row => {
                const item = {};

                // ---- seller_id & seller_url ----
                // href 形如 /users/SolidSales/shop/Currency
                const sellerLink = row.querySelector('a[href*="/users/"]');
                if (sellerLink) {
                    item.seller_url = sellerLink.href;
                    const m = sellerLink.getAttribute('href').match(/\\/users\\/([^\\/]+)/);
                    if (m) item.seller_id = m[1];
                }

                // ---- seller_name：div.profile__username > a ----
                const nameEl = row.querySelector('div[class*="profile__username"] a, div[class*="profile_username"] a');
                if (nameEl) item.seller_name = nameEl.innerText.trim();

                // ---- 好评率：.score 内紧跟 eld-icon 后面的 div（内容如 " 100% "）----
                const scoreDiv = row.querySelector('.score');
                if (scoreDiv) {
                    // 找 score 下所有直接子 div（非 eld-icon 组件），取文本含 % 的
                    for (const child of scoreDiv.children) {
                        const t = child.innerText ? child.innerText.trim() : '';
                        if (/^\\d+(\\.\\d+)?%$/.test(t)) {
                            item.rating_raw = t;
                            break;
                        }
                    }
                }

                // ---- 库存 / 最小起订 / 配送时间 ----
                // 结构：<div class="desktop--md-2 detail">
                //          <span class="label">In stock</span>
                //          <div class="value"> 2,939,999 B </div>
                //       </div>
                row.querySelectorAll('.detail').forEach(d => {
                    const labelEl = d.querySelector('span.label');
                    const valueEl = d.querySelector('.value');
                    if (!labelEl || !valueEl) return;
                    const label = labelEl.innerText.trim();
                    const value = valueEl.innerText.replace(/\\s+/g, ' ').trim();
                    if (/^in stock$/i.test(label))     item.stock_raw = value;
                    if (/^min\\.?\\s*qty/i.test(label)) item.min_order = value;
                    if (/^delivery time$/i.test(label)) item.delivery_time = value;
                });

                // ---- 价格：strong[aria-label="amount-price"]，内容如 " $0.017 " ----
                // eld-offer-price 是 Angular 组件，优先从 aria-label 取，回退取组件内第一个 strong
                const priceEl = row.querySelector('strong[aria-label="amount-price"]')
                    || row.querySelector('eld-offer-price strong')
                    || row.querySelector('eld-offer-price .text-lg');
                if (priceEl) {
                    item.price_raw = priceEl.innerText.trim();
                } else {
                    // 最终兜底：从 eld-offer-price 的 innerText 用正则抠价格
                    const offerPriceEl = row.querySelector('eld-offer-price');
                    if (offerPriceEl) {
                        const t = offerPriceEl.innerText.trim();
                        const m = t.match(/[$€£¥]?\\s*[\\d.,]+/);
                        if (m) item.price_raw = m[0].trim();
                        item._price_source = 'innerText_fallback:' + t.substring(0, 50);
                    }
                }

                // offer 链接暂无独立地址，用卖家页代替
                item.offer_url = item.seller_url || null;

                results.push(item);
            });
            return results;
        }
    """)

    items = []
    for raw in items_raw:
        item = dict(raw)
        item.pop("_debug", None)

        # 价格 & 货币：price_raw 形如 "$0.017"，货币固定 USD
        price_raw = raw.get("price_raw", "")
        if raw.get("_price_source"):
            print(f"  [price fallback] {raw['_price_source']}")
        parsed_currency, parsed_price = parse_currency_from_price(price_raw)
        item["price"] = parsed_price or price_raw
        item["currency"] = parsed_currency or "USD"
        item.pop("price_raw", None)
        item.pop("_price_source", None)

        # 好评率：去掉 % 保留两位小数
        item["rating"] = parse_rating(raw.get("rating_raw", ""))
        item.pop("rating_raw", None)

        # 库存：去掉单位后缀（"2,939,999 B" → "2939999"）
        stock_raw = raw.get("stock_raw", "")
        stock_num_str = re.sub(r"[^0-9kmKM.]", "", stock_raw.replace(",", ""))
        item["stock"] = stock_num_str or None
        item.pop("stock_raw", None)

        item["is_online"] = False
        item["platform"] = "eldorado"

        items.append(item)

    return items


async def run(worker_index: int = 0, worker_count: int = 1):
    # 读取目标
    targets = db.get_pending_targets(worker_index, worker_count)
    print(f"[*] worker {worker_index + 1}/{worker_count}: 从数据库读取到 {len(targets)} 个爬取目标")

    if not targets:
        print("[*] 当前分片没有爬取目标")
        return 0

    async with async_playwright() as p:
        browser = await p.chromium.launch(**config.browser_launch_kwargs())
        context = await browser.new_context(
            viewport=config.VIEWPORT,
            user_agent=config.USER_AGENT,
        )
        # 强制 USD：G2G 按 IP 自动切货币，注入 cookie 固定为 USD
        await context.add_cookies([{
            "name": "g2g_regional",
            "value": '{"country":"US","language":"en","currency":"USD"}',
            "domain": ".g2g.com",
            "path": "/",
        }])
        await context.add_init_script("""
            Object.defineProperty(navigator, 'webdriver', { get: () => undefined });
            window.chrome = { runtime: {} };
        """)
        page = await context.new_page()

        total_saved = 0
        failed_count = 0
        for idx, target in enumerate(targets):
            target_id = target.get("id")
            try:
                game_product_id = int(target.get("game_product_id") or 0)
            except (TypeError, ValueError):
                game_product_id = 0
            url = target.get("url")
            name = target.get("name", "")
            category = str(target.get("category") or "").strip()

            if game_product_id <= 0:
                print(
                    f"  [{idx+1}/{len(targets)}] {name} 跳过: "
                    f"未关联游戏产品 (game_product_id={game_product_id})"
                )
                continue

            if not url:
                print(f"  [{idx+1}/{len(targets)}] 跳过: 无 URL")
                continue

            # 到期时间 = 上次爬取完成时间 + 30 秒 + crawl_interval（秒）。
            # 未配置或配置为 0 时，仍保留基础 30 秒间隔；首次爬取不等待。
            crawl_interval = target.get("crawl_interval")
            last_crawl_at = target.get("last_crawl_at")
            if last_crawl_at:
                try:
                    interval_seconds = 30 + max(0, int(crawl_interval or 0))
                    if hasattr(last_crawl_at, "timestamp"):
                        last_ts = last_crawl_at.timestamp()
                    else:
                        from datetime import datetime as dt
                        last_ts = dt.strptime(str(last_crawl_at), "%Y-%m-%d %H:%M:%S").timestamp()
                    import time
                    remaining = math.ceil(last_ts + interval_seconds - time.time())
                    if remaining > 0:
                        print(
                            f"  [{idx+1}/{len(targets)}] {name} 跳过: "
                            f"未到间隔时间，还需等待约 {remaining} 秒（总间隔 {interval_seconds} 秒）"
                        )
                        continue
                except Exception as e:
                    print(f"  [{idx+1}/{len(targets)}] 间隔时间解析异常: {e}，继续执行")

            platform = "eldorado" if "eldorado.gg" in url else "g2g"
            category_label = category or "未设置类别"
            print(
                f"\n[{idx+1}/{len(targets)}] {name} "
                f"(target_id={target_id}, game_product_id={game_product_id}, "
                f"category={category_label})"
            )

            try:
                # 爬取前先将版本号 +1，本批数据全部使用新版本号写入。
                # PHP 侧通过 crawl_target.version 对应 crawl_data.version 做改价策略。
                version = db.increment_version(target_id)
                print(f"  -> version={version}")

                # 按 category 分流到不同爬取函数
                if category == "ELD游戏币":
                    # Eldorado 反爬较强，单独创建干净的 context，不带 G2G cookie
                    eld_context = await browser.new_context(
                        viewport=config.VIEWPORT,
                        user_agent=(
                            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                            "AppleWebKit/537.36 (KHTML, like Gecko) "
                            "Chrome/124.0.0.0 Safari/537.36"
                        ),
                        locale="en-US",
                        timezone_id="America/New_York",
                        extra_http_headers={
                            "Accept-Language": "en-US,en;q=0.9",
                        },
                    )
                    # 强制 USD + 英文：Eldorado 按 IP 自动切货币（如服务器在新加坡会变 SGD），
                    # 注入货币偏好 cookie 让页面与接口都返回 USD。
                    await eld_context.add_cookies([
                        {
                            "name": "eldoradogg_currencyPreference",
                            "value": "USD",
                            "domain": "www.eldorado.gg",
                            "path": "/",
                        },
                        {
                            "name": "eldoradogg_locale",
                            "value": "en-US",
                            "domain": "www.eldorado.gg",
                            "path": "/",
                        },
                    ])
                    await eld_context.add_init_script("""
                        Object.defineProperty(navigator, 'webdriver', { get: () => undefined });
                        Object.defineProperty(navigator, 'languages', { get: () => ['en-US', 'en'] });
                        Object.defineProperty(navigator, 'plugins', { get: () => [1,2,3,4,5] });
                        window.chrome = { runtime: {}, loadTimes: function(){}, csi: function(){}, app: {} };
                        const origQuery = window.navigator.permissions.query;
                        window.navigator.permissions.query = (p) =>
                            p.name === 'notifications'
                                ? Promise.resolve({ state: Notification.permission })
                                : origQuery(p);
                    """)
                    eld_page = await eld_context.new_page()
                    try:
                        items = await scrape_eldorado_page(eld_page, url)
                    finally:
                        await eld_context.close()
                elif category in {"金币", "游戏币"}:
                    items = await scrape_other_offer_page(page, url)
                else:
                    items = await scrape_page(page, url)
                print(f"  -> 提取 {len(items)} 条")

                inserted = db.save_crawl_data(
                    target_id,
                    platform,
                    items,
                    game_product_id=game_product_id,
                    version=version,
                )
                total_saved += inserted
                print(f"  -> 新增 {inserted} 条")

                db.update_last_crawl(target_id)
                # 发信号通知 PHP：该目标已爬完，由 PHP 消费通知后执行改价策略
                db.insert_crawl_notify(target_id, version, inserted)
                print(f"  -> 已写入爬取完成通知(crawl_notify)")
            except Exception as e:
                failed_count += 1
                print(f"  -> 错误: {e}")

        await browser.close()
        print(f"\n[*] 分片完成! 共保存 {total_saved} 条数据，失败 {failed_count} 个目标")
        return 1 if failed_count else 0


def parse_args(argv=None):
    parser = argparse.ArgumentParser(description="从数据库分片爬取目标")
    parser.add_argument("--worker-count", type=int, default=1, help="总进程数，默认 1")
    parser.add_argument("--worker-index", type=int, default=0, help="当前进程编号，从 0 开始")
    args = parser.parse_args(argv)
    if args.worker_count < 1:
        parser.error("--worker-count 必须大于 0")
    if not 0 <= args.worker_index < args.worker_count:
        parser.error("--worker-index 必须在 [0, worker-count) 内")
    return args


if __name__ == "__main__":
    args = parse_args()
    raise SystemExit(asyncio.run(run(args.worker_index, args.worker_count)))
