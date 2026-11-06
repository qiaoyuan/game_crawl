"""
从数据库 crawl_target 表读取 URL 列表，批量爬取并保存到 crawl_data 表
用法: python -m tools.crawl_from_db
生产独立调度: bash run_crawl_and_consume.sh（进程数读取 CRAWL_WORKER_COUNT）
单轮分片: python -m tools.crawl_from_db --worker-count 2 --worker-index 0
"""

import argparse
import asyncio
import json
import math
import re
import time
from contextlib import AsyncExitStack, contextmanager
from datetime import datetime
from decimal import Decimal, InvalidOperation
from time import monotonic, perf_counter
from playwright.async_api import TimeoutError as PlaywrightTimeoutError, async_playwright
from g2g import config, db
from g2g.crawl_filter import compile_policies, identifiers, number, select_top3

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


def store_is_excluded(seller_id, seller_name, excluded_stores):
    return bool(excluded_stores & identifiers([seller_id, seller_name]))


def filter_excluded_stores(items):
    """先排除配置中的店铺，不对它们处理报价或选择详情加强。"""
    excluded_stores = identifiers(config.CRAWL_EXCLUDE_STORES)
    if not excluded_stores:
        return items
    remaining = [item for item in items if not store_is_excluded(
        item.get("seller_id"), item.get("seller_name"), excluded_stores
    )]
    if len(remaining) < len(items):
        print(f"  -> 全局排除店铺: 跳过 {len(items) - len(remaining)} 条")
    return remaining


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


@contextmanager
def timed_stage(label):
    """记录阶段开始及成功/失败耗时，停滞时也能看到当前阶段。"""
    print(f"  -> 阶段开始: {label}", flush=True)
    started = perf_counter()
    try:
        yield
    except BaseException:
        print(f"  -> 阶段失败: {label}，耗时: {perf_counter() - started:.2f} 秒", flush=True)
        raise
    else:
        print(f"  -> 阶段完成: {label}，耗时: {perf_counter() - started:.2f} 秒", flush=True)


G2G_CARD_COUNT_JS = "() => document.querySelectorAll('[aria-label=\"Product Card\"]').length"
G2G_CARD_READINESS_JS = r"""
    limit => {
        const cards = Array.from(document.querySelectorAll('[aria-label="Product Card"]')).slice(0, limit);
        let ready = 0;
        for (const card of cards) {
            const sellerName = card.querySelector('.truncate.text-xs.font-medium');
            const sellerLink = card.querySelector('a[href*="g2g.com/"]:not([href*="offer"]):not([href*="categories"])');
            const sellerId = sellerLink && /g2g\.com\/[A-Za-z0-9_-]+$/.test(sellerLink.getAttribute('href') || '');
            const price = card.querySelector('.text-base.font-bold');
            const hasSeller = sellerId || (sellerName && sellerName.innerText.trim());
            if (hasSeller && price && /\d/.test(price.innerText)) ready++;
        }
        return {count: cards.length, ready};
    }
"""


async def wait_for_g2g_card_fields(page, offer_limit, timeout_ms=3000):
    """卡片价格和卖家身份齐全即可读取；超时保留原有缺字段提取行为。"""
    state = await page.evaluate(G2G_CARD_READINESS_JS, offer_limit)
    if state["count"] == state["ready"]:
        return state
    try:
        handle = await page.wait_for_function(
            "limit => { const state = (" + G2G_CARD_READINESS_JS + ")(limit); "
            "return state.count > 0 && state.count === state.ready ? state : false; }",
            arg=offer_limit, polling=100, timeout=timeout_ms,
        )
        try:
            return await handle.json_value()
        finally:
            await handle.dispose()
    except PlaywrightTimeoutError:
        state = await page.evaluate(G2G_CARD_READINESS_JS, offer_limit)
        print(
            f"  [!] G2G 卡片字段等待达到 {timeout_ms / 1000:g} 秒，"
            f"就绪 {state['ready']}/{state['count']}，继续按现有规则提取"
        )
        return state


