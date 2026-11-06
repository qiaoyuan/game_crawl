"""手动登录后监控已打开的 Eldorado 订单页面，以 JSON Lines 输出文本变化。"""

import argparse
import asyncio
import json
import os
import signal
import sys
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

from playwright.async_api import Error as PlaywrightError, async_playwright


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

    def publish(self, snapshot):
        if snapshot == self.current:
            return
        self.current = snapshot
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


async def extract_messages(frame, selector):
    """优先提取 TalkJS 消息字段，不采集附件地址或相对时间。"""
    return await frame.locator(selector).evaluate_all(r"""containers => {
        const clean = value => (value || '').replace(/\s+/g, ' ').trim();
        const visible = node => node.getClientRects().length &&
            getComputedStyle(node).visibility !== 'hidden';
        const nodes = [];
        for (const container of containers.filter(visible)) {
            if (container.matches('.MessageDiv')) nodes.push(container);
            else nodes.push(...container.querySelectorAll('.MessageDiv'));
        }
        const unique = [...new Set(nodes)].filter(visible);
        if (unique.length) return unique.map(node => {
            const body = node.querySelector('.MessageBody__message-text');
            const time = node.querySelector('[role="time"]');
            const links = [...node.querySelectorAll('a')]
                .filter(link => !link.closest('.MessageBody__message-text'))
                .map(link => clean(link.getAttribute('download') || link.innerText))
                .filter(name => !/^https?:\/\//i.test(name));
            const images = [...node.querySelectorAll('a img[alt]')]
                .map(img => clean(img.alt)).filter(name => !/^https?:\/\//i.test(name));
            return {
                text: clean(body ? body.innerText : ''),
                time: time ? (time.getAttribute('title') || '') : '',
                direction: (node.matches('.UserMessage__by-me') ||
                    node.querySelector('.UserMessage__by-me') ||
                    node.closest('.UserMessage__by-me')) ? 'outgoing' : 'unknown',
                attachments: [...new Set([...links, ...images].filter(Boolean))]
            };
        });
        // 容器结构改变时保留可见文本，供用户识别；可能包含相对时间。
        return containers.filter(visible).map(node => clean(node.innerText))
            .filter(Boolean).map(text => ({text, source: 'fallback'}));
    }""")


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


async def run(args):
    output = None
    if args.output:
        fd = os.open(args.output, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
        output = os.fdopen(fd, "a", encoding="utf-8")
    hub = EventHub(output)
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
        if output is not None:
            output.close()


def main():
    parser = argparse.ArgumentParser(
        description="连接已开启远程调试的 Chromium 浏览器，手动登录 Eldorado 并打开订单后监控指定元素的可见文本变化，以 JSON Lines 输出快照。不会自动刷新或点击订单。")
    parser.add_argument("--url-contains", default="/order/",
                        help="订单详情 URL 必须包含的区分大小写的字符串（默认 /order/）")
    parser.add_argument("--order-selector", default=".order-container", help="订单基本信息容器的 CSS 选择器（默认 .order-container）")
    parser.add_argument("--messages-selector", default="#chat-scrollable", help="聊天 iframe 内的消息 CSS 选择器（默认 #chat-scrollable）")
    parser.add_argument("--messages-frame-selector", default="iframe[src^='https://app.talkjs.com/']", help="主页面中聊天 iframe 的 CSS 选择器（默认匹配 app.talkjs.com）")
    parser.add_argument("--cdp-url", default="http://127.0.0.1:9222", help="已开启远程调试的 Chromium 浏览器 CDP 地址（默认 http://127.0.0.1:9222）")
    parser.add_argument("--chrome-auto-connect", action="store_true", help="通过 Chrome 的 DevToolsActivePort 自动连接已运行的 Chrome（macOS 默认路径）")
    parser.add_argument("--chrome-active-port", default=str(Path.home() / "Library/Application Support/Google/Chrome/DevToolsActivePort"), help="Chrome 的 DevToolsActivePort 文件路径；与 --chrome-auto-connect 配合使用")
    parser.add_argument("--interval", type=float, default=3, help="检查间隔，秒（默认 3）")
    parser.add_argument("--output", help="可选 JSONL 文件，追加写入；包含订单与聊天敏感信息")
    args = parser.parse_args()
    if args.interval < 1:
        parser.error("--interval 不能小于 1 秒")
    if not args.url_contains.strip():
        parser.error("--url-contains 不能为空")
    try:
        asyncio.run(run(args))
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
