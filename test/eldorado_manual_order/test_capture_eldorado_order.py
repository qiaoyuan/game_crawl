"""在本地拦截的页面中验证手动采集的订单选择、分步执行和写入位置。"""

import argparse
import json
import tempfile
import unittest
from contextlib import ExitStack
from functools import partial
from pathlib import Path
from unittest.mock import AsyncMock, patch
from urllib.parse import urlsplit

from playwright.async_api import async_playwright

from g2g.eldorado.manual import capture_eldorado_order as manual, eldorado_manual_records as records


_ID = "ca001122-3344-4567-8899-aabbccddeeff"
_OTHER = "aa001122-3344-4567-8899-aabbccddeeff"
_URL = "https://www.eldorado.gg/order/" + _ID


def seller_detail(order_id):
    values = {"Game": "Aion 2", "Region": "EU", "Faction": "Example", "Server": "Sample",
              "Quantity": "12 M", "Buyer": "test-buyer"}
    fields = "".join(f"<div><span>{k}</span><span>{v}</span></div>" for k, v in values.items())
    prices = {"Order Price": "$8.00", "Commission": "-$0.50", "You receive": "$7.50"}
    payments = "".join(f"<div><label>{k}</label><span>{v}</span></div>" for k, v in prices.items())
    return f"""<eld-order-header><div class="order-id"><span>Order ID: {order_id}</span></div></eld-order-header>
        <eld-seller-order-details-card><div class="order-details-card">{fields}</div></eld-seller-order-details-card>
        <eld-order-seller-payment><div class="prices-container">{payments}</div></eld-order-seller-payment>
        <iframe src="https://app.talkjs.com/chat"></iframe>"""


class ManualCaptureTests(unittest.IsolatedAsyncioTestCase):
    def test_target_requires_hyphenated_uuid_or_same_site_order_url(self):
        for target in [_ID, _ID.upper(), _URL, _URL + "/?ignore=token#ignored"]:
            self.assertEqual(manual.parse_order_id(target), _ID)
        for bad in [_ID.replace("-", ""), _URL + "/cancel", "https://other.test/order/" + _ID,
                    _URL.replace("https:", "http:"), _URL.replace("www.", "user:pass@www.")]:
            with self.assertRaises(argparse.ArgumentTypeError):
                manual.parse_order_id(bad)

    async def test_detail_then_explicit_chat_use_only_requested_id_and_keep_existing_tabs(self):
        detail_requests = []

        async def serve(route):
            path = urlsplit(route.request.url).path
            if path.startswith("/order/"):
                detail_requests.append(route.request.url)
                body = seller_detail(path.rsplit("/", 1)[-1])
            elif path == "/chat":
                body = ('<div id="chat-scrollable"><div class="MessageDiv UserMessage__by-other">'
                        '<span class="MessageBody__message-text">example reply</span></div></div>')
            else:
                body = "<p>Dashboard</p>"
            await route.fulfill(content_type="text/html; charset=utf-8", body=body)

        async with async_playwright() as pw, await pw.chromium.launch() as browser:
            context = await browser.new_context()
            await context.route("**/*", serve)
            other = await context.new_page()
            await other.goto(_URL.replace(_ID, _OTHER))
            with ExitStack() as stack:
                root = stack.enter_context(tempfile.TemporaryDirectory())
                stack.enter_context(patch.object(manual, "save_order", partial(records.save_order, root=root)))
                stack.enter_context(patch.object(manual, "save_chat", partial(records.save_chat, root=root)))
                with patch.object(manual, "read_chat_history", new_callable=AsyncMock) as read_chat:
                    path, created, count = await manual.capture(browser, "order", _URL + "?private=secret")
                    read_chat.assert_not_awaited()
                self.assertTrue(created)
                self.assertEqual(count, 1)
                self.assertEqual(detail_requests[-1], _URL)
                self.assertEqual(path, Path(root) / _ID / "order.jsonl")
                self.assertEqual(json.loads(path.read_text())["quantity"], "12 M")
                self.assertFalse(path.with_name("chat.jsonl").exists())
                self.assertEqual(context.pages, [other])

                exact = await context.new_page()
                await exact.goto(_URL)
                opened = list(detail_requests)
                with patch.object(manual, "read_order_fields", new_callable=AsyncMock) as read_order:
                    chat_path, changed, count = await manual.capture(browser, "chat", _ID)
                    read_order.assert_not_awaited()
                self.assertTrue(changed)
                self.assertEqual(count, 1)
                self.assertEqual(detail_requests, opened)
                self.assertEqual(context.pages, [other, exact])
                snapshot = json.loads(chat_path.read_text())
                self.assertEqual(snapshot["order_id"], _ID)
                self.assertEqual([m["text"] for m in snapshot["messages"]], ["example reply"])

                async def moved(page, selectors):
                    await page.goto(_URL.replace(_ID, _OTHER))
                    return [{"text": "wrong order"}]

                with patch.object(manual, "read_chat_history", side_effect=moved), self.assertRaises(ValueError):
                    await manual.capture(browser, "chat", _ID)
                self.assertEqual(json.loads(chat_path.read_text()), snapshot)

    async def test_ambiguous_browser_context_cannot_open_an_order(self):
        async with async_playwright() as pw, await pw.chromium.launch() as browser:
            for _ in range(2):
                context = await browser.new_context()
                await context.route("**/*", lambda route: route.fulfill(body="Dashboard"))
                page = await context.new_page()
                await page.goto("https://www.eldorado.gg/dashboard/orders/sold")
            with self.assertRaisesRegex(ValueError, "唯一"):
                await manual.capture(browser, "order", _ID)
            self.assertEqual([len(ctx.pages) for ctx in browser.contexts], [1, 1])
