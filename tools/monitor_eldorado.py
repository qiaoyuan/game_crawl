"""从已登录的 Eldorado 浏览器读取订单和聊天变化，输出 JSON Lines。"""

import argparse
import asyncio
import hashlib
import json
import math
import os
import signal
import sys
from contextlib import ExitStack
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

from playwright.async_api import Error as PlaywrightError, async_playwright

from tools.eldorado_chat_history import extract_messages, read_chat_history
from tools.eldorado_order_details import read_order_fields
from tools.eldorado_sold_list import SOLD_URL, find_recent_orders, find_sold_orders, is_order_url, is_sold_url, open_sold_order


def chrome_debug_endpoint(active_port_file):
    """从 Chrome 本机调试信息读取地址；内容具有访问权限，不得记录。"""
    try:
        lines = Path(active_port_file).read_text(encoding="utf-8").splitlines()
        port = int(lines[0])
        path = lines[1]
        if not 1 <= port <= 65535 or not path.startswith("/devtools/browser/"):
            raise ValueError
        if any(c in path for c in "?# \t\r\n"):
            raise ValueError
    except (OSError, UnicodeError, IndexError, ValueError):
        raise ValueError("无法读取 Chrome 调试连接信息；请确认已打开远程调试。") from None
    return f"ws://127.0.0.1:{port}{path}"


class EventHub:
    def __init__(self, output=None):
        self.current = None
        self.output = output
        self.chat_digests = {}

    def publish(self, snapshot):
        if snapshot == self.current:
            return
        self.current = snapshot
        self.write(snapshot)

    def publish_order(self, snapshot):
        url = snapshot["url"]
        # Order panels can contain a countdown. Only changes in the conversation
        # warrant another copy, and a long-running monitor need not retain chat text.
        serialized = json.dumps(snapshot["messages"], ensure_ascii=False, sort_keys=True)
        digest = hashlib.sha256(serialized.encode("utf-8")).digest()
        if self.chat_digests.get(url) != digest:
            self.write({**snapshot, "captured_at": datetime.now(timezone.utc).isoformat()})
            self.chat_digests[url] = digest

    def write(self, snapshot):
        line = json.dumps(snapshot, ensure_ascii=False, separators=(",", ":"))
        print(line, flush=True)
        if self.output is not None:
            self.output.write(line + "\n")
            self.output.flush()


async def visible_text(page, selector):
    """返回可见匹配元素的文本；忽略空白差异，保留元素顺序。"""
    return await page.locator(selector).evaluate_all("""nodes => nodes
        .filter(node => {
            const style = getComputedStyle(node);
            return node.getClientRects().length && style.visibility !== 'hidden';
        })
        .map(node => (node.innerText || '').replace(/\\s+/g, ' ').trim())
        .filter(Boolean)
    """)


def allowed_url(url, url_contains):
    parsed = urlsplit(url)
    return (parsed.scheme == "https" and parsed.hostname == "www.eldorado.gg"
            and "/order/" in parsed.path and url_contains in url)


async def read_messages(page, frame_selector, messages_selector):
    """读取可见 iframe，不返回 iframe 地址（其中可能含认证信息）。"""
    messages = []
    available = False
    matched = False
    frames = page.locator(frame_selector)
    for index in range(await frames.count()):
        iframe = frames.nth(index)
        if not await iframe.is_visible():
            continue
        handle = await iframe.element_handle()
        if handle is None:
            continue
        frame = await handle.content_frame()
        if frame is None or not frame.url or frame.url == "about:blank":
            continue
        frame_url = frame.url
        count = await frame.locator(messages_selector).count()
        texts = await extract_messages(frame, messages_selector)
        if frame.is_detached() or frame.url != frame_url:
            # 丢弃导航期间的不一致快照，下一轮重新读取。
            raise PlaywrightError("frame changed during read")
        available = True
        matched = matched or count > 0
        messages.extend(texts)
    return available, matched, messages


def safe_page_url(url):
    """保留订单路径，丢弃可能含敏感信息的查询字符串及 fragment。"""
    parts = urlsplit(url)
    return urlunsplit((parts.scheme, parts.netloc, parts.path, "", ""))


