"""
爬取 G2G 产品卡片列表（无需登录）
用法: python -m tools.scrape_g2g_products [URL] [--scroll N]
"""

import asyncio
import json
import os
import re
import argparse
from datetime import datetime
from playwright.async_api import async_playwright
from g2g import config

DEFAULT_URL = (
    "https://www.g2g.com/cn/categories/torchlight-infinite-items"
    "?fa=lgc_31452_server%3Algc_31452_server_63229%7Clgc_31452_item_type%3Algc_31452_item_type_46842"
    "&sort=lowest_price"
)

ROOT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DOWNLOAD_DIR = os.path.join(ROOT_DIR, "downloads")

# 货币符号 → 货币代码映射（页面实际显示的货币）
CURRENCY_SYMBOL_MAP = {
    "S$": "SGD", "US$": "USD", "A$": "AUD", "C$": "CAD",
    "HK$": "HKD", "NZ$": "NZD",
    "$": "USD",
    "€": "EUR", "£": "GBP", "¥": "JPY", "CN¥": "CNY",
    "RM": "MYR", "₩": "KRW", "₹": "INR", "฿": "THB", "₱": "PHP",
    "Rp": "IDR", "R$": "BRL", "CHF": "CHF",
}


def parse_currency_from_price(price_text: str) -> tuple[str | None, str | None]:
    """从价格文本中解析货币代码和纯数字价格"""
    if not price_text:
        return None, None
    text = price_text.strip()
    sorted_symbols = sorted(CURRENCY_SYMBOL_MAP.keys(), key=len, reverse=True)
    for symbol in sorted_symbols:
        if text.startswith(symbol):
            price_str = text[len(symbol):].strip().replace(",", "")
            return CURRENCY_SYMBOL_MAP[symbol], price_str
    suffix_match = re.match(r"^([\d.,]+)\s*([A-Z]{3})$", text)
    if suffix_match:
        return suffix_match.group(2).upper(), suffix_match.group(1).replace(",", "")
    return None, text


