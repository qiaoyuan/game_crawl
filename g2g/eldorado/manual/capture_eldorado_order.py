"""按订单号单次采集 Eldorado 卖家订单详情或聊天。"""

import argparse
import asyncio
import re
from contextlib import asynccontextmanager
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import urlsplit

from playwright.async_api import Error as PlaywrightError, async_playwright

from g2g.eldorado.shared.chrome import chrome_debug_endpoint
from g2g.eldorado.shared.eldorado_chat_history import read_chat_history
from g2g.eldorado.manual.eldorado_manual_records import find_saved_order, save_chat, save_order
from g2g.eldorado.shared.eldorado_order_details import read_order_fields
from g2g.eldorado.shared.eldorado_sold_list import SOLD_URL, find_sold_orders, is_order_url, is_sold_url


_SITE = "https://www.eldorado.gg"
_UUID = re.compile(r"[a-f0-9]{8}(?:-[a-f0-9]{4}){3}-[a-f0-9]{12}", re.I | re.ASCII)
_CHAT_SELECTORS = SimpleNamespace(messages_frame_selector="iframe[src^='https://app.talkjs.com/']",
                                  messages_selector="#chat-scrollable")
_PORT_FILE = Path.home() / "Library/Application Support/Google/Chrome/DevToolsActivePort"


def parse_order_id(target):
    error = argparse.ArgumentTypeError("请输入有效的订单 UUID 或 Eldorado 订单详情网址")
    target = target.strip()
    try:
        if is_order_url(target):
            target = urlsplit(target).path.rstrip("/").rsplit("/", 1)[-1]
    except ValueError:
        raise error from None
    if not _UUID.fullmatch(target):
        raise error
    return target.lower()


def _matches(url, order_id):
    return is_order_url(url) and urlsplit(url).path.rstrip("/") == "/order/" + order_id


@asynccontextmanager
async def _order_page(browser, order_id):
    contexts = [context for context in browser.contexts
                if any(page.url.startswith(_SITE + "/") for page in context.pages)]
    matches = [[page for page in context.pages if _matches(page.url, order_id)] for context in contexts]
    matched = [pages for pages in matches if pages]
    if len(matched) == 1:
        pages = matched[0]
        page = pages[0]
        for candidate in pages:
            if await candidate.locator("eld-seller-order-details-card .order-details-card:visible").count():
                page = candidate
                break
        yield page
        return
    if len(contexts) != 1:
        raise ValueError("请在目标浏览器会话中打开此订单，确保能唯一确定其所属会话")
    page = await contexts[0].new_page()
    try:
        await page.goto(f"{_SITE}/order/{order_id}", wait_until="domcontentloaded", timeout=15000)
        if not _matches(page.url, order_id):
            raise ValueError("订单详情发生跳转，请检查登录状态及订单号")
        yield page
    finally:
        if not page.is_closed():
            await page.close()


async def _verify_chat_order(page, order_id):
    if not _matches(page.url, order_id):
        raise ValueError("当前页面与指定订单不符")
    handle = await page.wait_for_function(r"""id => {
        const visible = el => el.getClientRects().length && getComputedStyle(el).visibility !== 'hidden';
        const ids = [...document.querySelectorAll('eld-order-header .order-id span')].filter(visible)
            .map(el => el.innerText.trim().match(/^Order ID\s*:\s*(\S+)$/i)).filter(Boolean);
        return ids.length && ids.every(match => match[1] === id) &&
            [...document.querySelectorAll('eld-seller-order-details-card .order-details-card')].some(visible);
    }""", arg=order_id, polling=200, timeout=10000)
    await handle.dispose()
    if not _matches(page.url, order_id):
        raise ValueError("读取期间订单详情发生跳转")