async def monitor(browser, hub, args):
    while True:
        snapshot = {"status": "waiting"}
        for page in (page for context in browser.contexts for page in context.pages):
            if not allowed_url(page.url, args.url_contains):
                continue
            try:
                url = page.url
                order = await visible_text(page, args.order_selector)
                available, matched, messages = await read_messages(
                    page, args.messages_frame_selector, args.messages_selector)
                # Navigation can happen during the two DOM reads.
                if page.url != url or not allowed_url(page.url, args.url_contains):
                    continue
                if order or available:
                    snapshot = {"status": "ready", "url": safe_page_url(url),
                                "order": order, "messages_frame_available": available,
                                "messages_selector_found": matched, "messages": messages}
                    break
            except PlaywrightError:
                continue
        hub.publish(snapshot)
        await asyncio.sleep(args.interval)


async def monitor_sold(browser, hub, args, *, journal=None):
    # Keep a reference to the chosen context. A redirect to login or a user
    # switching tabs must never cause queued orders to open in another account.
    pages = [page for context in browser.contexts for page in context.pages]
    page = next((item for item in pages if is_sold_url(item.url)), None)
    owns_page = page is None
    if owns_page:
        context = next((item.context for item in pages
                        if urlsplit(item.url).hostname == "www.eldorado.gg"), None)
        if context is None and len(browser.contexts) != 1:
            raise SystemExit("请在目标浏览器会话中打开售出订单列表。")
        page = await (context or browser.contexts[0]).new_page()

    queue = asyncio.Queue()
    pending = set()

    async def read_details():
        while True:
            url, candidate = await queue.get()
            try:
                async with open_sold_order(page, url) as detail:
                    if journal is not None:
                        fields = await read_order_fields(detail, candidate["order_id"])
                        if journal.record(candidate, fields):
                            print("已写入一笔新订单。", file=sys.stderr, flush=True)
                        continue
                    messages = await read_chat_history(detail, args)
                    order = await visible_text(detail, args.order_selector)
                    if safe_page_url(detail.url).rstrip("/") != url:
                        raise PlaywrightError("订单在读取期间跳转")
                    hub.publish_order({"status": "ready", "url": url, "game": "Aion 2",
                                       "order": order, "messages": messages})
            except PlaywrightError:
                print("一笔订单详情暂未就绪，将在后续刷新时重试。", file=sys.stderr, flush=True)
            finally:
                pending.discard(url)

    async def refresh_list():
        nonlocal page, owns_page
        loop = asyncio.get_running_loop()
        while not page.is_closed():
            started = loop.time()
            try:
                if journal is not None and is_order_url(page.url):
                    page = await page.context.new_page()
                    owns_page = True
                if is_sold_url(page.url):
                    await page.reload(wait_until="domcontentloaded", timeout=15000)
                elif page.url == "chrome-error://chromewebdata/" or (owns_page and page.url == "about:blank"):
                    await page.goto(SOLD_URL, wait_until="domcontentloaded", timeout=15000)
                if is_sold_url(page.url):
                    if journal is not None:
                        rows = (await find_recent_orders(page))[:1]
                        baseline, new = journal.observe(rows)
                        candidates = journal.pending()
                        items = [(order["url"], order) for order in candidates]
                        result = "已建立起点" if baseline else f"发现 {new} 笔新单"
                        status = f"已检查列表前 {len(rows)} 笔，{result}，待记录 {len(items)} 笔。"
                    else:
                        urls = await find_sold_orders(page)
                        items = [(url, None) for url in urls]
                        status = f"售出列表刷新完成，当前页有 {len(urls)} 笔 Aion 2 订单。"
                    for url, candidate in items:
                        if url not in pending:
                            pending.add(url)
                            queue.put_nowait((url, candidate))
                    print(status, file=sys.stderr, flush=True)
                else:
                    print("正在等待登录并返回售出订单列表。", file=sys.stderr, flush=True)
            except PlaywrightError:
                print("售出列表暂未就绪，下轮刷新重试。", file=sys.stderr, flush=True)
            await asyncio.sleep(max(0, args.interval - (loop.time() - started)))

    poller = asyncio.create_task(refresh_list())
    reader = asyncio.create_task(read_details())
    try:
        done, _ = await asyncio.wait((poller, reader), return_when=asyncio.FIRST_COMPLETED)
        for task in done:
            task.result()
    finally:
        for task in (poller, reader):
            task.cancel()
        await asyncio.gather(poller, reader, return_exceptions=True)
        if owns_page and not page.is_closed():
            await page.close()