async def scrape(url: str = DEFAULT_URL, max_scroll: int = 10):
    os.makedirs(DOWNLOAD_DIR, exist_ok=True)

    async with async_playwright() as p:
        browser = await p.chromium.launch(**config.browser_launch_kwargs())
        context = await browser.new_context(
            viewport={"width": 1280, "height": 800},
            user_agent=(
                "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/150.0.0.0 Safari/537.36"
            ),
        )
        await context.add_init_script("""
            Object.defineProperty(navigator, 'webdriver', { get: () => undefined });
            window.chrome = { runtime: {} };
        """)
        page = await context.new_page()

        print(f"[*] 打开页面...")
        await page.goto(url, wait_until="domcontentloaded", timeout=60000)

        # 等待 Product Card 加载
        print("[*] 等待产品卡片加载...")
        for i in range(60):
            cards = await page.query_selector_all('[aria-label="Product Card"]')
            if len(cards) > 0:
                print(f"  -> 检测到 {len(cards)} 个卡片 ({i+1}s)")
                break
            await asyncio.sleep(1)
        await asyncio.sleep(3)

        # ========== 页面级别货币检测 ==========
        page_currency = await page.evaluate("""
            (() => {
                const currencyBtn = document.querySelector('[data-testid="currency-selector"]')
                    || document.querySelector('[class*="currency"] button')
                    || document.querySelector('button[class*="Currency"]');
                if (currencyBtn) return currencyBtn.innerText.trim().split(/\\s+/)[0];
                const banners = document.body.innerText.match(/All prices (?:are )?in\\s+([A-Z]{3})/i);
                if (banners) return banners[1].toUpperCase();
                const currencyMatch = document.body.innerText.match(/(?:Prices? (?:in|are in)|Currency[：:]\\s*)([A-Z]{3})/i);
                if (currencyMatch) return currencyMatch[1].toUpperCase();
                return null;
            })()
        """)
        if page_currency:
            print(f"  -> 页面货币: {page_currency}")
        else:
            print(f"  -> 未检测到页面级别货币，将从每个卡片价格文本解析")

        # 滚动加载更多
        prev_count = 0
        for s in range(max_scroll):
            count = len(await page.query_selector_all('[aria-label="Product Card"]'))
            if count == prev_count:
                print(f"  -> 已加载全部 ({count} 个)")
                break
            print(f"  -> 滚动 {s+1}: {count} 个卡片")
            prev_count = count
            await page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
            await asyncio.sleep(2)

        # 提取数据
        items_raw = await page.evaluate("""
            (() => {
                const cards = document.querySelectorAll('[aria-label="Product Card"]');
                const results = [];
                cards.forEach(card => {
                    const item = {};
                    const sellerLink = card.querySelector('a[href*="g2g.com/"]:not([href*="offer"]):not([href*="categories"])');
                    if (sellerLink) {
                        const href = sellerLink.getAttribute('href') || '';
                        const match = href.match(/g2g\\.com\\/([A-Za-z0-9_-]+)$/);
                        if (match) item.seller_id = match[1];
                        item.seller_url = href;
                    }
                    const sellerNameEl = card.querySelector('.truncate.text-xs.font-medium');
                    if (sellerNameEl) item.seller_name = sellerNameEl.innerText.trim();
                    let levelEl = card.querySelector('[class*="text-\\\\[10px"]');
                    if (levelEl) item.seller_level = levelEl.innerText.trim();
                    const titleEl = card.querySelector('.line-clamp-2');
                    if (titleEl) item.product_title = titleEl.innerText.trim();

                    const chips = card.querySelectorAll('.h-chip__content');
                    chips.forEach(chip => {
                        const t = chip.innerText.trim();
                        if (!t) return;
                        if (/^\\d{1,3}%$/.test(t)) item.rating = t;
                        else if (/Sold|sold|\\u5df2\\u552e/.test(t)) item.sold_count = t;
                        else if (/^[\\d.,]+[kKmM]?$/.test(t)) item.stock = t;
                        else if (/^Min|^min|^\\u6700\\u5c0f/.test(t)) item.min_order = t;
                        else if (/\\d+\\s*(\\u5206\\u949f|\\u5c0f\\u65f6|Mins?|Hours?|Hr|Minute|Hour)/i.test(t)) item.delivery_time = t;
                    });

                    // 价格 & 货币 — 适配新旧两种卡片结构
                    const priceEl = card.querySelector('.text-base.font-bold');
                    if (priceEl) {
                        const priceText = priceEl.innerText.trim();
                        const symbolMatch = priceText.match(/^(US\\$|S\\$|A\\$|C\\$|HK\\$|NZ\\$|CN\\u00a5|RM|Rp|R\\$|\\u20ac|\\u00a3|\\u00a5|\\u20a9|\\u20b9|\\u0e3f|\\u20b1|CHF|\\$)?\\s*([\\d.,]+)/);
                        if (symbolMatch && symbolMatch[1] && symbolMatch[1].length > 0) {
                            item.price_raw = symbolMatch[2];
                            item.currency_from_price = symbolMatch[1];
                        } else {
                            item.price_raw = priceText;
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

        # ========== Python 侧货币解析 ==========
        items = []
        for raw in items_raw:
            item = {k: v for k, v in raw.items()}
            price_raw = raw.get("price_raw", "")
            currency_from_price = raw.get("currency_from_price", "")
            currency_label = raw.get("currency_label", "")

            parsed_currency = None
            if currency_from_price:
                cs = currency_from_price.strip()
                if cs.upper() in {"USD", "SGD", "EUR", "GBP", "AUD", "CAD", "JPY", "CNY", "HKD", "MYR", "KRW", "INR", "THB", "PHP", "IDR", "CHF", "NZD", "BRL"}:
                    parsed_currency = cs.upper()
                else:
                    sorted_syms = sorted(CURRENCY_SYMBOL_MAP.keys(), key=len, reverse=True)
                    for sym in sorted_syms:
                        if cs.startswith(sym):
                            parsed_currency = CURRENCY_SYMBOL_MAP[sym]
                            break

            if not parsed_currency and currency_label:
                cl = currency_label.strip().upper()
                if cl in {"USD", "SGD", "EUR", "GBP", "AUD", "CAD", "JPY", "CNY", "HKD", "MYR", "KRW", "INR", "THB", "PHP", "IDR", "CHF", "NZD", "BRL"}:
                    parsed_currency = cl

            if not parsed_currency:
                p_cur, _ = parse_currency_from_price(price_raw)
                parsed_currency = p_cur

            item["currency"] = parsed_currency or page_currency or "USD"
            item["price"] = price_raw
            item.pop("price_raw", None)
            item.pop("currency_from_price", None)
            item.pop("currency_label", None)
            items.append(item)

        print(f"\n[✓] 共提取 {len(items)} 个产品")

        for i, item in enumerate(items[:5]):
            print(f"\n  [{i+1}] {item.get('product_title', '?')[:40]}")
            print(f"      卖家: {item.get('seller_id', '?')} ({item.get('seller_level', '?')})")
            print(f"      已售: {item.get('sold_count', '?')}  库存: {item.get('stock', '?')}  交货: {item.get('delivery_time', '?')}")
            print(f"      价格: {item.get('price', '?')} {item.get('currency', '')}  最低起订: {item.get('min_order', '?')}")

        if len(items) > 5:
            print(f"\n  ... 还有 {len(items)-5} 条")

        # 保存
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")

        json_path = os.path.join(DOWNLOAD_DIR, f"products_{timestamp}.json")
        with open(json_path, "w", encoding="utf-8") as f:
            json.dump(items, f, indent=2, ensure_ascii=False)
        print(f"\n[✓] JSON: {json_path}")

        await browser.close()
        print("[✓] 完成!")


def main():
    parser = argparse.ArgumentParser(description="爬取 G2G 产品卡片")
    parser.add_argument("url", nargs="?", default=DEFAULT_URL, help="页面 URL")
    parser.add_argument("--scroll", type=int, default=10, help="最大滚动次数 (默认10)")
    args = parser.parse_args()
    asyncio.run(scrape(args.url, args.scroll))


if __name__ == "__main__":
    main()
