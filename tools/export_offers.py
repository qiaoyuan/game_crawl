"""
导出 Offers 工具
用法: python -m tools.export_offers
"""

import asyncio
import json
import os
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

        # 调用 bulk_export API
        print("[*] 调用 bulk_export API...")
        data = api.bulk_export(auth_token)
        print(f"  -> 响应: {json.dumps(data, indent=2, ensure_ascii=False)}")

        if data.get("code") == 2000:
            result_url = data["payload"]["result"]
            api.download_file(result_url)
        else:
            print(f"[!] API 错误: {json.dumps(data, ensure_ascii=False)}")

        # 更新会话
        await br.save_session(context)
        await browser.close()
        print("\n[✓] 完成!")


if __name__ == "__main__":
    asyncio.run(run())