async def run(args):
    with ExitStack() as stack:
        journal = output = None
        if args.sold_orders:
            from tools.eldorado_order_journal import OrderJournal
            journal = stack.enter_context(OrderJournal(args.output))
        elif args.output:
            fd = os.open(args.output, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
            output = stack.enter_context(os.fdopen(fd, "a", encoding="utf-8"))
        await run_connected(args, EventHub(output), journal)


async def run_connected(args, hub, journal):
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, stop.set)
        except (NotImplementedError, RuntimeError):
            pass
    try:
        async with async_playwright() as playwright:
            try:
                endpoint = chrome_debug_endpoint(args.chrome_active_port) if args.chrome_auto_connect else args.cdp_url
                if args.chrome_auto_connect:
                    print("正在连接 Chrome；如浏览器弹出连接请求，请允许。", file=sys.stderr, flush=True)
                browser = await playwright.chromium.connect_over_cdp(endpoint, timeout=60000 if args.chrome_auto_connect else 5000)
            except (PlaywrightError, ValueError):
                print("无法连接浏览器。请确认已启用远程调试、允许 Chrome 的连接请求，并检查连接参数。", file=sys.stderr)
                raise SystemExit(1) from None
            browser.on("disconnected", lambda _: stop.set())
            try:
                if args.sold_orders or args.sold_chats:
                    print(f"已连接浏览器，每 {args.interval:g} 秒刷新售出列表。", file=sys.stderr, flush=True)
                    task = asyncio.create_task(monitor_sold(browser, hub, args, journal=journal))
                else:
                    print("已连接浏览器，请手动登录并打开订单；变化将以 JSON Lines 输出。", file=sys.stderr, flush=True)
                    task = asyncio.create_task(monitor(browser, hub, args))
                stop_task = asyncio.create_task(stop.wait())
                done, pending = await asyncio.wait((task, stop_task), return_when=asyncio.FIRST_COMPLETED)
                for pending_task in pending:
                    pending_task.cancel()
                await asyncio.gather(*pending, return_exceptions=True)
                for done_task in done:
                    done_task.result()
            finally:
                await browser.close()
    finally:
        for sig in (signal.SIGINT, signal.SIGTERM):
            try:
                loop.remove_signal_handler(sig)
            except (NotImplementedError, RuntimeError):
                pass


def main():
    parser = argparse.ArgumentParser(
        description="连接已登录的 Chromium 浏览器，读取 Eldorado 订单及聊天，按变化输出 JSON Lines。")
    modes = parser.add_mutually_exclusive_group()
    modes.add_argument("--sold-orders", action="store_true", help="检查售出列表第一笔，按订单号记录 Aion 2 新单")
    modes.add_argument("--sold-chats", action="store_true", help="按聊天变化记录售出列表当前页 Aion 2 订单")
    parser.add_argument("--url-contains", default="/order/",
                        help="订单详情 URL 必须包含的区分大小写的字符串（默认 /order/）")
    parser.add_argument("--order-selector", default=".order-container", help="订单基本信息容器的 CSS 选择器（默认 .order-container）")
    parser.add_argument("--messages-selector", default="#chat-scrollable", help="聊天 iframe 内的消息 CSS 选择器（默认 #chat-scrollable）")
    parser.add_argument("--messages-frame-selector", default="iframe[src^='https://app.talkjs.com/']", help="主页面中聊天 iframe 的 CSS 选择器（默认匹配 app.talkjs.com）")
    parser.add_argument("--cdp-url", default="http://127.0.0.1:9222", help="已开启远程调试的 Chromium 浏览器 CDP 地址（默认 http://127.0.0.1:9222）")
    parser.add_argument("--chrome-auto-connect", action="store_true", help="通过 Chrome 的 DevToolsActivePort 自动连接已运行的 Chrome（macOS 默认路径）")
    parser.add_argument("--chrome-active-port", default=str(Path.home() / "Library/Application Support/Google/Chrome/DevToolsActivePort"), help="Chrome 的 DevToolsActivePort 文件路径；与 --chrome-auto-connect 配合使用")
    parser.add_argument("--interval", type=float, help="间隔秒数，售出列表默认 30，手动详情默认 3")
    parser.add_argument("--output", help="可选 JSONL 文件，追加写入；包含订单与聊天敏感信息")
    args = parser.parse_args()
    if args.interval is None:
        args.interval = 30 if args.sold_orders or args.sold_chats else 3
    if args.sold_orders and not args.output:
        args.output = "downloads/eldorado_new_orders.jsonl"
    if not math.isfinite(args.interval) or args.interval < 1:
        parser.error("--interval 不能小于 1 秒")
    if not args.url_contains.strip():
        parser.error("--url-contains 不能为空")
    try:
        asyncio.run(run(args))
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