async def scrape_page(page, url: str) -> list:
    """爬取单个页面的产品卡片"""
    offer_limit = config.CRAWL_OFFER_LIMIT
    print(f"  [*] 打开: {url}")
    with timed_stage("G2G 页面导航"):
        await page.goto(url, wait_until="domcontentloaded", timeout=60000)

    # 兜底：如果 g2g_regional cookie 货币不是 USD，用 JS 设 cookie 后刷新
    with timed_stage("G2G 货币检查"):
        currency_check = await page.evaluate("""() => {
            const m = document.cookie.match(/g2g_regional=([^;]+)/);
            if (m) {
                try { return JSON.parse(decodeURIComponent(m[1])).currency; } catch(_) {}
            }
            return null;
        }""")
    if currency_check and currency_check != "USD":
        print(f"  [!] 检测到货币 {currency_check}，强制切换为 USD 并刷新...")
        with timed_stage("G2G USD 切换与重新导航"):
            await page.evaluate("""() => {
                document.cookie = 'g2g_regional=%7B%22country%22%3A%22US%22%2C%22language%22%3A%22en%22%2C%22currency%22%3A%22USD%22%7D; path=/; domain=.g2g.com; max-age=86400';
            }""")
            await page.goto(url, wait_until="domcontentloaded", timeout=60000)

    # 等待卡片加载
    with timed_stage("G2G 首批卡片加载"):
        for i in range(60):
            count = await page.evaluate(G2G_CARD_COUNT_JS)
            if count > 0:
                break
            await asyncio.sleep(1)

    # 滚动加载
    with timed_stage("G2G 列表数量检查与滚动加载"):
        prev_count = 0
        scroll_count = 0
        for s in range(10):
            count = await page.evaluate(G2G_CARD_COUNT_JS)
            if count >= offer_limit:
                break
            if count == prev_count:
                break
            prev_count = count
            await page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
            scroll_count += 1
            await asyncio.sleep(2)
        print(f"  -> G2G 列表检查: 卡片 {count}，上限 {offer_limit}，滚动 {scroll_count} 次")

    # 数量确定后检查最终候选，包含滚动中新加载的卡片。
    with timed_stage("G2G 卡片字段就绪等待（最多 3 秒）"):
        state = await wait_for_g2g_card_fields(page, offer_limit)
        print(f"  -> G2G 卡片字段: 就绪 {state['ready']}/{state['count']}")

    # ========== 先提取页面级别货币（全局指示器） ==========
    with timed_stage("G2G 页面货币解析"):
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
    with timed_stage("G2G 卡片数据提取"):
        items_raw = await page.evaluate("""
            limit => {
                const VALID_CURRENCY_CODES = new Set([
                    'USD','SGD','EUR','GBP','AUD','CAD','JPY','CNY','HKD','MYR','KRW','INR','THB','PHP','IDR','CHF','NZD','BRL','SEK','NOK','DKK','PLN','AED','SAR'
                ]);

                const cards = Array.from(document.querySelectorAll('[aria-label="Product Card"]')).slice(0, limit);
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
            }
        """, offer_limit)

    # ========== Python 侧货币解析：优先从页面实际内容提取 ==========
    with timed_stage("G2G 数据过滤与价格解析"):
        items = []
        for raw in filter_excluded_stores(items_raw):
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


DETAIL_UNIT_PRICE_SELECTOR = (
    ".vue-portal-target .pricing-container .text-center.text-body-2.g-mt-12"
)


def parse_detail_unit_price(text):
    """只解析详情标注的单价，不接受列表最低价或订单总价。"""
    match = re.match(
        r"^\s*(?:单价|Unit\s*price)\s*([\d,]+(?:\.\d+)?)\s*([A-Z]{3})\b",
        text, re.IGNORECASE,
    )
    value = number(match[1].replace(",", "")) if match else None
    if value is None or value <= 0:
        raise ValueError(f"详情单价无效: {text!r}")
    return match[1].replace(",", ""), match[2].upper()


async def refresh_other_offer_prices(page, items, timeout_ms=15000, enhance_stores=None, policies=None):
    """逐店查看详情；任何单价未确认时中止目标，避免用旧价生成改价通知。"""
    stores = identifiers(enhance_stores)
    if stores:
        candidates = [item for item in items if stores & identifiers(
            [item.get("seller_id"), item.get("seller_name")]
        )]
    else:
        # 空名单按列表报价选策略 Top3，仅决定加强对象，不裁剪入库候选。
        candidates = select_top3(items, policies or [])
    print(f"  -> 店铺加强: {len(items)} 条中需查看 {len(candidates)} 家")
    started = perf_counter()
    for index, item in enumerate(candidates, 1):
        seller_id = item.get("seller_id")
        print(f"  -> 加强 [{index}/{len(candidates)}] {seller_id}: 开始定位", flush=True)
        stage = ["定位店铺"]
        try:
            # DOM 读取、点击及句柄清理也计入同一个期限，避免局部超时之外无限等待。
            await asyncio.wait_for(
                _refresh_other_offer_price(page, item, timeout_ms, stage),
                timeout=timeout_ms / 1000,
            )
        except Exception as error:
            reason = f"总等待超过 {timeout_ms / 1000:g}s" if isinstance(error, asyncio.TimeoutError) else str(error)
            raise ValueError(f"加强店铺 {seller_id} 详情单价刷新失败（{stage[0]}）: {reason}") from error
    print(f"  -> 店铺详情刷新: {len(candidates)} 家，耗时 {perf_counter() - started:.1f}s")
    return items


