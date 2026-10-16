"""验证 ELD 直接读取页面、混合目标的 context 隔离及浏览器资源释放。"""
import asyncio
import contextlib
import io
import unittest
from datetime import datetime
from unittest.mock import AsyncMock, MagicMock, patch

from g2g import config, db
from tools import crawl_from_db


def records():
    return [{"seller_name": f"seller{i}", "seller_id": f"seller{i}", "stock": "1",
             "price": str(20 - i), "currency": "USD"} for i in range(8)]


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
        self.dom = self.enterContext(patch.object(crawl_from_db, "scrape_eldorado_dom",
                                                  new=AsyncMock(return_value=records())))

    async def test_eld_round_saves_page_order_and_closes_each_context(self):
        targets = [{"id": i, "game_product_id": 1, "category": "ELD游戏币",
                    "url": f"https://www.eldorado.gg/currency/g/{i}"} for i in (24, 26, 40)]
        contexts = [browser_context() for _ in targets]
        self.browser.new_context.side_effect = contexts
        self.dom.return_value = records()[1:]
        with contextlib.ExitStack() as stack:
            stack.enter_context(contextlib.redirect_stdout(io.StringIO()))
            stack.enter_context(patch.object(crawl_from_db, "async_playwright", return_value=self.manager))
            stack.enter_context(patch.object(db, "get_pending_targets", return_value=targets))
            stack.enter_context(patch.object(db, "increment_version", return_value=9))
            save = stack.enter_context(patch.object(db, "save_crawl_data", return_value=7))
            update = stack.enter_context(patch.object(db, "update_last_crawl"))
            notify = stack.enter_context(patch.object(db, "insert_crawl_notify"))
            self.assertEqual(await crawl_from_db.run(0, 2), 0)
        self.playwright.chromium.launch.assert_awaited_once()
        self.playwright.request.new_context.assert_not_awaited()
        self.assertEqual(save.call_count, 3)
        for target, call, context in zip(targets, save.call_args_list, contexts):
            self.assertEqual(call.args[:2], (target["id"], "eldorado"))
            self.assertEqual(call.args[2], records()[1:])
            self.assertEqual(call.kwargs, {"game_product_id": 1, "version": 9})
            context.close.assert_awaited_once()
        self.assertEqual(update.call_count, 3)
        self.assertEqual([call.args for call in notify.call_args_list], [(i, 9, 7) for i in (24, 26, 40)])
        self.browser.close.assert_awaited_once()
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

    async def test_both_eld_crawl_types_save_page_order_without_strategy_or_sort(self):
        targets = [{"id": i + 1, "game_product_id": 1, "category": "ELD游戏币", "crawl_type": i,
                    "url": "https://www.eldorado.gg/currency/g/220?offerSortingCriterion=Cheapest"}
                   for i in (0, 1)]
        with contextlib.ExitStack() as stack:
            stack.enter_context(contextlib.redirect_stdout(io.StringIO()))
            stack.enter_context(patch.object(crawl_from_db, "async_playwright", return_value=self.manager))
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
            self.assertEqual(call.args[2], records())  # 刻意使用非价格升序，库存全部为 1。
        self.assertEqual([call.args for call in notify.call_args_list], [(1, 9, 8), (2, 9, 8)])
        self.playwright.request.new_context.assert_not_awaited()

    async def test_empty_page_result_is_saved_and_notified_once(self):
        target = {"id": 24, "game_product_id": 1, "category": "ELD游戏币",
                  "url": "https://www.eldorado.gg/currency/g/24"}
        self.dom.return_value = []
        with contextlib.ExitStack() as stack:
            stack.enter_context(contextlib.redirect_stdout(io.StringIO()))
            stack.enter_context(patch.object(crawl_from_db, "async_playwright", return_value=self.manager))
            stack.enter_context(patch.object(db, "get_pending_targets", return_value=[target]))
            stack.enter_context(patch.object(db, "increment_version", return_value=9))
            save = stack.enter_context(patch.object(db, "save_crawl_data", return_value=0))
            update = stack.enter_context(patch.object(db, "update_last_crawl"))
            notify = stack.enter_context(patch.object(db, "insert_crawl_notify"))
            self.assertEqual(await crawl_from_db.run(), 0)
        self.assertEqual(save.call_args.args[2], [])
        update.assert_called_once()
        notify.assert_called_once_with(24, 9, 0)
        self.dom.assert_awaited_once()
        self.context.close.assert_awaited_once()
        self.playwright.request.new_context.assert_not_awaited()

    async def test_mixed_targets_reuse_browser_and_keep_eld_context_isolated(self):
        eld_contexts = [browser_context(), browser_context()]
        self.browser.new_context.side_effect = [self.context, *eld_contexts]
        self.dom.side_effect = [[], records()]
        with patch.object(crawl_from_db, "async_playwright", return_value=self.manager), contextlib.redirect_stdout(io.StringIO()):
            async with crawl_from_db.CrawlSession() as session:
                first_page = await session.get_g2g_page()
                self.assertEqual(await session.scrape_eldorado("https://www.eldorado.gg/currency/g/24"), [])
                self.assertEqual(await session.scrape_eldorado("https://www.eldorado.gg/currency/g/26"), records())
                self.assertIs(await session.get_g2g_page(), first_page)
        self.playwright.chromium.launch.assert_awaited_once()
        self.assertEqual(self.browser.new_context.await_count, 3)
        self.context.new_page.assert_awaited_once()
        self.assertEqual(self.context.add_cookies.await_args.args[0][0]["domain"], ".g2g.com")
        for context in eld_contexts:
            context.new_page.assert_awaited_once()
            self.assertEqual({cookie["name"] for cookie in context.add_cookies.await_args.args[0]},
                             {"eldoradogg_currencyPreference", "eldoradogg_locale"})
            context.close.assert_awaited_once()
        self.context.close.assert_awaited_once()
        self.browser.close.assert_awaited_once()
        self.playwright.request.new_context.assert_not_awaited()

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

    async def test_failed_context_close_retires_browser_before_rebuilding(self):
        replacement_context = browser_context()
        replacement_browser = chromium(replacement_context)
        self.playwright.chromium.launch.side_effect = [self.browser, replacement_browser]
        self.context.new_page.return_value.goto.side_effect = TimeoutError("park timed out")
        self.context.close.side_effect = RuntimeError("context close failed")
        with patch.object(crawl_from_db, "async_playwright", return_value=self.manager), \
             contextlib.redirect_stdout(io.StringIO()):
            async with crawl_from_db.CrawlSession() as session:
                await session.get_g2g_page()
                await session.park_g2g_page()
                self.browser.close.assert_awaited_once()
                self.assertIsNone(session.browser)
                self.assertIs(await session.get_g2g_page(), replacement_context.new_page.return_value)
        self.assertEqual(self.playwright.chromium.launch.await_count, 2)
        self.context.close.assert_awaited_once()
        replacement_context.close.assert_awaited_once()
        replacement_browser.close.assert_awaited_once()

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

    async def test_cancelled_browser_cleanup_still_releases_driver(self):
        with patch.object(crawl_from_db, "async_playwright", return_value=self.manager), \
             contextlib.redirect_stdout(io.StringIO()):
            session = crawl_from_db.CrawlSession()
            await session.__aenter__()
            await session.scrape_eldorado("https://www.eldorado.gg/currency/g/24")
            with patch.object(session, "_close_browser", new=AsyncMock(side_effect=asyncio.CancelledError)):
                with self.assertRaises(asyncio.CancelledError):
                    await session.__aexit__(None, None, None)
        self.manager.__aexit__.assert_awaited_once()

    async def test_partial_eld_initialization_failure_releases_context(self):
        for phase in ("add_cookies", "add_init_script", "new_page"):
            with self.subTest(phase=phase):
                self.setUp()
                error = RuntimeError(f"{phase} failed")
                getattr(self.context, phase).side_effect = error
                with patch.object(crawl_from_db, "async_playwright", return_value=self.manager), contextlib.redirect_stdout(io.StringIO()):
                    async with crawl_from_db.CrawlSession() as session:
                        with self.assertRaises(RuntimeError) as caught:
                            await session.scrape_eldorado("https://www.eldorado.gg/currency/g/24")
                        self.assertIs(caught.exception, error)
                self.context.close.assert_awaited_once()
                self.browser.close.assert_awaited_once()
                self.playwright.request.new_context.assert_not_awaited()

    async def test_failed_page_target_sends_no_notification_and_next_target_runs(self):
        targets = [{"id": i, "game_product_id": 1, "category": "ELD游戏币",
                    "url": f"https://www.eldorado.gg/currency/g/{i}"} for i in (24, 26)]
        contexts = [browser_context(), browser_context()]
        self.browser.new_context.side_effect = contexts
        self.dom.side_effect = [RuntimeError("DOM failed"), records()]
        with contextlib.ExitStack() as stack:
            stack.enter_context(contextlib.redirect_stdout(io.StringIO()))
            stack.enter_context(patch.object(crawl_from_db, "async_playwright", return_value=self.manager))
            stack.enter_context(patch.object(db, "get_pending_targets", return_value=targets))
            stack.enter_context(patch.object(db, "increment_version", return_value=9))
            save = stack.enter_context(patch.object(db, "save_crawl_data", return_value=8))
            update = stack.enter_context(patch.object(db, "update_last_crawl"))
            notify = stack.enter_context(patch.object(db, "insert_crawl_notify"))
            self.assertEqual(await crawl_from_db.run(), 1)
        save.assert_called_once()
        self.assertEqual(save.call_args.args[0], 26)
        update.assert_called_once()
        self.assertEqual(update.call_args.args[0], 26)
        notify.assert_called_once_with(26, 9, 8)
        for context in contexts:
            context.close.assert_awaited_once()
        self.browser.close.assert_awaited_once()

if __name__ == "__main__":
    unittest.main()
