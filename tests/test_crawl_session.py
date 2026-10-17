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


def browser_context():
    page = MagicMock()
    page.goto = AsyncMock()
    page.close = AsyncMock()
    context = MagicMock()
    context.add_cookies = AsyncMock()
    context.add_init_script = AsyncMock()
    context.new_page = AsyncMock(return_value=page)
    context.close = AsyncMock()
    return context


def chromium(context):
    browser = MagicMock()
    browser.new_context = AsyncMock(return_value=context)
    browser.close = AsyncMock()
    browser.is_connected.return_value = True
    return browser


class CrawlSessionTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.request = MagicMock()
        self.request.get = AsyncMock()
        self.request.dispose = AsyncMock()
        self.context = browser_context()
        self.browser = chromium(self.context)
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

    async def test_both_eld_types_save_numeric_price_order_without_strategy_filter(self):
        targets = [{"id": i + 1, "game_product_id": 1, "category": "ELD游戏币", "crawl_type": i,
                    "url": "https://www.eldorado.gg/currency/g/220?offerSortingCriterion=MostPopular"}
                   for i in (0, 1)]
        offers = list(reversed(records()))
        for record in offers:
            record["offer"]["quantity"] = 1
        self.request.get.side_effect = [api_response(records=offers) for _ in targets]
        with contextlib.ExitStack() as stack:
            stack.enter_context(contextlib.redirect_stdout(io.StringIO()))
            stack.enter_context(patch.object(crawl_from_db, "async_playwright", return_value=self.manager))
            stack.enter_context(patch.object(config, "CRAWL_EXCLUDE_STORES", ""))
            stack.enter_context(patch.object(db, "get_pending_targets", return_value=targets))
            policies = stack.enter_context(patch.object(db, "get_crawl_strategies"))
            stack.enter_context(patch.object(db, "increment_version", return_value=9))
            save = stack.enter_context(patch.object(db, "save_crawl_data", return_value=8))
            stack.enter_context(patch.object(db, "update_last_crawl"))
            notify = stack.enter_context(patch.object(db, "insert_crawl_notify"))
            self.assertEqual(await crawl_from_db.run(), 0)
        policies.assert_not_called()
        self.assertEqual(save.call_count, 2)
        for call in save.call_args_list:
            self.assertEqual([row["price"] for row in call.args[2]], [str(i) for i in range(1, 9)])
            self.assertTrue(all(row["stock"] == "1" for row in call.args[2]))
        self.assertEqual([call.args for call in notify.call_args_list], [(1, 9, 8), (2, 9, 8)])
        self.playwright.chromium.launch.assert_not_awaited()

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
        eld_context = browser_context()
        self.browser.new_context.side_effect = [self.context, eld_context]
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
        self.context.new_page.assert_awaited_once()
        eld_context.new_page.assert_awaited_once()
        g2g_cookies = self.context.add_cookies.await_args.args[0]
        eld_cookies = eld_context.add_cookies.await_args.args[0]
        self.assertEqual(g2g_cookies[0]["domain"], ".g2g.com")
        self.assertEqual({cookie["name"] for cookie in eld_cookies},
                         {"eldoradogg_currencyPreference", "eldoradogg_locale"})
        dom.assert_awaited_once()
        self.context.close.assert_awaited_once()
        eld_context.close.assert_awaited_once()
        self.browser.close.assert_awaited_once()
        self.request.dispose.assert_awaited_once()
        failed.dispose.assert_awaited_once()
        success.dispose.assert_awaited_once()

    async def test_park_reuses_g2g_page_context_and_browser_until_session_exit(self):
        with patch.object(crawl_from_db, "async_playwright", return_value=self.manager), \
             contextlib.redirect_stdout(io.StringIO()):
            async with crawl_from_db.CrawlSession() as session:
                page = await session.get_g2g_page()
                await session.park_g2g_page()
                self.assertIs(session.g2g_context, self.context)
                self.assertIs(await session.get_g2g_page(), page)
                self.context.close.assert_not_awaited()
                self.browser.close.assert_not_awaited()
        page.goto.assert_awaited_once_with("about:blank", wait_until="commit", timeout=3000)
        self.playwright.chromium.launch.assert_awaited_once()
        self.browser.new_context.assert_awaited_once()
        self.context.close.assert_awaited_once()
        self.browser.close.assert_awaited_once()

    async def test_failed_park_closes_context_and_rebuilds_on_same_browser(self):
        replacement = browser_context()
        self.browser.new_context.side_effect = [self.context, replacement]
        self.context.new_page.return_value.goto.side_effect = TimeoutError("park timed out")
        with patch.object(crawl_from_db, "async_playwright", return_value=self.manager), \
             contextlib.redirect_stdout(io.StringIO()):
            async with crawl_from_db.CrawlSession() as session:
                await session.get_g2g_page()
                await session.park_g2g_page()
                self.assertIsNone(session.page)
                self.assertIsNone(session.g2g_context)
                self.context.close.assert_awaited_once()
                self.assertIs(await session.get_g2g_page(), replacement.new_page.return_value)
        self.playwright.chromium.launch.assert_awaited_once()
        self.assertEqual(self.browser.new_context.await_count, 2)
        self.context.close.assert_awaited_once()
        replacement.close.assert_awaited_once()
        self.browser.close.assert_awaited_once()

    async def test_failed_context_close_retires_browser_without_disposing_eld_request(self):
        replacement_context = browser_context()
        replacement_browser = chromium(replacement_context)
        self.playwright.chromium.launch.side_effect = [self.browser, replacement_browser]
        self.context.new_page.return_value.goto.side_effect = TimeoutError("park timed out")
        self.context.close.side_effect = RuntimeError("context close failed")
        self.request.get.return_value = api_response(records=records())
        with patch.object(crawl_from_db, "async_playwright", return_value=self.manager), \
             contextlib.redirect_stdout(io.StringIO()):
            async with crawl_from_db.CrawlSession() as session:
                await session.scrape_eldorado("https://www.eldorado.gg/currency/g/24")
                await session.get_g2g_page()
                await session.park_g2g_page()
                self.browser.close.assert_awaited_once()
                self.assertIsNone(session.browser)
                self.assertIs(session.eldorado_request, self.request)
                self.request.dispose.assert_not_awaited()
                self.assertIs(await session.get_g2g_page(), replacement_context.new_page.return_value)
        self.assertEqual(self.playwright.chromium.launch.await_count, 2)
        self.context.close.assert_awaited_once()
        replacement_context.close.assert_awaited_once()
        replacement_browser.close.assert_awaited_once()
        self.request.dispose.assert_awaited_once()

    async def test_disconnected_browser_rebuilds_instead_of_returning_stale_page(self):
        replacement_context = browser_context()
        replacement_browser = chromium(replacement_context)
        self.playwright.chromium.launch.side_effect = [self.browser, replacement_browser]
        with patch.object(crawl_from_db, "async_playwright", return_value=self.manager), \
             contextlib.redirect_stdout(io.StringIO()):
            async with crawl_from_db.CrawlSession() as session:
                old_page = await session.get_g2g_page()
                self.browser.is_connected.return_value = False
                new_page = await session.get_g2g_page()
                self.assertIsNot(new_page, old_page)
                self.assertIs(new_page, replacement_context.new_page.return_value)
        self.context.close.assert_awaited_once()
        self.browser.close.assert_awaited_once()
        replacement_context.close.assert_awaited_once()
        replacement_browser.close.assert_awaited_once()
        self.manager.__aenter__.assert_awaited_once()

    async def test_partial_g2g_initialization_failure_releases_context_and_preserves_error(self):
        for phase in ("add_cookies", "add_init_script", "new_page"):
            with self.subTest(phase=phase):
                self.setUp()
                replacement = browser_context()
                self.browser.new_context.side_effect = [self.context, replacement]
                error = RuntimeError(f"{phase} failed")
                getattr(self.context, phase).side_effect = error
                with patch.object(crawl_from_db, "async_playwright", return_value=self.manager), \
                     contextlib.redirect_stdout(io.StringIO()):
                    async with crawl_from_db.CrawlSession() as session:
                        with self.assertRaises(RuntimeError) as caught:
                            await session.get_g2g_page()
                        self.assertIs(caught.exception, error)
                        self.assertIsNone(session.page)
                        self.assertIsNone(session.g2g_context)
                        self.context.close.assert_awaited_once()
                        self.assertIs(await session.get_g2g_page(), replacement.new_page.return_value)
                self.context.close.assert_awaited_once()
                replacement.close.assert_awaited_once()
                self.browser.close.assert_awaited_once()
                self.playwright.chromium.launch.assert_awaited_once()

    async def test_cleanup_failure_keeps_completed_result_and_prevents_duplicate_browser(self):
        target = {"id": 18, "game_product_id": 20, "category": "物品",
                  "url": "https://www.g2g.com/fixture"}
        self.context.new_page.return_value.goto.side_effect = TimeoutError("park timed out")
        self.context.close.side_effect = RuntimeError("context stuck")
        self.browser.close.side_effect = RuntimeError("browser stuck")
        with patch.object(crawl_from_db, "async_playwright", return_value=self.manager), \
             patch.object(crawl_from_db, "scrape_page", new=AsyncMock(return_value=[{"price": "1"}])), \
             patch.object(db, "increment_version", return_value=9), \
             patch.object(db, "save_crawl_data", return_value=1) as save, \
             patch.object(db, "update_last_crawl"), \
             patch.object(db, "insert_crawl_notify") as notify, \
             contextlib.redirect_stdout(io.StringIO()):
            async with crawl_from_db.CrawlSession() as session:
                result = await crawl_from_db.crawl_target(session, 0, target, 1)
                self.assertEqual(result[:2], (1, False))
                self.assertIsNone(session.page)
                self.assertIsNone(session.g2g_context)
                self.assertTrue(session.browser_cleanup_pending)
                with self.assertRaisesRegex(RuntimeError, "旧 Chromium 尚未回收"):
                    await session.get_g2g_page()
                self.playwright.chromium.launch.assert_awaited_once()
                self.browser.close.side_effect = None
        save.assert_called_once()
        notify.assert_called_once_with(18, 9, 1)
        self.context.close.assert_awaited_once()
        self.manager.__aexit__.assert_awaited_once()

    async def test_hung_context_close_is_cancelled_then_browser_is_closed(self):
        cancelled = []
        original_wait_for = asyncio.wait_for

        async def hung_close():
            try:
                await asyncio.Event().wait()
            finally:
                cancelled.append(True)

        async def quick_timeout(awaitable, timeout):
            return await original_wait_for(awaitable, min(timeout, 0.01))

        self.context.close.side_effect = hung_close
        self.context.new_page.return_value.goto.side_effect = TimeoutError("park timed out")
        with patch.object(crawl_from_db, "async_playwright", return_value=self.manager), \
             patch.object(crawl_from_db.asyncio, "wait_for", side_effect=quick_timeout) as wait, \
             contextlib.redirect_stdout(io.StringIO()):
            async with crawl_from_db.CrawlSession() as session:
                await session.get_g2g_page()
                await session.park_g2g_page()
                self.assertIsNone(session.browser)
        self.assertEqual(cancelled, [True])
        self.browser.close.assert_awaited_once()
        self.assertIn(3, [call.kwargs["timeout"] for call in wait.await_args_list])

    async def test_cancelled_browser_cleanup_still_disposes_request_and_driver(self):
        with patch.object(crawl_from_db, "async_playwright", return_value=self.manager), \
             patch.object(crawl_from_db, "scrape_eldorado_api", new=AsyncMock(return_value=[])), \
             contextlib.redirect_stdout(io.StringIO()):
            session = crawl_from_db.CrawlSession()
            await session.__aenter__()
            await session.scrape_eldorado("https://www.eldorado.gg/currency/g/24")
            with patch.object(session, "_close_browser", new=AsyncMock(side_effect=asyncio.CancelledError)):
                with self.assertRaises(asyncio.CancelledError):
                    await session.__aexit__(None, None, None)
        self.request.dispose.assert_awaited_once()
        self.manager.__aexit__.assert_awaited_once()

    async def test_api_failure_conditions_fall_back_once_and_release_response(self):
        for failure in ("http", "json", "empty", "network", "url", "price"):
            with self.subTest(failure=failure):
                self.setUp()
                response = api_response(status=503 if failure == "http" else 200)
                if failure == "json":
                    response.json.side_effect = ValueError("invalid JSON")
                if failure == "price":
                    response.json.return_value = {"results": [
                        {"offer": {"pricePerUnitInUSD": {"amount": "NaN"}},
                         "user": {"username": "invalid"}}]}
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