async def _refresh_other_offer_price(page, item, timeout_ms, stage):
    seller_id = item.get("seller_id")
    if not seller_id:
        raise ValueError("加强店铺缺少 seller_id，不能确认详情单价归属")
    seller_path = "/" + seller_id
    card = page.locator("#pcOtherOffer .other-seller--gradient").filter(
        has=page.locator(f"a[href={json.dumps(seller_path, ensure_ascii=False)}]")
    )
    if await card.count() != 1:
        raise ValueError(f"无法唯一定位加强店铺: {seller_id}")
    # 数量为 1 时 G2G 不渲染单价节点，记录详情金额节点以校验异步刷新。
    previous = await page.query_selector(
        DETAIL_UNIT_PRICE_SELECTOR + ", .vue-portal-target .pricing-container #final-price"
    )
    try:
        previous_text = await previous.text_content() if previous else ""
        selected = await page.evaluate("""path => Array.from(
            document.querySelectorAll('#pcMain a.g-card-no-deco.cursor-pointer')
        ).some(a => new URL(a.href).pathname.toLowerCase() === path.toLowerCase())
        """, seller_path)
        stage[0] = "点击查看"
        print(f"  -> 加强 {seller_id}: 点击查看", flush=True)
        await card.get_by_role("button", name=re.compile(r"^(查看|View)$", re.I)).click(
            timeout=max(1, timeout_ms - 100), no_wait_after=True,
        )
        stage[0] = "等待店铺及详情价格刷新"
        print(f"  -> 加强 {seller_id}: 等待详情价格", flush=True)
        result = await page.wait_for_function("""state => {
            const seller = Array.from(document.querySelectorAll(
                '#pcMain a.g-card-no-deco.cursor-pointer'
            )).some(a => new URL(a.href).pathname.toLowerCase() === state.path.toLowerCase());
            if (!seller) return false;
            const visible = el => el && el.getClientRects().length;
            const prices = Array.from(document.querySelectorAll(state.selector))
                .filter(el => visible(el) && /^(单价|Unit\\s*price)/i.test(el.innerText.trim()));
            let price, text, source;
            if (prices.length === 1) {
                price = prices[0];
                text = price.innerText.trim();
                source = 'offer_detail';
            } else if (prices.length === 0) {
                const containers = Array.from(document.querySelectorAll('.vue-portal-target .pricing-container'))
                    .filter(visible);
                if (containers.length !== 1) return false;
                const container = containers[0];
                const inputs = container.querySelectorAll('input');
                // 只接受确定的一件金额，不能把多件总价或其他金额当作单价。
                if (inputs.length !== 1 || !/^1(?:\\.0+)?$/.test(inputs[0].value.trim())) return false;
                price = container.querySelector('#final-price');
                const currency = price && price.nextElementSibling;
                if (!visible(price) || !visible(currency)) return false;
                const label = price.parentElement.previousElementSibling;
                if (!label || !/^(总金额|总价|Total\\s*amount)$/i.test(label.innerText.trim())) return false;
                text = '单价 ' + price.innerText.trim() + ' ' + currency.innerText.trim();
                source = 'offer_detail_single_unit';
            } else return false;
            const refreshed = state.selected || price !== state.previous
                || price.textContent.trim() !== state.text.trim();
            return refreshed && {text, source};
        }""", arg={"path": seller_path, "selector": DETAIL_UNIT_PRICE_SELECTOR,
                    "previous": previous, "text": previous_text, "selected": selected},
            timeout=max(1, timeout_ms - 100))
        try:
            detail = await result.json_value()
            price, currency = parse_detail_unit_price(detail["text"])
        finally:
            await result.dispose()
        item.update(price=price, unit_price=price, currency=currency,
                    price_label="单价", unit_price_source=detail["source"])
        print(f"  -> 店铺 {seller_id} 详情单价: {price} {currency} ({detail['source']})", flush=True)
    finally:
        if previous and not asyncio.current_task().cancelling():
            await previous.dispose()


