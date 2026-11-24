"""以拦截的浏览器页面验证售出列表轮询和按订单输出，不连接真实账号。"""

import asyncio
import contextlib
import io
import json
import unittest
from collections import Counter
from types import SimpleNamespace
from urllib.parse import urlsplit
from unittest.mock import patch

from playwright.async_api import async_playwright

from g2g.eldorado.shared import eldorado_chat_history as chat
from g2g.eldorado.shared.eldorado_sold_list import SOLD_URL
from g2g.eldorado.monitor.monitor_eldorado import EventHub, monitor_sold


class SoldMonitorTests(unittest.IsolatedAsyncioTestCase):
    async def test_list_refreshes_during_slow_read_and_only_changed_orders_are_emitted(self):
        # The first chat stays blocked until several list refreshes have occurred.
        # No timing or coordination with an external website is required.
        slow_chat = asyncio.Event()
        release_chat = asyncio.Event()
        refreshed = asyncio.Event()
        list_reads_while_blocked = 0
        detail_requests = Counter()
        reads_before_third = {}
        sold_html = """
            <a class="grid-row" href="/order/first"><span class="game-name">Aion 2</span></a>
            <a class="grid-row" href="/order/second"><span class="game-name">AION2</span></a>
            <a class="grid-row" href="/order/other"><span class="game-name">Aion 20</span></a>
        """

        async def serve(route):
            nonlocal list_reads_while_blocked
            url = urlsplit(route.request.url)
            if url.path == "/dashboard/orders/sold":
                if slow_chat.is_set() and not release_chat.is_set():
                    list_reads_while_blocked += 1
                    if list_reads_while_blocked >= 3:
                        refreshed.set()
                body = sold_html
                if list_reads_while_blocked >= 2:
                    body += '<a class="grid-row" href="/order/third"><span class="game-name">Aion 2</span></a>'
            elif url.path.startswith("/order/"):
                key = url.path.rsplit("/", 1)[-1]
                if key == "third" and not reads_before_third:
                    reads_before_third.update(detail_requests)
                detail_requests[key] += 1
                body = (f'<div class="order-container">{key}</div>'
                        f'<iframe src="https://app.talkjs.com/chat/{key}"></iframe>')
            elif url.path.startswith("/chat/"):
                key = url.path.rsplit("/", 1)[-1]
                if key == "first" and not release_chat.is_set():
                    slow_chat.set()
                    await release_chat.wait()
                messages = [key]
                if key == "first" and detail_requests[key] >= 3:
                    messages.append("new reply")
                rows = "".join(f'<div class="MessageDiv"><span class="MessageBody__message-text">'
                               f'{msg}</span></div>' for msg in messages)
                body = f'<div id="chat-scrollable">{rows}</div>'
            else:
                await route.abort()
                return
            await route.fulfill(content_type="text/html", body=body)

        async with async_playwright() as playwright, await playwright.chromium.launch() as browser:
            ctx = await browser.new_context()
            await ctx.route("**/*", serve)
            page = await ctx.new_page()
            await page.goto(SOLD_URL)
            args = SimpleNamespace(interval=0.15, url_contains="/order/", order_selector=".order-container",
                                   messages_frame_selector='iframe[src^="https://app.talkjs.com/"]',
                                   messages_selector="#chat-scrollable")
            stream = io.StringIO()
            hub = EventHub(stream)

            def snapshots():
                return [row for line in stream.getvalue().splitlines()
                        if (row := json.loads(line)).get("status") == "ready"]

            async def wait_for_new_reply():
                while not any(any(m["text"] == "new reply" for m in row["messages"]) for row in snapshots()):
                    await asyncio.sleep(0.03)

            with contextlib.redirect_stdout(io.StringIO()), patch.object(chat, "SETTLE_SECONDS", 0.06), \
                 patch.object(chat, "POLL_SECONDS", 0.02):
                task = asyncio.create_task(monitor_sold(browser, hub, args))
                try:
                    await asyncio.wait_for(refreshed.wait(), timeout=8)
                    self.assertEqual(detail_requests, {"first": 1})
                    release_chat.set()
                    await asyncio.wait_for(wait_for_new_reply(), timeout=10)
                finally:
                    release_chat.set()
                    task.cancel()
                    with contextlib.suppress(asyncio.CancelledError):
                        await task

            records = [(urlsplit(row["url"]).path, [m["text"] for m in row["messages"]])
                       for row in snapshots()]
            self.assertEqual(records, [("/order/first", ["first"]), ("/order/second", ["second"]),
                                       ("/order/third", ["third"]),
                                       ("/order/first", ["first", "new reply"])])
            self.assertEqual(reads_before_third["second"], 1)
            self.assertLessEqual(reads_before_third["first"], 2)
            self.assertNotIn("other", detail_requests)
            self.assertEqual(ctx.pages, [page])


if __name__ == "__main__":
    unittest.main()
