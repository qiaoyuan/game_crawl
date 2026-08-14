"""
从数据库 crawl_target 表读取 URL 列表，批量爬取并保存到 crawl_data 表
用法: python -m tools.crawl_from_db
"""

import asyncio
import json
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
        item.pop("unit_price_raw", None)
        item.pop("currency_label", None)
        items.append(item)

    return items


async def run():
    # 读取目标
    targets = db.get_pending_targets()
    print(f"[*] 从数据库读取到 {len(targets)} 个爬取目标")

    if not targets:
        print("[!] crawl_target 表为空")
        return

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
        for idx, target in enumerate(targets):
            target_id = target.get("id")
            game_product_id = target.get("game_product_id") or 0
            url = target.get("url")
            name = target.get("name", "")
            category = str(target.get("category") or "").strip()

            if not url:
                print(f"  [{idx+1}/{len(targets)}] 跳过: 无 URL")
                continue

            platform = "eldorado" if "eldorado.gg" in url else "g2g"
            category_label = category or "未设置类别"
            print(
                f"\n[{idx+1}/{len(targets)}] {name} "
                f"(target_id={target_id}, game_product_id={game_product_id}, "
                f"category={category_label})"
            )

            try:
                # “金币”和“游戏币”都是 /offer/group 分类页，使用 #pcOtherOffer 竞品商户卡片。
                if category in {"金币", "游戏币"}:
                    items = await scrape_other_offer_page(page, url)
                else:
                    items = await scrape_page(page, url)
                print(f"  -> 提取 {len(items)} 条")

                inserted, updated = db.save_crawl_data(
                    target_id,
                    platform,
                    items,
                    game_product_id=game_product_id,
                )
                total_saved += inserted + updated
                print(f"  -> 新增 {inserted} 条, 更新 {updated} 条")

                db.update_last_crawl(target_id)
                # 发信号通知 PHP：该目标已爬完，由 PHP 消费通知后执行改价策略
                db.insert_crawl_notify(target_id, inserted + updated)
                print(f"  -> 已写入爬取完成通知(crawl_notify)")
            except Exception as e:
                print(f"  -> 错误: {e}")

        await browser.close()
        print(f"\n[✓] 全部完成! 共保存 {total_saved} 条数据")


if __name__ == "__main__":
    asyncio.run(run())