async def scrape_other_offer_page(page, url: str, refresh_unit_prices=False, enhance_stores=None, policies=None) -> list:
    """爬取游戏币分类页 #pcOtherOffer 下的竞品商户卡片"""
    offer_limit = config.CRAWL_OFFER_LIMIT
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

    # 达到抓取上限即停止等待；不足时才尝试滚动加载。
    previous_count = -1
    stable_rounds = 0
    for _ in range(10):
        count = len(await page.query_selector_all(card_selector))
        if count >= offer_limit:
            break
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
        limit => {
            const container = document.querySelector('#pcOtherOffer');
            if (!container) return [];

            const cards = Array.from(container.querySelectorAll('.other-seller--gradient')).slice(0, limit);
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
        """, offer_limit,
    )

    items = []
    valid_currency_codes = {
        "USD", "SGD", "EUR", "GBP", "AUD", "CAD", "JPY", "CNY", "HKD",
        "MYR", "KRW", "INR", "THB", "PHP", "IDR", "CHF", "NZD", "BRL",
    }
    for raw in filter_excluded_stores(items_raw):
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

    if refresh_unit_prices:
        return await refresh_other_offer_prices(page, items, enhance_stores=enhance_stores, policies=policies)
    return items


def sort_eldorado_prices(items):
    """按真实 USD 单价数值稳定升序；无效报价不能当作最低价入库。"""
    def price_key(item):
        try:
            price = Decimal(str(item.get("price")))
        except InvalidOperation as error:
            raise ValueError("ELD 单价无效") from error
        if item.get("currency") != "USD" or not price.is_finite() or price <= 0:
            raise ValueError("ELD 单价必须是有限正数且币种为 USD")
        return price

    return sorted(items, key=price_key)


def eldorado_cheapest_url(url):
    """DOM 兜底同样使用最低价第一页，保留商品和区服筛选。"""
    from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

    parts = urlsplit(url)
    query = [(key, value) for key, value in parse_qsl(parts.query, keep_blank_values=True)
             if key not in {"offerSortingCriterion", "pageIndex", "pageSize", "page", "page_size"}]
    query.extend((("offerSortingCriterion", "Cheapest"), ("pageIndex", "1")))
    return urlunsplit(parts._replace(query=urlencode(query)))


async def scrape_eldorado_api(request, url: str) -> list | None:
    """直接请求 offers API；None 表示需要 DOM 兜底，空列表是有效结果。"""
    from urllib.parse import parse_qs, quote, urlencode, urlparse

    offer_limit = config.ELD_OFFER_PAGE_SIZE

    print(f"  [*] 请求 Eldorado 最低价前 {offer_limit} 条: {url}")

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
        # 属性筛选透传，强制最低价排序；分页参数不透传，固定第一页，条数由 ELD_OFFER_PAGE_SIZE 控制。
        # 否则会抓成该游戏的全量报价而不是当前筛选的道具。
        passthrough = []
        for key, values in sorted(query.items()):
            if not values:
                continue
            env_match = re.fullmatch(r"te_v(\d+)", key)
            if env_match:
                params.append((f"tradeEnvironmentValue{env_match.group(1)}", values[0]))
            elif key not in {"offerSortingCriterion", "pageIndex", "pageSize", "page", "page_size"}:
                passthrough.append((key, values[0]))
        params.extend([
            ("pageIndex", "1"),
            ("pageSize", str(offer_limit)),
            ("offerSortingCriterion", "Cheapest"),
        ])
        params.extend(passthrough)
        api_url = (
            "https://www.eldorado.gg/api/predefinedOffers/augmentedGame/offers?"
            + urlencode(params)
        )
        try:
            with timed_stage("ELD API 请求（最低价第一页）"):
                api_response = await request.get(
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
            excluded_stores = identifiers(config.CRAWL_EXCLUDE_STORES)
            excluded_count = 0
            # 最低价候选由 API 的 Cheapest 排序选出；限定范围后排除店铺，不向后补位。
            for record in payload.get("results", [])[:offer_limit]:
                offer = record.get("offer") or {}
                user = record.get("user") or {}
                username = str(user.get("username") or "").strip()
                seller_id = user.get("id") or offer.get("userId") or username
                if store_is_excluded(seller_id, username, excluded_stores):
                    excluded_count += 1
                    continue
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
                                "amount": Decimal(str(local_amount)) / Decimal(str(rate)),
                                "currency": "USD",
                            }
                        except (TypeError, ValueError, InvalidOperation, ZeroDivisionError):
                            price_info = {}
                    else:
                        price_info = {}
                unit = unit_label(offer.get("unitSystem"))
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
                    "seller_id": seller_id,
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

            if excluded_count:
                print(f"  -> 全局排除店铺: 跳过 {excluded_count} 条")
            if api_items or excluded_count:
                api_items = sort_eldorado_prices(api_items)
                print(
                    f"  -> Eldorado API 最低价第一页，按单价升序返回 {len(api_items)} 条 "
                    f"(价格使用 USD: {sum(i.get('currency') == 'USD' for i in api_items)} 条)"
                )
                return api_items
    except Exception as e:
        print(f"  [!] Eldorado API 数据解析失败，回退 DOM 解析: {e}")
    finally:
        if api_response is not None:
            await api_response.dispose()

    return None


async def scrape_eldorado_page(page, url: str) -> list:
    """兼容已有页面调用，生产入口可直接请求 API 而不创建页面。"""
    items = await scrape_eldorado_api(page.request, url)
    if items is not None:
        return items
    return await scrape_eldorado_dom(page, url)


async def scrape_eldorado_dom(page, url: str) -> list:
    """API 不可用时才加载 Eldorado 页面。"""
    offer_limit = config.ELD_OFFER_PAGE_SIZE
    url = eldorado_cheapest_url(url)

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

    # 第一页够 8 条后不再滚动加载；不足时等首屏渲染稳定。
    previous_count = -1
    stable_rounds = 0
    for _ in range(10):
        count = len(await page.query_selector_all(card_selector))
        if count >= offer_limit:
            break
        if count == previous_count:
            stable_rounds += 1
        else:
            stable_rounds = 0
        if stable_rounds >= 2:
            break
        previous_count = count
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
        limit => {
            const rows = Array.from(document.querySelectorAll('#other-sellers .offer-row')).slice(0, limit);
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
    """, offer_limit)

    items = []
    for raw in filter_excluded_stores(items_raw):
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

    return sort_eldorado_prices(items)


