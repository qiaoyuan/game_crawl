"""本地真实 DOM 验证 ELD 保留页面前 N 条顺序，不自行排序或按库存过滤。"""
import contextlib
import io
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from playwright.async_api import async_playwright

from g2g import config
from tools import crawl_from_db


class EldoradoPageOrderTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.playwright = await async_playwright().start()
        self.browser = await self.playwright.chromium.launch(headless=True)
        self.page = await self.browser.new_page()
        cards = []
        # 页面刻意不是价格升序，后面的低价店也不能挤入页面前八个。
        self.prices = [90, 20, 31, 35, 39, 50, 40, 45, 1, 2]
        for index, price in enumerate(self.prices):
            stock = 1 if index in (3, 4) else 60
            cards.append(f"""<div class="offer-row">
                <div class="profile__username"><a href="/users/seller{index}/shop/Currency">seller{index}</a></div>
                <div class="score"><div>99.5%</div></div>
                <div class="detail"><span class="label">In stock</span><div class="value">{stock}</div></div>
                <div class="detail"><span class="label">Min. qty.</span><div class="value">1</div></div>
                <strong aria-label="amount-price">${price}</strong>
            </div>""")
        await self.page.set_content('<base href="https://www.eldorado.gg/"><div id="other-sellers">'
                                    + ''.join(cards) + '</div>')

    async def asyncTearDown(self):
        await self.browser.close()
        await self.playwright.stop()

    async def scrape(self, excluded="", limit=8):
        url = "https://www.eldorado.gg/currency/g/220?offerSortingCriterion=Cheapest"
        with contextlib.ExitStack() as stack:
            goto = stack.enter_context(patch.object(self.page, "goto", new=AsyncMock()))
            api = stack.enter_context(patch.object(self.page.request, "get", new=AsyncMock(side_effect=AssertionError("不得直连报价 API"))))
            stack.enter_context(patch.object(config, "CRAWL_EXCLUDE_STORES", excluded))
            stack.enter_context(patch.object(config, "ELD_OFFER_PAGE_SIZE", limit))
            stack.enter_context(patch.object(config, "CRAWL_OFFER_LIMIT", 10))
            stack.enter_context(patch.object(crawl_from_db, "asyncio", SimpleNamespace(sleep=AsyncMock())))
            evaluate = stack.enter_context(patch.object(self.page, "evaluate", wraps=self.page.evaluate))
            stack.enter_context(contextlib.redirect_stdout(io.StringIO()))
            rows = await crawl_from_db.scrape_eldorado_page(self.page, url)
        goto.assert_awaited_once_with(url, wait_until="domcontentloaded", timeout=60000)
        api.assert_not_awaited()
        self.assertFalse(any("scrollTo" in call.args[0] for call in evaluate.await_args_list))
        return rows

    async def test_first_eight_preserve_dom_order_and_stock_one(self):
        rows = await self.scrape()
        self.assertEqual([row["seller_name"] for row in rows], [f"seller{i}" for i in range(8)])
        self.assertEqual([row["price"] for row in rows], [str(price) for price in self.prices[:8]])
        self.assertEqual([row["stock"] for row in rows[3:5]], ["1", "1"])
        self.assertTrue(all(row["currency"] == "USD" and row["rating"] == "99.50" for row in rows))

    async def test_excludes_only_within_first_eight_without_refilling(self):
        rows = await self.scrape(excluded="seller0,seller3")
        self.assertEqual([row["seller_name"] for row in rows],
                         [f"seller{i}" for i in (1, 2, 4, 5, 6, 7)])

    async def test_all_first_eight_excluded_does_not_take_ninth(self):
        rows = await self.scrape(excluded=",".join(f"seller{i}" for i in range(8)))
        self.assertEqual(rows, [])

    async def test_eld_limit_is_independent_of_g2g_limit(self):
        rows = await self.scrape(limit=5)
        self.assertEqual([row["seller_name"] for row in rows], [f"seller{i}" for i in range(5)])


if __name__ == "__main__":
    unittest.main()
