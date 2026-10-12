"""验证 ELD API 无需浏览器，以及混合目标、DOM 兜底和资源释放。"""
import asyncio
import contextlib
import io
import json
import unittest
from datetime import datetime
from unittest.mock import AsyncMock, MagicMock, patch
from urllib.parse import parse_qs, urlparse

from g2g import config, db
from tools import crawl_from_db


def api_response(status=200, records=None):
    response = MagicMock(status=status, ok=200 <= status < 300)
    response.json = AsyncMock(return_value={"results": records or []})
    response.dispose = AsyncMock()
    return response


def records():
    return [{"offer": {"id": str(i), "pricePerUnitInUSD": {"amount": i + 1}},
             "user": {"username": f"seller{i}"}} for i in range(8)]


class CrawlSessionTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.request = MagicMock()
        self.request.get = AsyncMock()
        self.request.dispose = AsyncMock()
        self.context = MagicMock()
        self.context.add_cookies = AsyncMock()
        self.context.add_init_script = AsyncMock()
        self.context.new_page = AsyncMock()
        self.context.close = AsyncMock()
        self.browser = MagicMock()
        self.browser.new_context = AsyncMock(return_value=self.context)
        self.browser.close = AsyncMock()
        self.playwright = MagicMock()
        self.playwright.request.new_context = AsyncMock(return_value=self.request)
        self.playwright.chromium.launch = AsyncMock(return_value=self.browser)
        self.manager = MagicMock()
        self.manager.__aenter__ = AsyncMock(return_value=self.playwright)
        self.manager.__aexit__ = AsyncMock()

    async def test_eld_only_round_saves_snapshots_without_launching_chromium(self):
        targets = [{"id": i, "game_product_id": 1, "category": "ELD游戏币",
                    "url": f"https://www.eldorado.gg/currency/g/{i}"} for i in (24, 26, 40)]
        responses = [api_response(records=records()) for _ in targets]
        self.request.get.side_effect = responses
        with contextlib.ExitStack() as stack:
            stack.enter_context(contextlib.redirect_stdout(io.StringIO()))
            stack.enter_context(patch.object(crawl_from_db, "async_playwright", return_value=self.manager))
            stack.enter_context(patch.object(config, "CRAWL_EXCLUDE_STORES", "seller0"))
            stack.enter_context(patch.object(config, "ELD_OFFER_PAGE_SIZE", 8))
            stack.enter_context(patch.object(db, "get_pending_targets", return_value=targets))
            stack.enter_context(patch.object(db, "increment_version", return_value=9))
            save = stack.enter_context(patch.object(db, "save_crawl_data", return_value=7))
            update = stack.enter_context(patch.object(db, "update_last_crawl"))
            notify = stack.enter_context(patch.object(db, "insert_crawl_notify"))
            self.assertEqual(await crawl_from_db.run(0, 2), 0)
        self.playwright.chromium.launch.assert_not_awaited()
        self.browser.new_context.assert_not_awaited()
        self.playwright.request.new_context.assert_awaited_once()
        self.assertEqual(save.call_count, 3)
        for target, call in zip(targets, save.call_args_list):
            self.assertEqual(call.args[:2], (target["id"], "eldorado"))
            self.assertEqual([row["seller_name"] for row in call.args[2]],
                             [f"seller{i}" for i in range(1, 8)])
            self.assertEqual(call.kwargs, {"game_product_id": 1, "version": 9})
        self.assertEqual(update.call_count, 3)
        self.assertEqual([call.args for call in notify.call_args_list],
                         [(i, 9, 7) for i in (24, 26, 40)])
        self.request.dispose.assert_awaited_once()
        for response in responses:
            response.dispose.assert_awaited_once()
        self.manager.__aexit__.assert_awaited_once()

    async def test_no_due_or_valid_targets_does_not_start_playwright(self):
        now = datetime(2026, 10, 12, 12)
        targets = [
            {"id": 1, "game_product_id": 0, "url": "https://example.test"},
            {"id": 2, "game_product_id": 1, "url": ""},
            {"id": 3, "game_product_id": 1, "url": "https://example.test",
             "last_crawl_at": now, "crawl_interval": 60},
        ]
        with patch.object(db, "get_pending_targets", return_value=targets), \
             patch.object(crawl_from_db.time, "time", return_value=now.timestamp()), \
             patch.object(crawl_from_db, "async_playwright") as playwright, \
             patch.object(db, "increment_version") as increment, \
             contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(await crawl_from_db.run(), 0)
        playwright.assert_not_called()
        increment.assert_not_called()

    async def test_all_excluded_api_result_is_saved_as_zero_without_browser_fallback(self):
        target = {"id": 24, "game_product_id": 1, "category": "ELD游戏币",
                  "url": "https://www.eldorado.gg/currency/g/24"}
        self.request.get.return_value = api_response(records=records())
        with patch.object(crawl_from_db, "async_playwright", return_value=self.manager), \
             patch.object(config, "CRAWL_EXCLUDE_STORES", ",".join(f"seller{i}" for i in range(8))), \
             patch.object(db, "get_pending_targets", return_value=[target]), \
             patch.object(db, "increment_version", return_value=9), \
             patch.object(db, "save_crawl_data", return_value=0) as save, \
             patch.object(db, "update_last_crawl") as update, \
             patch.object(db, "insert_crawl_notify") as notify, \
             contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(await crawl_from_db.run(), 0)
        self.assertEqual(save.call_args.args[2], [])
        update.assert_called_once()
        notify.assert_called_once_with(24, 9, 0)
        self.playwright.chromium.launch.assert_not_awaited()
        self.request.dispose.assert_awaited_once()

    async def test_mixed_targets_reuse_browser_and_keep_eld_context_isolated(self):
        failed, success = api_response(status=403), api_response(records=records())
        self.request.get.side_effect = [failed, success]
        with patch.object(crawl_from_db, "async_playwright", return_value=self.manager), \
             patch.object(crawl_from_db, "scrape_eldorado_dom", new=AsyncMock(return_value=[])) as dom, \
             contextlib.redirect_stdout(io.StringIO()):
            async with crawl_from_db.CrawlSession() as session:
                first_page = await session.get_g2g_page()
                self.assertEqual(await session.scrape_eldorado("https://www.eldorado.gg/currency/g/24"), [])
                # Empty DOM result is a successful result; later API success still uses API.
                self.assertEqual(len(await session.scrape_eldorado("https://www.eldorado.gg/currency/g/26")), 8)
                self.assertIs(await session.get_g2g_page(), first_page)
        self.playwright.chromium.launch.assert_awaited_once()
        self.assertEqual(self.browser.new_context.await_count, 2)
        self.assertEqual(self.context.new_page.await_count, 2)
        g2g_cookies, eld_cookies = [call.args[0] for call in self.context.add_cookies.await_args_list]
        self.assertEqual(g2g_cookies[0]["domain"], ".g2g.com")
        self.assertEqual({cookie["name"] for cookie in eld_cookies},
                         {"eldoradogg_currencyPreference", "eldoradogg_locale"})
        dom.assert_awaited_once()
        self.context.close.assert_awaited_once()
        self.browser.close.assert_awaited_once()
        self.request.dispose.assert_awaited_once()
        failed.dispose.assert_awaited_once()
        success.dispose.assert_awaited_once()

    async def test_api_failure_conditions_fall_back_once_and_release_response(self):
        for failure in ("http", "json", "empty", "network", "url"):
            with self.subTest(failure=failure):
                self.setUp()
                response = api_response(status=503 if failure == "http" else 200)
                if failure == "json":
                    response.json.side_effect = ValueError("invalid JSON")
                self.request.get.return_value = response
                if failure == "network":
                    self.request.get.side_effect = TimeoutError("request timed out")
                url = "https://www.eldorado.gg/currency/g/24" if failure != "url" else "https://www.eldorado.gg/fixture"
                with patch.object(crawl_from_db, "async_playwright", return_value=self.manager), \
                     patch.object(crawl_from_db, "scrape_eldorado_dom", new=AsyncMock(return_value=[{"price": "2"}])) as dom, \
                     contextlib.redirect_stdout(io.StringIO()):
                    async with crawl_from_db.CrawlSession() as session:
                        self.assertEqual(await session.scrape_eldorado(url), [{"price": "2"}])
                dom.assert_awaited_once_with(self.context.new_page.return_value, url)
                self.playwright.chromium.launch.assert_awaited_once()
                self.context.close.assert_awaited_once()
                self.browser.close.assert_awaited_once()
                self.request.dispose.assert_awaited_once()
                if failure not in ("network", "url"):
                    response.dispose.assert_awaited_once()
                else:
                    response.dispose.assert_not_awaited()

    async def test_failed_dom_target_sends_no_notification_and_next_api_target_runs(self):
        targets = [{"id": i, "game_product_id": 1, "category": "ELD游戏币",
                    "url": f"https://www.eldorado.gg/currency/g/{i}"} for i in (24, 26)]
        self.request.get.side_effect = [api_response(status=403), api_response(records=records())]
        with patch.object(crawl_from_db, "async_playwright", return_value=self.manager), \
             patch.object(crawl_from_db, "scrape_eldorado_dom", new=AsyncMock(side_effect=RuntimeError("DOM failed"))), \
             patch.object(config, "CRAWL_EXCLUDE_STORES", ""), \
             patch.object(db, "get_pending_targets", return_value=targets), \
             patch.object(db, "increment_version", return_value=9), \
             patch.object(db, "save_crawl_data", return_value=8) as save, \
             patch.object(db, "update_last_crawl") as update, \
             patch.object(db, "insert_crawl_notify") as notify, \
             contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(await crawl_from_db.run(), 1)
        save.assert_called_once()
        self.assertEqual(save.call_args.args[0], 26)
        update.assert_called_once()
        self.assertEqual(update.call_args.args[0], 26)
        notify.assert_called_once_with(26, 9, 8)
        self.context.close.assert_awaited_once()
        self.browser.close.assert_awaited_once()
        self.request.dispose.assert_awaited_once()


