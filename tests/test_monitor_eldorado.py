"""本地验证订单页面快照与变化输出，不访问真实账户。"""

import asyncio
import contextlib
import io
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from playwright.async_api import async_playwright

from tools.monitor_eldorado import (EventHub, allowed_url, chrome_debug_endpoint, monitor,
                                    read_messages, safe_page_url, visible_text)


class EventHubTests(unittest.TestCase):
    def test_only_changed_snapshots_are_emitted_to_stdout_and_file(self):
        output, stdout = io.StringIO(), io.StringIO()
        hub = EventHub(output)
        snapshots = [
            {"status": "waiting"},
            {"status": "ready", "order": ["订单 1"], "messages": ["你好"]},
            {"status": "ready", "order": ["订单 1"], "messages": ["你好", "回复"]},
        ]
        with contextlib.redirect_stdout(stdout):
            for snapshot in snapshots:
                hub.publish(snapshot)
                hub.publish(snapshot)
        self.assertEqual([json.loads(line) for line in stdout.getvalue().splitlines()], snapshots)
        self.assertEqual(output.getvalue(), stdout.getvalue())

    def test_url_requires_exact_https_host_and_path_marker(self):
        self.assertTrue(allowed_url("https://www.eldorado.gg/order/123", "/order/"))
        for url in ("http://www.eldorado.gg/order/123",
                    "https://www.eldorado.gg.attacker.test/order/123",
                    "https://www.eldorado.gg/login"):
            self.assertFalse(allowed_url(url, "/order/"))
        self.assertEqual(safe_page_url("https://www.eldorado.gg/order/123?token=secret#token"),
                         "https://www.eldorado.gg/order/123")


class ChromeDebugEndpointTests(unittest.TestCase):
    def test_reads_dynamic_port_and_browser_endpoint(self):
        with tempfile.TemporaryDirectory() as directory:
            active_port = Path(directory) / "DevToolsActivePort"
            active_port.write_text("49231\n/devtools/browser/example-id\n", encoding="utf-8")
            self.assertEqual(chrome_debug_endpoint(active_port),
                             "ws://127.0.0.1:49231/devtools/browser/example-id")

    def test_rejects_invalid_file_without_exposing_contents(self):
        with tempfile.TemporaryDirectory() as directory:
            active_port = Path(directory) / "DevToolsActivePort"
            for content in ("secret", "0\n/devtools/browser/secret", "65536\n/devtools/browser/secret",
                            "9222\n/other/secret", "9222\n/devtools/browser/secret?token=secret"):
                with self.subTest(content=content):
                    active_port.write_text(content, encoding="utf-8")
                    with self.assertRaises(ValueError) as error:
                        chrome_debug_endpoint(active_port)
                    self.assertNotIn("secret", str(error.exception))
            active_port.unlink()
            with self.assertRaises(ValueError):
                chrome_debug_endpoint(active_port)


class MonitorTests(unittest.IsolatedAsyncioTestCase):
    async def test_navigation_during_reads_does_not_publish_stale_order(self):
        page = SimpleNamespace(url="https://www.eldorado.gg/order/1")
        context = SimpleNamespace(pages=[page])
        browser = SimpleNamespace(contexts=[context])
        hub = SimpleNamespace(publish=unittest.mock.Mock())
        args = SimpleNamespace(url_contains="/order/", order_selector="#order",
                               messages_frame_selector="iframe", messages_selector="#messages", interval=1)

        async def read_messages_then_navigate(*_args):
            page.url = "https://www.eldorado.gg/login"
            return True, True, ["private text"]

        with patch("tools.monitor_eldorado.visible_text", new=AsyncMock(return_value=["private text"])), \
             patch("tools.monitor_eldorado.read_messages", side_effect=read_messages_then_navigate), \
             patch("tools.monitor_eldorado.asyncio.sleep", new=AsyncMock(side_effect=asyncio.CancelledError)):
            with self.assertRaises(asyncio.CancelledError):
                await monitor(browser, hub, args)
        hub.publish.assert_called_once_with({"status": "waiting"})

    async def test_visible_text_reads_rendered_text_only(self):
        async with async_playwright() as playwright:
            browser = await playwright.chromium.launch(headless=True)
            try:
                page = await browser.new_page()
                await page.set_content('<div class="msg"> Hello   world </div>'
                                       '<div class="msg" style="display:none">hidden</div>'
                                       '<div class="msg">你好</div>')
                self.assertEqual(await visible_text(page, ".msg"), ["Hello world", "你好"])
            finally:
                await browser.close()

    async def test_cross_origin_frame_reads_updates_and_reports_absence(self):
        async with async_playwright() as playwright:
            browser = await playwright.chromium.launch(headless=True)
            try:
                page = await browser.new_page()

                async def serve(route):
                    if "talkjs.test" in route.request.url:
                        await route.fulfill(content_type="text/html; charset=utf-8", body='<div class="message">你好</div>')
                    else:
                        await route.fulfill(content_type="text/html", body='<iframe src="https://talkjs.test/chat"></iframe>')

                await page.route("**/*", serve)
                await page.goto("https://eldorado.test/order/1")
                selector = 'iframe[src*="talkjs"]'
                self.assertEqual(await read_messages(page, selector, ".message"),
                                 (True, True, [{"text": "你好", "source": "fallback"}]))
                frame = page.frame(url="https://talkjs.test/chat")
                await frame.evaluate("document.body.insertAdjacentHTML('beforeend', '<div class=message>回复</div>')")
                self.assertEqual(await read_messages(page, selector, ".message"),
                                 (True, True, [{"text": "你好", "source": "fallback"},
                                               {"text": "回复", "source": "fallback"}]))
                await frame.evaluate('''document.body.innerHTML = `<div id="chat-scrollable">
                    <div class="MessageDiv UserMessage__by-me"><div class="MessageBody__message-text">hello</div>
                    <span role="time" title="2026-11-03 12:00">just now</span>
                    <a href="https://secret.test/credential">file.txt</a></div></div>`''')
                self.assertEqual(await read_messages(page, selector, "#chat-scrollable"),
                                 (True, True, [{"text": "hello", "time": "2026-11-03 12:00",
                                                "direction": "outgoing", "attachments": ["file.txt"]}]))
                before = await read_messages(page, selector, "#chat-scrollable")
                await frame.locator('[role="time"]').evaluate("el => el.textContent = '2 minutes ago'")
                after = await read_messages(page, selector, "#chat-scrollable")
                self.assertEqual(before, after)
                stdout = io.StringIO()
                with contextlib.redirect_stdout(stdout):
                    hub = EventHub()
                    hub.publish({"messages": before[2]})
                    hub.publish({"messages": after[2]})
                self.assertEqual(len(stdout.getvalue().splitlines()), 1)
                self.assertEqual(await read_messages(page, selector, ".missing"), (True, False, []))
                await page.locator("iframe").evaluate("el => el.remove()")
                self.assertEqual(await read_messages(page, selector, ".message"), (False, False, []))
            finally:
                await browser.close()


if __name__ == "__main__":
    unittest.main()