def crawl_interval_deadline(target, label):
    """将本轮任务快照的开始间隔转换为绝对到期时间；0 表示立即可执行。"""
    last_crawl_at = target.get("last_crawl_at")
    try:
        interval_seconds = max(0, int(target.get("crawl_interval") or 0))
        if not interval_seconds or not last_crawl_at:
            return 0, interval_seconds
        last_started_at = (
            last_crawl_at if isinstance(last_crawl_at, datetime)
            else datetime.strptime(str(last_crawl_at), "%Y-%m-%d %H:%M:%S")
        )
        return last_started_at.timestamp() + interval_seconds, interval_seconds
    except (TypeError, ValueError, OverflowError, OSError) as e:
        print(f"  {label} 间隔时间解析异常: {e}，继续执行")
        return 0, 0


async def wait_for_crawl_interval(target, label):
    """单目标兼容入口；生产轮次由 iter_scheduled_targets 避免队列头部等待。"""
    deadline, interval_seconds = crawl_interval_deadline(target, label)
    remaining = deadline - time.time()

    if remaining <= 0:
        return True
    # cron 每 30 秒触发一次：最多等一个调度周期，不提前爬取。
    if remaining > 30:
        print(
            f"  {label} 跳过: 未到间隔时间，还需等待约 {math.ceil(remaining)} 秒"
            f"（开始间隔 {interval_seconds} 秒）"
        )
        return False
    print(
        f"  {label} 等待约 {remaining:.1f} 秒后执行"
        f"（开始间隔 {interval_seconds} 秒）"
    )
    await asyncio.sleep(remaining)
    return True


async def iter_scheduled_targets(targets, total):
    """优先执行到期目标；只有队列无到期目标时才等待最近到期者。"""
    pending = []
    for idx, target in targets:
        label = f"[{idx+1}/{total}] {target.get('name', '')}"
        deadline, interval = crawl_interval_deadline(target, label)
        pending.append((idx, target, label, deadline, interval))

    while pending:
        now = time.time()
        ready = next((entry for entry in pending if entry[3] <= now), None)
        if ready is not None:
            if ready is not pending[0]:
                print(f"  {ready[2]} 调度: 优先执行已到期任务，未到期任务延后")
            pending.remove(ready)
            yield ready[0], ready[1]
            continue

        nearest = min(pending, key=lambda entry: (entry[3], entry[0]))
        remaining = nearest[3] - now
        if remaining > 30:
            for _, _, label, deadline, interval in pending:
                print(
                    f"  {label} 跳过: 未到间隔时间，还需等待约 {math.ceil(deadline - now)} 秒"
                    f"（开始间隔 {interval} 秒）"
                )
            return

        print(
            f"  {nearest[2]} 等待约 {remaining:.1f} 秒后执行"
            f"（当前无已到期任务，开始间隔 {nearest[4]} 秒）"
        )
        await asyncio.sleep(remaining)
        # 等待可能被提前唤醒，重新核对到期时间，保证不提前抓取。


ELDORADO_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/124.0.0.0 Safari/537.36"
)
ELDORADO_COOKIES = [
    {
        "name": name, "value": value, "domain": "www.eldorado.gg", "path": "/",
        "expires": -1, "httpOnly": False, "secure": False, "sameSite": "Lax",
    }
    for name, value in (
        ("eldoradogg_currencyPreference", "USD"),
        ("eldoradogg_locale", "en-US"),
    )
]


