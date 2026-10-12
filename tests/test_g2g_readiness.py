"""用本地 Chromium DOM 验证 G2G 候选范围和延迟字段，不访问真实网站。"""
import contextlib
import io
import unittest
from unittest.mock import AsyncMock, patch

from playwright.async_api import async_playwright

from g2g import config
from tools import crawl_from_db


def card(index, price=None):
    price = str(index + 1) if price is None else price
    return f'''<div aria-label="Product Card" id="card{index}">
        <a href="https://www.g2g.com/seller{index}">seller{index}</a>
        <div class="flex flex-wrap"><span class="text-base font-bold">{price}</span>
        <span class="text-xs font-medium">USD</span></div>
        <span class="line-clamp-2">Product {index}</span>
    </div>'''


class G2GReadinessTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.playwright = await async_playwright().start()
        self.browser = await self.playwright.chromium.launch(headless=True)
        self.page = await self.browser.new_page()
        await self.page.route("http://fixture.test/**", lambda route: route.fulfill(
            body="<html><body></body></html>", content_type="text/html",
        ))
        await self.page.goto("http://fixture.test/")

    async def asyncTearDown(self):
        await self.browser.close()
        await self.playwright.stop()

    async def scrape(self, limit, excluded=""):
        with patch.object(self.page, "goto", new=AsyncMock()), \
             patch.object(config, "CRAWL_OFFER_LIMIT", limit), \
             patch.object(config, "CRAWL_EXCLUDE_STORES", excluded):
            return await crawl_from_db.scrape_page(self.page, "http://fixture.test/")

    async def test_ready_first_n_does_not_wait_scroll_or_backfill_excluded_rows(self):
        await self.page.set_content("".join(card(i) for i in range(10)) +
                                    '<div aria-label="Product Card"></div>')
        with patch.object(self.page, "query_selector_all", wraps=self.page.query_selector_all) as handles, \
             patch.object(self.page, "wait_for_function", wraps=self.page.wait_for_function) as wait, \
             patch.object(self.page, "evaluate", wraps=self.page.evaluate) as evaluate:
            rows = await self.scrape(10, "seller0,seller3")
        self.assertEqual([row["seller_id"] for row in rows],
                         [f"seller{i}" for i in range(10) if i not in (0, 3)])
        self.assertEqual([row["price"] for row in rows],
                         [str(i + 1) for i in range(10) if i not in (0, 3)])
        self.assertTrue(all(row["currency"] == "USD" for row in rows))
        handles.assert_not_awaited()
        wait.assert_not_awaited()
        self.assertFalse(any("scrollTo" in call.args[0] for call in evaluate.await_args_list))

    async def test_waits_for_delayed_price_and_seller_in_current_candidates(self):
        await self.page.set_content(card(0, "") + card(1, "0.987654") + """
            <script>
                document.querySelector('#card1 a').removeAttribute('href');
                setTimeout(() => {
                    document.querySelector('#card0 .text-base').textContent = '0.123456';
                }, 100);
                setTimeout(() => {
                    document.querySelector('#card1 a').href = 'https://www.g2g.com/seller1';
                }, 300);
            </script>
        """)
        with patch.object(self.page, "wait_for_function", wraps=self.page.wait_for_function) as wait:
            rows = await self.scrape(2)
        self.assertEqual([row["seller_id"] for row in rows], ["seller0", "seller1"])
        self.assertEqual([row["price"] for row in rows], ["0.123456", "0.987654"])
        wait.assert_awaited_once()

    async def test_checks_fields_of_cards_added_during_loading(self):
        await self.page.set_content(card(0) + """
            <script>
                window.scrolls = 0;
                window.scrollTo = () => {
                    window.scrolls++;
                    const item = document.createElement('div');
                    item.setAttribute('aria-label', 'Product Card');
                    item.innerHTML = '<a href="https://www.g2g.com/seller1">seller1</a>' +
                        '<span class="text-base font-bold"></span>';
                    document.body.append(item);
                    setTimeout(() => item.querySelector('span').textContent = '0.765432', 100);
                };
            </script>
        """)
        # Skip the old two-second scroll pause so the readiness phase must catch hydration.
        with patch.object(crawl_from_db.asyncio, "sleep", new=AsyncMock()), \
             patch.object(self.page, "wait_for_function", wraps=self.page.wait_for_function) as wait:
            rows = await self.scrape(2)
        self.assertEqual(await self.page.evaluate("window.scrolls"), 1)
        self.assertEqual(rows[1]["price"], "0.765432")
        wait.assert_awaited_once()

    async def test_fewer_available_cards_stop_when_count_stabilizes(self):
        await self.page.set_content(card(0) + card(1) +
                                    '<script>window.scrolls = 0; window.scrollTo = () => window.scrolls++;</script>')
        with patch.object(crawl_from_db.asyncio, "sleep", new=AsyncMock()):
            rows = await self.scrape(10)
        self.assertEqual(len(rows), 2)
        self.assertEqual(await self.page.evaluate("window.scrolls"), 1)

    async def test_missing_fields_timeout_keeps_existing_extraction_semantics(self):
        await self.page.set_content(card(0, ""))
        original_wait = crawl_from_db.wait_for_g2g_card_fields

        async def short_wait(page, limit):
            return await original_wait(page, limit, timeout_ms=100)

        output = io.StringIO()
        with patch.object(crawl_from_db, "wait_for_g2g_card_fields", side_effect=short_wait), \
             contextlib.redirect_stdout(output):
            rows = await self.scrape(1)
        self.assertEqual(rows[0]["seller_id"], "seller0")
        self.assertEqual(rows[0]["price"], "")
        self.assertIn("就绪 0/1，继续按现有规则提取", output.getvalue())

    async def test_page_failure_is_not_hidden_as_readiness_timeout(self):
        error = RuntimeError("page crashed")
        with patch.object(self.page, "evaluate", new=AsyncMock(side_effect=error)):
            with self.assertRaises(RuntimeError) as caught:
                await crawl_from_db.wait_for_g2g_card_fields(self.page, 10)
        self.assertIs(caught.exception, error)
