"""
爬取 Eldorado.gg 商户数据（无需登录）
用法: python -m tools.scrape_eldorado [URL]
"""

import asyncio
import json
import os
import argparse
from datetime import datetime
from playwright.async_api import async_playwright
from g2g import config

DEFAULT_URL = (
    "https://www.eldorado.gg/wow-classic-gold/g/92-0-0"
    "?te_v0=NA%20%26%20OC%20Anniversary"
    "&te_v1=Dreamscythe"
    "&te_v2=Alliance"
    "&offerSortingCriterion=Cheapest"
)

ROOT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DOWNLOAD_DIR = os.path.join(ROOT_DIR, "downloads")


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

        # 等待页面渲染
        print("[*] 等待列表加载...")
        await asyncio.sleep(5)

        # 截图看页面结构
        try:
            await page.screenshot(path=os.path.join(DOWNLOAD_DIR, "eldorado_debug.png"))
        except Exception:
            pass

        # 尝试找到 Other sellers 区域
        # 等待包含 seller/offer 的元素出现
        for i in range(30):
            found = await page.evaluate("""
                (() => {
                    const text = document.body.innerText;
                    return text.includes('Other sellers') || text.includes('other sellers')
                        || text.includes('Sellers') || text.includes('Offers');
                })()
            """)
            if found:
                print(f"  -> 检测到 sellers 区域 ({i+1}s)")
                break
            await asyncio.sleep(1)

        await asyncio.sleep(3)

        # 尝试找到并点击 "Show more" 按钮加载更多商户
        print("[*] 尝试加载更多商户...")
        for attempt in range(10):
            clicked = await page.evaluate("""
                (() => {
                    const btns = document.querySelectorAll('button, a, [role="button"]');
                    for (const btn of btns) {
                        const t = btn.innerText.trim().toLowerCase();
                        if (t === 'show more' || t === 'load more' || t === 'view all'
                            || t === 'see more' || t === 'show all'
                            || t.includes('show more') || t.includes('view all')) {
                            btn.click();
                            return t;
                        }
                    }
                    return null;
                })()
            """)
            if clicked:
                print(f"  -> 点击了 '{clicked}' ({attempt+1})")
                await asyncio.sleep(3)
            else:
                if attempt == 0:
                    print("  -> 未找到 Show more 按钮")
                break

        # 滚动到底部确保全部加载
        await page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
        await asyncio.sleep(2)

        await page.screenshot(path=os.path.join(DOWNLOAD_DIR, "eldorado_sellers.png"))

        # 提取商户数据 - 基于文本模式匹配
        items = await page.evaluate("""
            (() => {
                const results = [];
                const bodyText = document.body.innerText;

                // 找到 "Other sellers" 区域
                const otherSellersIdx = bodyText.indexOf('Other sellers');
                if (otherSellersIdx === -1) return [];

                const sellersText = bodyText.substring(otherSellersIdx);
                const lines = sellersText.split('\\n').map(l => l.trim()).filter(l => l);

                // 跳过标题行和排序选项
                let i = 0;
                while (i < lines.length) {
                    const line = lines[i];
                    // 跳过标题和排序标签
                    if (line.includes('Other sellers') || line === 'Recommended'
                        || line === 'Cheapest first' || line === 'Lowest min. quantity') {
                        i++;
                        continue;
                    }
                    break;
                }

                // 解析每个 seller 块
                // 每个 seller 的格式:
                //   Name
                //   XX.X%
                //   N,NNN reviews
                //   In stock
                //   NNN,NNN
                //   Min. qty.
                //   NNN
                //   Delivery time
                //   XX min - X h
                //   $X.XXXX / unit   (或 Current offer 后面跟价格)

                while (i < lines.length) {
                    const item = {};

                    // 名称（当前行）
                    if (lines[i] && !lines[i].match(/^[\\d.]+$/) && !lines[i].includes('%')
                        && !lines[i].includes('reviews') && !lines[i].includes('stock')
                        && !lines[i].includes('qty') && !lines[i].includes('Delivery')
                        && !lines[i].includes('$') && !lines[i].includes('offer')
                        && lines[i].length > 1 && lines[i].length < 50) {
                        item.name = lines[i];
                        i++;
                    } else {
                        i++;
                        continue;
                    }

                    // 好评率 (XX.X%)
                    if (i < lines.length && lines[i].match(/^[\\d.]+%$/)) {
                        item.rating = lines[i];
                        i++;
                    }

                    // 评论数 (N,NNN reviews)
                    if (i < lines.length && lines[i].match(/^[\\d,]+\\s*reviews?$/)) {
                        item.reviews = lines[i];
                        i++;
                    }

                    // In stock
                    if (i < lines.length && lines[i].match(/^In stock$/i)) {
                        i++;
                        // 库存数量
                        if (i < lines.length && lines[i].match(/^[\\d,]+$/)) {
                            item.stock = lines[i];
                            i++;
                        }
                    }

                    // Min. qty.
                    if (i < lines.length && lines[i].match(/^Min\\.?\\s*qty/i)) {
                        i++;
                        if (i < lines.length && lines[i].match(/^[\\d,]+$/)) {
                            item.min_qty = lines[i];
                            i++;
                        }
                    }

                    // Delivery time
                    if (i < lines.length && lines[i].match(/^Delivery time$/i)) {
                        i++;
                        if (i < lines.length) {
                            item.delivery_time = lines[i];
                            i++;
                        }
                    }

                    // 价格 ($X.XXXX / unit)
                    if (i < lines.length && lines[i].match(/^Current offer$/i)) {
                        i++;
                    }
                    if (i < lines.length && lines[i].match(/\\$[\\d.]+/)) {
                        item.price = lines[i];
                        i++;
                    }

                    if (item.name && item.price) {
                        results.push(item);
                    }
                }

                return results;
            })()
        """)

        print(f"\n[✓] 共提取 {len(items)} 个商户")

        for i, item in enumerate(items[:10]):
            print(f"  [{i+1}] {item.get('name', '?')}  |  库存: {item.get('stock', '?')}  |  价格: {item.get('price', '?')}")

        if len(items) > 10:
            print(f"  ... 还有 {len(items)-10} 条")

        # 保存 JSON
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        json_path = os.path.join(DOWNLOAD_DIR, f"eldorado_sellers_{timestamp}.json")
        with open(json_path, "w", encoding="utf-8") as f:
            json.dump(items, f, indent=2, ensure_ascii=False)
        print(f"\n[✓] JSON: {json_path}")

        await browser.close()
        print("[✓] 完成!")


def main():
    parser = argparse.ArgumentParser(description="爬取 Eldorado.gg 商户数据")
    parser.add_argument("url", nargs="?", default=DEFAULT_URL, help="页面 URL")
    args = parser.parse_args()
    asyncio.run(scrape(args.url))


if __name__ == "__main__":
    main()