class StandaloneRequestTests(unittest.IsolatedAsyncioTestCase):
    async def test_real_playwright_request_can_fetch_and_parse_without_chromium(self):
        received = []

        async def serve(reader, writer):
            try:
                headers = (await reader.readuntil(b"\r\n\r\n")).decode()
                received.append(headers)
                body = json.dumps({"results": records()}).encode()
                writer.write(b"HTTP/1.1 200 OK\r\nContent-Type: application/json\r\n"
                             + f"Content-Length: {len(body)}\r\nConnection: close\r\n\r\n".encode() + body)
                await writer.drain()
            finally:
                writer.close()
                await writer.wait_closed()

        server = await asyncio.start_server(serve, "127.0.0.1", 0)
        port = server.sockets[0].getsockname()[1]
        original_api = crawl_from_db.scrape_eldorado_api

        class LocalRequest:
            def __init__(self, request):
                self.request = request

            async def get(self, url, **kwargs):
                return await self.request.get(f"http://127.0.0.1:{port}/?{urlparse(url).query}", **kwargs)

        async def local_api(request, url):
            return await original_api(LocalRequest(request), url)

        async with server:
            with patch.object(crawl_from_db, "scrape_eldorado_api", side_effect=local_api), \
                 patch.object(config, "CRAWL_EXCLUDE_STORES", ""), \
                 contextlib.redirect_stdout(io.StringIO()):
                async with crawl_from_db.CrawlSession() as session:
                    playwright = await session.get_playwright()
                    with patch.object(playwright.chromium, "launch", new=AsyncMock(side_effect=AssertionError("browser is unnecessary"))) as launch:
                        for target in (24, 26):
                            rows = await session.scrape_eldorado(f"https://www.eldorado.gg/currency/g/{target}")
                            self.assertEqual([row["seller_name"] for row in rows], [f"seller{i}" for i in range(8)])
                        launch.assert_not_awaited()
                        state = await session.eldorado_request.storage_state()
                        self.assertEqual({cookie["name"]: cookie["value"] for cookie in state["cookies"]},
                                         {"eldoradogg_currencyPreference": "USD", "eldoradogg_locale": "en-US"})
        self.assertEqual(len(received), 2)
        for headers in received:
            self.assertIn(f"User-Agent: {crawl_from_db.ELDORADO_USER_AGENT}".lower(), headers.lower())
            params = parse_qs(urlparse(headers.splitlines()[0].split()[1]).query)
            self.assertEqual(params["pageIndex"], ["1"])
            self.assertEqual(params["pageSize"], ["8"])


if __name__ == "__main__":
    unittest.main()
