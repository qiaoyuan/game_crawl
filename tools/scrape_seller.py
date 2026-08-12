"""
爬取商户数据工具
用法: python -m tools.scrape_seller
"""

import asyncio
import json
import os
import csv
from datetime import datetime
from playwright.async_api import async_playwright
from g2g import config, browser as br, auth, api


async def run():
    os.makedirs(config.DOWNLOAD_DIR, exist_ok=True)
    auth_token = None

    async with async_playwright() as p:
        browser = await br.create_browser(p)
        context = await br.create_context(browser, use_session=True)
        page = await context.new_page()

        # 监听请求捕获 token
        def on_request(request):
            nonlocal auth_token
            auth_header = request.headers.get("authorization", "")
            if auth_header.startswith("eyJ") and not auth_token:
                auth_token = auth_header
                print(f"[✓] 捕获到 Token: {auth_header[:60]}...")

        page.on("request", on_request)

        # 确保已登录
        logged_in = await auth.ensure_logged_in(page, context)
        if not logged_in:
            print("[!] 登录失败")
            await browser.close()
            return

        # 保存会话
        await br.save_session(context)

        # 导航到 offers/list 触发 token
        if not auth_token:
            await page.goto(config.OFFERS_LIST_URL, wait_until="domcontentloaded", timeout=60000)
            await asyncio.sleep(5)

        # 从存储提取 token
        if not auth_token:
            auth_token = await auth.get_token(page)

        if not auth_token:
            print("[!] 未能获取 Token")
            br.clear_session()
            await browser.close()
            return

        # ========== 爬取商户数据 ==========
        print("[*] 开始爬取商户数据...")

        # 1. 获取卖家信息
        print("\n[1] 获取卖家信息...")
        seller_info = api.get_seller_info(auth_token)
        print(f"  -> {json.dumps(seller_info, indent=2, ensure_ascii=False)}")

        # 2. 分页获取所有 offers
        print("\n[2] 获取 Offers 列表...")
        all_offers = []
        page_num = 1
        while True:
            print(f"  -> 正在获取第 {page_num} 页...")
            result = api.get_seller_offers(auth_token, page=page_num, page_size=50)

            if result.get("code") != 2000:
                print(f"  -> API 错误: {result.get('messages')}")
                break

            payload = result.get("payload", {})
            offers = payload.get("offers", payload.get("data", []))

            if not offers:
                break

            all_offers.extend(offers)
            print(f"     本页 {len(offers)} 条，累计 {len(all_offers)} 条")

            # 检查是否还有更多
            total = payload.get("total", 0)
            if len(all_offers) >= total:
                break

            page_num += 1
            await asyncio.sleep(0.5)  # 避免请求过快

        print(f"\n[✓] 共获取 {len(all_offers)} 条 Offers")

        # 3. 保存为 JSON
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        json_path = os.path.join(config.DOWNLOAD_DIR, f"seller_offers_{timestamp}.json")
        with open(json_path, "w", encoding="utf-8") as f:
            json.dump({
                "seller_info": seller_info.get("payload", {}),
                "offers": all_offers,
                "total": len(all_offers),
                "exported_at": timestamp,
            }, f, indent=2, ensure_ascii=False)
        print(f"[✓] JSON 已保存: {json_path}")

        # 4. 保存为 CSV
        if all_offers:
            csv_path = os.path.join(config.DOWNLOAD_DIR, f"seller_offers_{timestamp}.csv")
            # 取所有 key 的并集作为表头
            all_keys = []
            for offer in all_offers:
                for k in offer.keys():
                    if k not in all_keys:
                        all_keys.append(k)

            with open(csv_path, "w", newline="", encoding="utf-8-sig") as f:
                writer = csv.DictWriter(f, fieldnames=all_keys, extrasaction="ignore")
                writer.writeheader()
                writer.writerows(all_offers)
            print(f"[✓] CSV 已保存: {csv_path}")

        # 更新会话
        await br.save_session(context)
        await browser.close()
        print("\n[✓] 完成!")


if __name__ == "__main__":
    asyncio.run(run())