class CrawlSession:
    """每个 worker 按需创建并复用请求/浏览器，退出时统一释放。"""

    def __init__(self):
        self.resources = AsyncExitStack()
        self.playwright = None
        self.browser = None
        self.g2g_context = None
        self.page = None
        self.browser_cleanup_pending = False
        self.eldorado_request = None

    async def __aenter__(self):
        await self.resources.__aenter__()
        return self

    async def __aexit__(self, *args):
        try:
            await self._close_browser()
        finally:
            # 即使关闭期间被取消，也继续释放 request / Playwright driver。
            result = await self.resources.__aexit__(*args)
        return result

    async def _try_close(self, resource, label):
        try:
            await asyncio.wait_for(resource.close(), timeout=3)
            return True
        except Exception as error:
            print(f"  [!] {label}关闭失败: {type(error).__name__}: {error}", flush=True)
            return False

    async def _close_g2g_context(self):
        context, self.g2g_context = self.g2g_context, None
        self.page = None
        if context is None:
            return True
        return await self._try_close(context, "G2G 上下文")

    async def _close_browser(self):
        await self._close_g2g_context()
        if self.browser is None:
            self.browser_cleanup_pending = False
            return True
        browser = self.browser
        closed = await self._try_close(browser, "Chromium")
        if closed or browser.is_connected() is False:
            self.browser = None
            self.browser_cleanup_pending = False
            return True
        # 保留唯一的待回收 browser，下次清理成功前不再叠加新浏览器。
        self.browser_cleanup_pending = True
        return False

    async def get_playwright(self):
        if self.playwright is None:
            with timed_stage("Playwright 驱动启动"):
                self.playwright = await self.resources.enter_async_context(async_playwright())
        return self.playwright

    async def get_browser(self, reason):
        if self.browser is not None and (
            self.browser_cleanup_pending or self.browser.is_connected() is False
        ):
            if not await self._close_browser():
                raise RuntimeError("旧 Chromium 尚未回收，暂不创建新浏览器")
        if self.browser is None:
            playwright = await self.get_playwright()
            print(f"  -> 按需启动 Chromium（{reason}）")
            with timed_stage("Chromium 启动"):
                self.browser = await playwright.chromium.launch(**config.browser_launch_kwargs())
        return self.browser

    async def get_g2g_page(self):
        browser = await self.get_browser("G2G 页面抓取")
        if self.page is None:
            with timed_stage("G2G 上下文与页面初始化"):
                context = await browser.new_context(
                    viewport=config.VIEWPORT,
                    user_agent=config.USER_AGENT,
                )
                self.g2g_context = context
                try:
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
                    self.page = await context.new_page()
                except BaseException:
                    if not await self._close_g2g_context():
                        await self._close_browser()
                    raise
        return self.page

    async def park_g2g_page(self):
        if self.page is None:
            return
        try:
            with timed_stage("G2G 页面暂停"):
                await asyncio.wait_for(
                    self.page.goto("about:blank", wait_until="commit", timeout=3000), timeout=4)
        except Exception as error:
            print(f"  [!] G2G 页面暂停失败，关闭上下文后下次重建: {error}", flush=True)
            if not await self._close_g2g_context():
                await self._close_browser()

    async def scrape_eldorado(self, url):
        if self.eldorado_request is None:
            playwright = await self.get_playwright()
            self.eldorado_request = await playwright.request.new_context(
                user_agent=ELDORADO_USER_AGENT,
                extra_http_headers={"Accept-Language": "en-US,en;q=0.9"},
                storage_state={"cookies": ELDORADO_COOKIES, "origins": []},
            )
            self.resources.push_async_callback(self.eldorado_request.dispose)
            print("  -> ELD API 请求模式（成功时不启动 Chromium）")
        items = await scrape_eldorado_api(self.eldorado_request, url)
        if items is not None:
            return items

        browser = await self.get_browser("ELD API 不可用，DOM 兜底")
        context = await browser.new_context(
            viewport=config.VIEWPORT,
            user_agent=ELDORADO_USER_AGENT,
            locale="en-US",
            timezone_id="America/New_York",
            extra_http_headers={"Accept-Language": "en-US,en;q=0.9"},
        )
        try:
            # 每个 DOM 兜底目标使用独立 context，不带 G2G cookie。
            await context.add_cookies(ELDORADO_COOKIES)
            await context.add_init_script("""
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
            page = await context.new_page()
            return await scrape_eldorado_dom(page, url)
        finally:
            await context.close()


def valid_crawl_targets(targets):
    valid_targets = []
    for idx, target in enumerate(targets):
        try:
            game_product_id = int(target.get("game_product_id") or 0)
        except (TypeError, ValueError):
            game_product_id = 0
        url = target.get("url")
        name = target.get("name", "")

        if game_product_id <= 0:
            print(
                f"  [{idx+1}/{len(targets)}] {name} 跳过: "
                f"未关联游戏产品 (game_product_id={game_product_id})"
            )
            continue

        if not url:
            print(f"  [{idx+1}/{len(targets)}] 跳过: 无 URL")
            continue

        valid_targets.append((idx, target))

    return valid_targets


async def crawl_target(session, idx, target, total):
    target_id = target.get("id")
    game_product_id = int(target.get("game_product_id"))
    url = target.get("url")
    name = target.get("name", "")
    category = str(target.get("category") or "").strip()

    platform = "eldorado" if "eldorado.gg" in url else "g2g"
    category_label = category or "未设置类别"
    print(
        f"\n[{idx+1}/{total}] {name} "
        f"(target_id={target_id}, game_product_id={game_product_id}, "
        f"category={category_label})"
    )

    target_started = perf_counter()
    inserted = 0
    try:
        # 1 优先按绑定店铺加强，空名单按策略 Top3 加强；均保存全部候选。
        crawl_type = target.get("crawl_type") or 0
        enhance_enabled = crawl_type in (1, "1", "top3")
        if not enhance_enabled and crawl_type not in (0, "0", "default"):
            raise ValueError(f"未知爬虫类型: {crawl_type}")
        enhance_stores = target.get("enhance_stores") or ""
        enhance_policies = None
        if enhance_enabled and category in {"金币", "游戏币"} and not identifiers(enhance_stores):
            enhance_policies = compile_policies(db.get_crawl_strategies(target_id))

        # 爬取前先将版本号 +1，本批数据全部使用新版本号写入。
        # PHP 侧通过 crawl_target.version 对应 crawl_data.version 做改价策略。
        started_wall, started_mono = time.time(), monotonic()
        crawl_started_at = datetime.fromtimestamp(started_wall).replace(microsecond=0)
        version = db.increment_version(target_id)
        print(f"  -> version={version}, 开始时间={crawl_started_at}")

        # 按 category 分流到不同爬取函数
        if category == "ELD游戏币":
            items = await session.scrape_eldorado(url)
        elif category in {"金币", "游戏币"}:
            page = await session.get_g2g_page()
            if enhance_enabled:
                items = await scrape_other_offer_page(
                    page, url, refresh_unit_prices=True,
                    enhance_stores=enhance_stores, policies=enhance_policies,
                )
            else:
                items = await scrape_other_offer_page(page, url)
        else:
            page = await session.get_g2g_page()
            items = await scrape_page(page, url)
        print(f"  -> 提取 {len(items)} 条")

        inserted = db.save_crawl_data(
            target_id,
            platform,
            items,
            game_product_id=game_product_id,
            version=version,
        )
        print(f"  -> 新增 {inserted} 条")

        db.update_last_crawl(target_id, crawl_started_at)
        # 发信号通知 PHP：该目标已爬完，由 PHP 消费通知后执行改价策略
        db.insert_crawl_notify(target_id, version, inserted)
        print(f"  -> 已写入爬取完成通知(crawl_notify)")
        print(f"  -> 目标完成，耗时: {perf_counter() - target_started:.2f} 秒")
        return inserted, False, started_wall, started_mono
    except Exception as e:
        print(f"  -> 错误: {e}（目标耗时: {perf_counter() - target_started:.2f} 秒）")
        return inserted, True, None, None
    finally:
        if category != "ELD游戏币":
            await session.park_g2g_page()


async def run(worker_index: int = 0, worker_count: int = 1):
    """兼容一次性执行；生产脚本使用 run_scheduled。"""
    run_started = perf_counter()
    targets = db.get_pending_targets(worker_index, worker_count)
    print(
        f"[*] crawl_server={config.CRAWL_SERVER}, worker {worker_index + 1}/{worker_count}: "
        f"从数据库读取到 {len(targets)} 个爬取目标"
    )
    if not targets:
        print("[*] 当前分片没有爬取目标")
        return 0
    total_saved = failed_count = 0
    async with CrawlSession() as session:
        async for idx, target in iter_scheduled_targets(valid_crawl_targets(targets), len(targets)):
            saved, failed, _, _ = await crawl_target(session, idx, target, len(targets))
            total_saved += saved
            failed_count += failed
    print(
        f"\n[*] 分片完成! 共保存 {total_saved} 条数据，失败 {failed_count} 个目标，"
        f"worker 耗时: {perf_counter() - run_started:.2f} 秒"
    )
    return 1 if failed_count else 0


class RepeatingSchedule:
    """最早到期优先；仅供一个串行 worker 使用，互斥仍由启动脚本负责。"""

    def __init__(self, worker_index, worker_count, run_seconds):
        self.worker_index = worker_index
        self.worker_count = worker_count
        self.ends_at = monotonic() + run_seconds
        self.refresh_at = float("-inf")
        self.states = {}
        self.total = 0
        self.metadata_valid = False
        self.refresh_failures = 0

    def refresh(self):
        # 每次读取使用短连接；next_target 在查询失败期间暂停使用旧快照。
        targets = db.get_pending_targets(self.worker_index, self.worker_count)
        now, wall_now = monotonic(), time.time()
        previous_ids = set(self.states)
        states = {}
        for idx, target in valid_crawl_targets(targets):
            target_id = target["id"]
            state = self.states.get(target_id, {"first_due": now, "retry_at": 0})
            # 无间隔的任务沿用 cron 的基础 30 秒周期，避免驻留模式忙循环。
            effective = dict(target)
            try:
                interval = max(0, int(target.get("crawl_interval") or 0)) or 30
            except (TypeError, ValueError, OverflowError):
                interval = 30
            effective["crawl_interval"] = interval
            deadline, _ = crawl_interval_deadline(effective, f"target_id={target_id}")
            due = now + deadline - wall_now if deadline else state["first_due"]
            if "success_mono" in state:
                local_due = state["success_mono"] + interval
                # 自己已写入的时间（或更旧快照）使用单调时钟，墙钟回拨也不延迟。
                # 外部更晚的成功记录仍生效，但不能缩短本地实际开始间隔。
                if not deadline or deadline - interval <= math.floor(state["success_wall"]):
                    due = local_due
                else:
                    due = max(due, local_due)
            state.update(target=target, idx=idx, interval=interval,
                         due=max(due, state["retry_at"]))
            states[target_id] = state
        self.states, self.total = states, len(targets)
        self.refresh_at = now + 5
        self.metadata_valid = True
        if previous_ids != set(states):
            print(f"[*] worker {self.worker_index + 1}/{self.worker_count} "
                  f"刷新任务: {len(states)} 个有效目标", flush=True)

    async def next_target(self):
        while monotonic() < self.ends_at:
            if monotonic() >= self.refresh_at:
                try:
                    self.refresh()
                except Exception as error:
                    self.metadata_valid = False
                    self.refresh_failures += 1
                    self.refresh_at = monotonic() + 30
                    print(f"  [!] 任务刷新失败，暂停抓取，30 秒后重试: {error}", flush=True)
            now = monotonic()
            if now >= self.ends_at:
                return None
            state = min(self.states.values() if self.metadata_valid else (),
                        key=lambda s: (s["due"], s["target"]["id"]),
                        default=None)
            if state is not None and state["due"] <= now:
                return state
            # 每次最多等到下次元数据刷新，新增/停用/调整间隔能被重新读取。
            wake_at = min(self.ends_at, self.refresh_at,
                          state["due"] if state is not None else self.ends_at)
            await asyncio.sleep(max(0, wake_at - now))
        return None

    def completed(self, state, failed, started_wall, started_mono):
        if failed:
            # 不更新数据库 last_crawl_at；退避仅在本窗口内有效。
            state["retry_at"] = monotonic() + 30
            state["due"] = state["retry_at"]
            print(f"  -> target_id={state['target']['id']} 失败退避 30 秒", flush=True)
        else:
            state["success_mono"] = started_mono
            state["success_wall"] = started_wall
            state["retry_at"] = 0
            state["due"] = started_mono + state["interval"]
        remaining = max(0, state["due"] - monotonic())
        print(f"  -> target_id={state['target']['id']} 下次最早约 {remaining:.1f} 秒后执行", flush=True)


async def run_scheduled(worker_index=0, worker_count=1, run_seconds=None):
    """有界驻留：窗口内各分片独立调度，结束后释放所有资源供下次启动。"""
    if run_seconds is None:
        run_seconds = config.CRAWL_WORKER_RUN_SECONDS
    if run_seconds < 0:
        raise ValueError("run_seconds 不能小于 0")
    if run_seconds == 0:
        return await run(worker_index, worker_count)
    started = perf_counter()
    schedule = RepeatingSchedule(worker_index, worker_count, run_seconds)
    total_saved = failed_count = attempted = 0
    print(f"[*] crawl_server={config.CRAWL_SERVER}, worker {worker_index + 1}/{worker_count}: "
          f"独立调度窗口 {run_seconds} 秒，默认周期 30 秒，任务刷新 5 秒", flush=True)
    async with CrawlSession() as session:
        while True:
            state = await schedule.next_target()
            if state is None:
                break
            saved, failed, started_wall, started_mono = await crawl_target(
                session, state["idx"], state["target"], schedule.total)
            attempted += 1
            total_saved += saved
            failed_count += failed
            schedule.completed(state, failed, started_wall, started_mono)
    print(f"[*] 调度窗口结束: 执行 {attempted} 次，保存 {total_saved} 条，"
          f"目标失败 {failed_count} 次，任务刷新失败 {schedule.refresh_failures} 次，"
          f"worker 耗时: {perf_counter() - started:.2f} 秒", flush=True)
    return 1 if failed_count or schedule.refresh_failures else 0


def parse_args(argv=None):
    parser = argparse.ArgumentParser(description="从数据库分片爬取目标")
    parser.add_argument("--worker-count", type=int, default=1, help="总进程数，默认 1")
    parser.add_argument("--worker-index", type=int, default=0, help="当前进程编号，从 0 开始")
    parser.add_argument("--scheduled", action="store_true", help="窗口内独立按到期时间调度并复用浏览器")
    args = parser.parse_args(argv)
    if args.worker_count < 1:
        parser.error("--worker-count 必须大于 0")
    if not 0 <= args.worker_index < args.worker_count:
        parser.error("--worker-index 必须在 [0, worker-count) 内")
    return args


if __name__ == "__main__":
    args = parse_args()
    entrypoint = run_scheduled if args.scheduled else run
    raise SystemExit(asyncio.run(entrypoint(args.worker_index, args.worker_count)))
