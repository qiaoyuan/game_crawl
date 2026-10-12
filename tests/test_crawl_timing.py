"""阶段日志区分页面等待和资源初始化，并保留原始异常与资源复用。"""
import contextlib
import io
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

from g2g import config
from tools import crawl_from_db


class CrawlTimingTests(unittest.IsolatedAsyncioTestCase):
    async def test_g2g_navigation_card_wait_and_extraction_have_separate_timings(self):
        now = 0.0
        page = MagicMock()
        card_results = iter(([], [object()] * 10, [object()] * 10))

        async def navigate(*args, **kwargs):
            nonlocal now
            now += 4

        async def cards(*args):
            nonlocal now
            now += .2
            return next(card_results)

        async def sleep(seconds):
            nonlocal now
            now += seconds

        async def evaluate(script, *args):
            nonlocal now
            if "document.cookie.match" in script:
                now += .1
                return "USD"
            if "const currencyBtn" in script:
                now += .2
                return "USD"
            if "limit =>" in script:
                now += .4
                return [{"seller_id": "seller", "price_raw": "0.123456", "currency_label": "USD",
                         "rating": "96.00%"}]
            self.fail("Unexpected page operation")

        page.goto = AsyncMock(side_effect=navigate)
        page.query_selector_all = AsyncMock(side_effect=cards)
        page.evaluate = AsyncMock(side_effect=evaluate)
        output = io.StringIO()
        with patch.object(crawl_from_db, "perf_counter", side_effect=lambda: now), \
             patch.object(crawl_from_db.asyncio, "sleep", side_effect=sleep) as mocked_sleep, \
             patch.object(config, "CRAWL_OFFER_LIMIT", 10), \
             patch.object(config, "CRAWL_EXCLUDE_STORES", ""), \
             contextlib.redirect_stdout(output):
            rows = await crawl_from_db.scrape_page(page, "https://fixture.test/")

        self.assertEqual(rows[0]["price"], "0.123456")
        self.assertEqual(rows[0]["currency"], "USD")
        self.assertEqual(rows[0]["rating"], "96.00")
        page.goto.assert_awaited_once_with("https://fixture.test/", wait_until="domcontentloaded", timeout=60000)
        self.assertEqual([call.args[0] for call in mocked_sleep.await_args_list], [1, 3])
        for label, seconds in (("G2G 页面导航", "4.00"), ("G2G 货币检查", "0.10"),
                               ("G2G 首批卡片加载", "1.40"),
                               ("G2G 卡片稳定等待（固定 3 秒）", "3.00"),
                               ("G2G 列表数量检查与滚动加载", "0.20"),
                               ("G2G 页面货币解析", "0.20"), ("G2G 卡片数据提取", "0.40"),
                               ("G2G 数据过滤与价格解析", "0.00")):
            self.assertIn(f"阶段开始: {label}", output.getvalue())
            self.assertIn(f"阶段完成: {label}，耗时: {seconds} 秒", output.getvalue())

    async def test_navigation_failure_identifies_stage_and_preserves_original_error(self):
        page = MagicMock()
        error = TimeoutError("navigation timed out")
        page.goto = AsyncMock(side_effect=error)
        page.evaluate = AsyncMock()
        page.query_selector_all = AsyncMock()
        output = io.StringIO()
        with patch.object(crawl_from_db, "perf_counter", side_effect=[10, 14]), \
             contextlib.redirect_stdout(output):
            with self.assertRaises(TimeoutError) as caught:
                await crawl_from_db.scrape_page(page, "https://fixture.test/")
        self.assertIs(caught.exception, error)
        self.assertIn("阶段失败: G2G 页面导航，耗时: 4.00 秒", output.getvalue())
        page.evaluate.assert_not_awaited()
        page.query_selector_all.assert_not_awaited()

    async def test_driver_browser_and_page_startup_are_timed_once_then_reused(self):
        now = 0.0
        context, browser, playwright, manager = (MagicMock() for _ in range(4))

        async def start_driver(*args):
            nonlocal now
            now += 2
            return playwright

        async def launch(**kwargs):
            nonlocal now
            now += 3
            return browser

        async def new_context(**kwargs):
            nonlocal now
            now += 1
            return context

        async def new_page():
            nonlocal now
            now += 1
            return page

        page = MagicMock()
        manager.__aenter__ = AsyncMock(side_effect=start_driver)
        manager.__aexit__ = AsyncMock()
        playwright.chromium.launch = AsyncMock(side_effect=launch)
        browser.new_context = AsyncMock(side_effect=new_context)
        browser.close = AsyncMock()
        context.add_cookies = AsyncMock()
        context.add_init_script = AsyncMock()
        context.new_page = AsyncMock(side_effect=new_page)
        output = io.StringIO()
        with patch.object(crawl_from_db, "async_playwright", return_value=manager), \
             patch.object(crawl_from_db, "perf_counter", side_effect=lambda: now), \
             contextlib.redirect_stdout(output):
            async with crawl_from_db.CrawlSession() as session:
                self.assertIs(await session.get_g2g_page(), page)
                self.assertIs(await session.get_g2g_page(), page)
        for label, seconds in (("Playwright 驱动启动", 2), ("Chromium 启动", 3),
                               ("G2G 上下文与页面初始化", 2)):
            self.assertEqual(output.getvalue().count(f"阶段开始: {label}"), 1)
            self.assertIn(f"阶段完成: {label}，耗时: {seconds:.2f} 秒", output.getvalue())
        manager.__aenter__.assert_awaited_once()
        playwright.chromium.launch.assert_awaited_once()
        context.new_page.assert_awaited_once()
        browser.close.assert_awaited_once()
        manager.__aexit__.assert_awaited_once()
