"""
爬取 G2G 分类页商户数据（无需登录）
用法: python -m tools.scrape_category [URL]
"""

import asyncio
import json
import os
import re
import csv
import argparse
from datetime import datetime
from playwright.async_api import async_playwright
from g2g import config

DEFAULT_URL = (
    "https://www.g2g.com/cn/categories/wow-gold/offer/group"
    "?fa=lgc_2299_platform%3Algc_2299_platform_39979"
    "&region_id=dfced32f-2f0a-4df5-a218-1e068cfadffa"
)

ROOT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DOWNLOAD_DIR = os.path.join(ROOT_DIR, "downloads")

# 货币符号 → 货币代码映射
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


async def scrape(url: str = DEFAULT_URL):
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

        # 等待商户列表加载
        print("[*] 等待商户列表加载...")
        for i in range(60):
            el = await page.query_selector("#pcOtherOffer")
            if el:
                cards = await el.query_selector_all(".other-seller--gradient")
                if len(cards) > 0:
                    print(f"  -> 检测到 {len(cards)} 个商户 ({i+1}s)")
                    break
            await asyncio.sleep(1)
        await asyncio.sleep(2)

        # 提取商户数据
        items = await page.evaluate("""
            (() => {
                const container = document.querySelector('#pcOtherOffer');
                if (!container) return [];

                const cards = container.querySelectorAll('.other-seller--gradient');
                const results = [];

                cards.forEach(card => {
                    const item = {};

                    // 店铺名称
                    const nameEl = card.querySelector('.text-body2.ellipsis.text-weight-medium');
                    if (nameEl) item.seller_name = nameEl.innerText.trim();

                    // 店铺链接
                    const linkEl = card.querySelector('a[href]');
                    if (linkEl) item.seller_url = linkEl.href;

                    // 卖家等级
                    const levelEl = card.querySelector('.text-caption.text-secondary.text-weight-medium');
                    if (levelEl) item.seller_level = levelEl.innerText.trim();

                    // 头像
                    const avatarEl = card.querySelector('img.user-avatar');
                    if (avatarEl) item.avatar = avatarEl.src;

                    // 在线状态（绿点）
                    const onlineEl = card.querySelector('.g-round-indicator.bg-positive');
                    item.is_online = !!onlineEl;

                    // 好评率
                    const ratingEl = card.querySelector('.text-positive.text-weight-medium.q-ml-xs');
                    if (ratingEl) item.rating = ratingEl.innerText.trim();

                    // 已售出（中英文都匹配）
                    const soldEls = card.querySelectorAll('.bg-neutral-100-light.text-secondary');
                    soldEls.forEach(el => {
                        const t = el.innerText.trim();
                        if (t.includes('已售出') || t.toLowerCase().includes('sold')) item.sold_count = t;
                    });

                    // 所有 badge（最低起订/库存/交货时间）
                    const badges = card.querySelectorAll('.q-badge__delivery');
                    badges.forEach(badge => {
                        const t = badge.innerText.trim();
                        if (!t) return;
                        // "Min. 3k" / "最低 3k" → 最低起订
                        if (t.startsWith('最低') || t.match(/^Min\\.?\\s/i)) {
                            item.min_order = t;
                        }
                        // "10 Mins" / "30 Mins" / "10分钟 - 50分钟" → 交货时间
                        else if (t.match(/\\d+\\s*(Mins?|分钟|小时|Hours?|Hr)/i)) {
                            item.delivery_time = t;
                        }
                        // 纯数字+k/M → 库存
                        else if (t.match(/^[\\d.,]+[kKmM]?$/)) {
                            item.stock = t;
                        }
                    });

                    // 如果 sold_count 没找到，从所有 badge 文本中找
                    if (!item.sold_count) {
                        const allBadges = card.querySelectorAll('[class*="badge"]');
                        allBadges.forEach(b => {
                            const t = b.innerText.trim();
                            if (t.includes('已售出') || t.toLowerCase().includes('sold')) item.sold_count = t;
                        });
                    }

                    // 阶梯折扣
                    const chipEl = card.querySelector('.q-chip__content');
                    if (chipEl) item.has_volume_discount = chipEl.innerText.trim().includes('折扣');

                    // 单价（含货币符号，如 "S$ 0.87"）
                    const priceEl = card.querySelector('.text-primary.text-body.text-weight-bold');
                    if (priceEl) item.unit_price_raw = priceEl.innerText.trim();

                    // 货币标签
                    const currencyEl = card.querySelector('.text-secondary.text-body2.text-weight-medium');
                    if (currencyEl) item.currency_label = currencyEl.innerText.trim();

                    // 最低价标签
                    const lowestEl = card.querySelector('.text-secondary.text-caption-1');
                    if (lowestEl) item.price_label = lowestEl.innerText.trim();

                    results.push(item);
                });

                return results;
            })()
        """)

        # ========== Python 侧货币解析：优先从价格文本提取 ==========
        for item in items:
            price_raw = item.get("unit_price_raw", "")
            currency_label = item.get("currency_label", "")
            parsed_currency, parsed_price = parse_currency_from_price(price_raw)
            card_currency = None
            if currency_label and currency_label.strip().upper() in {"USD", "SGD", "EUR", "GBP", "AUD", "CAD", "JPY", "CNY", "HKD", "MYR", "KRW", "INR", "THB", "PHP", "IDR"}:
                card_currency = currency_label.strip().upper()
            item["unit_price"] = parsed_price or price_raw
            item["currency"] = parsed_currency or card_currency or "USD"
            item.pop("unit_price_raw", None)
            item.pop("currency_label", None)

        print(f"\n[✓] 共提取 {len(items)} 个商户")

        # 打印前5条预览
        for i, item in enumerate(items[:5]):
            print(f"\n  [{i+1}] {item.get('seller_name', '?')}")
            print(f"      等级: {item.get('seller_level', '?')}  好评率: {item.get('rating', '?')}  已售: {item.get('sold_count', '?')}")
            print(f"      库存: {item.get('stock', '?')}  单价: {item.get('unit_price', '?')} {item.get('currency', '')}  交货: {item.get('delivery_time', '?')}")
            print(f"      最低起订: {item.get('min_order', '?')}  在线: {'是' if item.get('is_online') else '否'}")

        if len(items) > 5:
            print(f"\n  ... 还有 {len(items)-5} 条")

        # 保存
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")

        json_path = os.path.join(DOWNLOAD_DIR, f"sellers_{timestamp}.json")
        with open(json_path, "w", encoding="utf-8") as f:
            json.dump(items, f, indent=2, ensure_ascii=False)
        print(f"\n[✓] JSON: {json_path}")

        if items:
            csv_path = os.path.join(DOWNLOAD_DIR, f"sellers_{timestamp}.csv")
            all_keys = []
            for item in items:
                for k in item.keys():
                    if k not in all_keys:
                        all_keys.append(k)
            with open(csv_path, "w", newline="", encoding="utf-8-sig") as f:
                writer = csv.DictWriter(f, fieldnames=all_keys, extrasaction="ignore")
                writer.writeheader()
                writer.writerows(items)
            print(f"[✓] CSV: {csv_path}")

        await page.screenshot(path=os.path.join(DOWNLOAD_DIR, "page_screenshot.png"))
        await browser.close()
        print("[✓] 完成!")


def main():
    parser = argparse.ArgumentParser(description="爬取 G2G 分类页商户数据")
    parser.add_argument("url", nargs="?", default=DEFAULT_URL, help="分类页 URL")
    args = parser.parse_args()
    asyncio.run(scrape(args.url))


if __name__ == "__main__":
    main()
