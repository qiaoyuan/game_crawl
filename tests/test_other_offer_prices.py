"""本地 DOM 夹具验证逐店查看的异步价格切换，不请求真实平台。"""
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from playwright.async_api import async_playwright

from g2g.crawl_filter import compile_policies, select_top3
from tools.crawl_from_db import parse_detail_unit_price, refresh_other_offer_prices
from tools import crawl_from_db


class DetailPriceParsingTests(unittest.TestCase):
    def test_unit_price_precision_and_currency(self):
        self.assertEqual(parse_detail_unit_price("单价\n 0.433299\n USD"), ("0.433299", "USD"))
        self.assertEqual(parse_detail_unit_price("Unit Price 0.5654513 SGD"), ("0.5654513", "SGD"))
        self.assertEqual(parse_detail_unit_price("Unit price 1,234.123456 USD"), ("1234.123456", "USD"))
        for value in ("Total Amount 1.14 USD", "最低 0.433299 USD", "单价 0 USD",
                      "单价 -1 USD", "Unit Price NaN USD", "单价 0.43"):
            with self.subTest(value=value), self.assertRaises(ValueError):
                parse_detail_unit_price(value)


class OtherOfferPriceTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.playwright = await async_playwright().start()
        self.browser = await self.playwright.chromium.launch(headless=True)
        self.page = await self.browser.new_page()
        # 切换店铺身份先于单价刷新，用于捕获读取上一家价格的错误。
        await self.page.set_content("""
            <base href="https://www.g2g.com/">
            <div id="pcMain"><a class="g-card-no-deco cursor-pointer" href="/initial">initial</a></div>
            <div class="vue-portal-target"><div class="pricing-container">
                <div class="text-center text-body-2 g-mt-12">单价 99 USD</div>
                <span id="final-price">999 USD</span>
            </div></div>
            <div id="pcOtherOffer"></div>
            <script>
            const rows = [['a','0.9'], ['b','0.4'], ['c','0.433299'], ['d','0.2']];
            window.clicks = [];
            for (const [id, price] of rows) {
                const card = document.createElement('div');
                card.className = 'other-seller--gradient';
                card.innerHTML = `<a href="/${id}">${id}</a><button>查看</button>`;
                card.querySelector('button').onclick = () => {
                    window.clicks.push(id);
                    document.querySelector('#pcMain a').setAttribute('href', '/' + id);
                    setTimeout(() => {
                        const old = document.querySelector('.g-mt-12');
                        const fresh = old.cloneNode();
                        fresh.textContent = '单价 ' + price + ' USD';
                        old.replaceWith(fresh);
                    }, 80);
                };
                document.querySelector('#pcOtherOffer').append(card);
            }
            </script>
        """)

    async def asyncTearDown(self):
        await self.browser.close()
        await self.playwright.stop()

    async def test_refreshes_every_seller_before_price_filter_and_top3(self):
        rows = [{"seller_id": seller, "price": str(index + 1), "currency": "USD"}
                for index, seller in enumerate("abcd")]
        await refresh_other_offer_prices(self.page, rows, timeout_ms=2000)
        self.assertEqual(await self.page.evaluate("window.clicks"), list("abcd"))
        self.assertEqual([row["price"] for row in rows], ["0.9", "0.4", "0.433299", "0.2"])
        self.assertTrue(all(row["unit_price"] == row["price"] for row in rows))
        compiled = compile_policies([{"config": {"minimum_price": 0.3}, "currency": "USD"}])
        self.assertEqual([row["seller_id"] for row in select_top3(rows, compiled)], list("abc"))
        compiled = compile_policies([{"config": {}, "currency": "USD"}])
        self.assertEqual([row["seller_id"] for row in select_top3(rows, compiled)], list("bcd"))

    async def test_page_only_extracts_first_ten_before_top3_without_extra_loading(self):
        await self.page.evaluate("""() => {
            const container = document.querySelector('#pcOtherOffer');
            container.innerHTML = '';
            for (let i = 0; i < 20; i++) {
                const card = document.createElement('div');
                card.className = 'other-seller--gradient';
                card.innerHTML = `<a href="https://www.g2g.com/seller${i}">seller${i}</a>
                    <span class="text-primary text-body text-weight-bold">${20-i}</span>`;
                container.append(card);
            }
        }""")
        for refresh in (False, True):
            with self.subTest(refresh=refresh), \
                 patch.object(self.page, "goto", new=AsyncMock()), \
                 patch.object(crawl_from_db.config, "CRAWL_OFFER_LIMIT", 10), \
                 patch.object(crawl_from_db, "asyncio", SimpleNamespace(sleep=AsyncMock())) as async_stub, \
                 patch.object(crawl_from_db, "refresh_other_offer_prices", new=AsyncMock()) as detail:
                rows = await crawl_from_db.scrape_other_offer_page(
                    self.page, "https://www.g2g.com/test", refresh_unit_prices=refresh,
                )
                if refresh:
                    rows = detail.await_args.args[1]
                else:
                    detail.assert_not_awaited()
                self.assertEqual([row["seller_id"] for row in rows],
                                 [f"seller{i}" for i in range(10)])
                async_stub.sleep.assert_not_awaited()

    async def test_stale_portal_for_new_seller_times_out_instead_of_using_old_price(self):
        await self.page.evaluate("""() => {
            document.querySelector('#pcOtherOffer button').onclick = () => {
                document.querySelector('#pcMain a').setAttribute('href', '/a');
            };
        }""")
        row = {"seller_id": "a", "price": "0.01", "currency": "USD"}
        with self.assertRaisesRegex(ValueError, "店铺 a 详情单价刷新失败"):
            await refresh_other_offer_prices(self.page, [row], timeout_ms=300)
        self.assertEqual(row["price"], "0.01")

    async def test_prefilter_skips_ineligible_stores_keeps_union_and_ignores_list_price(self):
        rows = [
            {"seller_id": "a", "stock": "1K", "rating": "99", "price": "0.0001", "currency": "EUR"},
            {"seller_id": "b", "stock": "1", "rating": "0", "price": "0.0001"},
            {"seller_id": "c", "stock": "1K", "rating": "89", "price": "0.433299"},
            {"seller_id": "d", "stock": "1K", "rating": "99", "price": "0.2"},
        ]
        compiled = compile_policies([
            {"config": {"blacklist_stores": ["b", "d"], "min_stock": 50, "min_rating": 90,
                        "minimum_price": 0.3}, "currency": "USD"},
            {"config": {"blacklist_stores": ["d"], "whitelist_stores": ["b"],
                        "min_stock": 50, "min_rating": 90}, "currency": "USD"},
        ])
        result = await refresh_other_offer_prices(self.page, rows, timeout_ms=2000, policies=compiled)
        self.assertEqual(await self.page.evaluate("window.clicks"), ["a", "b"])
        self.assertEqual([row["seller_id"] for row in result], ["a", "b"])
        self.assertEqual([row["price"] for row in result], ["0.9", "0.4"])
        self.assertEqual(result[0]["currency"], "USD")
        self.assertEqual(select_top3(result, compiled), result)

    async def test_no_possible_store_does_not_open_any_detail(self):
        rows = [{"seller_id": "a", "stock": "0", "rating": "99"}]
        compiled = compile_policies([{"config": {"min_stock": 50}, "currency": "USD"}])
        self.assertEqual(await refresh_other_offer_prices(self.page, rows, policies=compiled), [])
        self.assertEqual(await self.page.evaluate("window.clicks"), [])

    async def test_already_selected_seller_can_keep_loaded_detail_when_view_is_noop(self):
        await self.page.evaluate("""() => {
            document.querySelector('#pcMain a').setAttribute('href', '/a');
            document.querySelector('.g-mt-12').textContent = 'Unit Price 0.433299 USD';
            document.querySelector('#pcOtherOffer button').onclick = () => {};
        }""")
        row = {"seller_id": "a", "price": "0.01", "currency": "USD"}
        await refresh_other_offer_prices(self.page, [row], timeout_ms=1000)
        self.assertEqual(row["price"], "0.433299")


if __name__ == "__main__":
    unittest.main()