async def capture(browser, stage, order_id):
    """返回 (本地文件, 是否写入, 记录数)。order 和 chat 由不同的显式调用执行。"""
    order_id = parse_order_id(order_id)
    if stage not in {"order", "chat"}:
        raise ValueError("请选择 order 或 chat")
    async with _order_page(browser, order_id) as page:
        if stage == "order":
            fields = await read_order_fields(page, order_id)
            if not _matches(page.url, order_id) or fields["order_id"] != order_id:
                raise ValueError("读取期间订单详情发生跳转")
            path, changed = save_order(fields)
            return path, changed, 1
        await _verify_chat_order(page, order_id)
        messages = await read_chat_history(page, _CHAT_SELECTORS)
        await _verify_chat_order(page, order_id)
        path, changed = save_chat(order_id, messages)
        return path, changed, len(messages)


async def capture_latest(browser):
    contexts = [context for context in browser.contexts
                if any(page.url.startswith(_SITE + "/") for page in context.pages)]
    if len(contexts) != 1:
        raise ValueError("请在唯一的目标 Chrome 会话中打开 Eldorado 售出列表")
    context = contexts[0]
    page = next((page for page in context.pages if page.url == SOLD_URL), None)
    owned = page is None
    if owned:
        page = await context.new_page()
    try:
        if owned:
            await page.goto(SOLD_URL, wait_until="domcontentloaded", timeout=15000)
        else:
            await page.reload(wait_until="domcontentloaded", timeout=15000)
        if not is_sold_url(page.url):
            raise ValueError("售出列表发生跳转，请检查登录状态")
        await page.get_by_text("Recent", exact=True).first.wait_for(state="visible", timeout=5000)
        urls = await find_sold_orders(page)
        if not urls:
            raise ValueError("售出列表当前页没有 Aion 2 订单")
        order_id = parse_order_id(urls[0])
        saved = find_saved_order(order_id)
        if saved is not None:
            return saved, False, 1
        return await capture(browser, "order", order_id)
    finally:
        if owned and not page.is_closed():
            await page.close()


async def run(args):
    async with async_playwright() as playwright:
        try:
            endpoint = chrome_debug_endpoint(_PORT_FILE) if args.chrome_auto_connect else args.cdp_url
            browser = await playwright.chromium.connect_over_cdp(
                endpoint, timeout=60000 if args.chrome_auto_connect else 5000)
        except (ValueError, PlaywrightError):
            raise ValueError("无法连接 Chrome，请确认远程调试已开启并允许本次连接") from None
        # Closing Playwright disconnects this client; the browser and existing tabs remain open.
        if args.stage == "latest":
            return await capture_latest(browser)
        return await capture(browser, args.stage, args.order_id)


def main():
    parser = argparse.ArgumentParser(description="按指定订单号单次读取卖家订单详情或聊天")
    parser.add_argument("stage", choices=["latest", "order", "chat"])
    parser.add_argument("order_id", nargs="?", type=parse_order_id, metavar="ORDER_ID_OR_URL")
    connection = parser.add_mutually_exclusive_group()
    connection.add_argument("--chrome-auto-connect", action="store_true", help="连接本机已开启远程调试的 Chrome")
    connection.add_argument("--cdp-url", default="http://127.0.0.1:9222", help="指定已登录浏览器的调试地址")
    args = parser.parse_args()
    if (args.stage == "latest") != (args.order_id is None):
        parser.error("latest 无需订单号，order 和 chat 必须指定订单号")
    try:
        path, changed, count = asyncio.run(run(args))
    except PlaywrightError:
        parser.exit(1, "读取失败，请确认已登录卖家账号且指定订单页面已加载。\n")
    except ValueError as exc:
        parser.exit(1, f"{exc}\n")
    except OSError:
        parser.exit(1, "无法读写本地记录，请检查保存目录。\n")
    if args.stage != "chat":
        status = "已新建" if changed else "已存在"
        print(f"订单{status}（{count} 笔） {path}")
    else:
        status = "已保存" if changed else "无变化"
        print(f"聊天{status}（{count} 条） {path}")


if __name__ == "__main__":
    main()
