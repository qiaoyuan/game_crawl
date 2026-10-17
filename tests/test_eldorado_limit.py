import unittest
from urllib.parse import parse_qs, urlparse
from unittest.mock import AsyncMock, MagicMock, patch

from tools.crawl_from_db import scrape_eldorado_page, sort_eldorado_prices
from g2g import config


class EldoradoFirstPageTests(unittest.IsolatedAsyncioTestCase):
    async def test_api_only_requests_first_eight_and_preserves_product_filters(self):
        page = MagicMock()
        records = [{"offer": {"id": str(i), "pricePerUnitInUSD": {"amount": i + 1}},
                    "user": {"username": f"seller{i}"}} for i in range(20)]
        response = MagicMock(ok=True, status=200)
        response.dispose = AsyncMock()
        response.json = AsyncMock(return_value={"results": records})
        page.request.get = AsyncMock(return_value=response)
        url = ("https://www.eldorado.gg/poe-2-currency/g/220?"
               "path-of-exile-2-orbs=mirror-of-kalandra&te_v0=Forbidden%20Rites%20Standard"
               "&offerSortingCriterion=MostPopular&pageIndex=2&pageSize=150")
        with patch.object(config, "ELD_OFFER_PAGE_SIZE", 8):
            rows = await scrape_eldorado_page(page, url)
        page.request.get.assert_awaited_once()
        params = parse_qs(urlparse(page.request.get.await_args.args[0]).query)
        self.assertEqual(params["pageIndex"], ["1"])
        self.assertEqual(params["pageSize"], ["8"])
        self.assertEqual(params["path-of-exile-2-orbs"], ["mirror-of-kalandra"])
        self.assertEqual(params["tradeEnvironmentValue0"], ["Forbidden Rites Standard"])
        self.assertEqual(params["offerSortingCriterion"], ["Cheapest"])
        self.assertEqual([row["seller_name"] for row in rows], [f"seller{i}" for i in range(8)])
        page.goto.assert_not_called()
        response.dispose.assert_awaited_once()

    async def test_eld_limit_is_independent_of_g2g_limit(self):
        page = MagicMock()
        response = MagicMock(ok=True, status=200)
        response.dispose = AsyncMock()
        response.json = AsyncMock(return_value={"results": [
            {"offer": {"id": str(i), "pricePerUnitInUSD": {"amount": i + 1}},
             "user": {"username": f"seller{i}"}} for i in range(20)]})
        page.request.get = AsyncMock(return_value=response)
        with patch.object(config, "CRAWL_OFFER_LIMIT", 10), patch.object(config, "ELD_OFFER_PAGE_SIZE", 5):
            rows = await scrape_eldorado_page(page, "https://www.eldorado.gg/currency/g/220")
        self.assertEqual(len(rows), 5)
        params = parse_qs(urlparse(page.request.get.await_args.args[0]).query)
        self.assertEqual(params["pageSize"], ["5"])
        self.assertEqual(params["pageIndex"], ["1"])

    async def test_excludes_api_shops_before_parsing_prices_without_refilling(self):
        page = MagicMock()
        records = [{"offer": {"id": str(i), "pricePerUnitInUSD": {"amount": i + 1}},
                    "user": {"username": f"seller{i}"}} for i in range(20)]
        records[0]["user"]["username"] = " vivi_store "
        records[0]["offer"]["pricePerUnitInUSD"] = ["invalid"]
        records[2]["user"]["id"] = "OtherOwn"
        records[2]["offer"]["pricePerUnitInUSD"] = ["invalid"]
        response = MagicMock(ok=True, status=200)
        response.dispose = AsyncMock()
        response.json = AsyncMock(return_value={"results": records})
        page.request.get = AsyncMock(return_value=response)
        with patch.object(config, "CRAWL_EXCLUDE_STORES", "Vivi_Store,otherown"), \
             patch.object(config, "ELD_OFFER_PAGE_SIZE", 8):
            rows = await scrape_eldorado_page(page, "https://www.eldorado.gg/currency/g/220")
        self.assertEqual([row["seller_name"] for row in rows],
                         [f"seller{i}" for i in (1, 3, 4, 5, 6, 7)])
        page.goto.assert_not_called()

    async def test_api_all_excluded_returns_zero_without_dom_fallback(self):
        page = MagicMock()
        response = MagicMock(ok=True, status=200)
        response.dispose = AsyncMock()
        response.json = AsyncMock(return_value={"results": [
            {"user": {"username": "Vivi_Store"}, "offer": {"pricePerUnitInUSD": ["invalid"]}},
        ]})
        page.request.get = AsyncMock(return_value=response)
        with patch.object(config, "CRAWL_EXCLUDE_STORES", "Vivi_Store"):
            rows = await scrape_eldorado_page(page, "https://www.eldorado.gg/currency/g/220")
        self.assertEqual(rows, [])
        page.goto.assert_not_called()

    async def test_numeric_order_precision_ties_and_low_stock_are_preserved(self):
        page = MagicMock()
        prices = ["10", "2", "0.000000002", "0.000000001", "2", "100", "3", "1"]
        records = [{"offer": {"id": str(i), "quantity": 1,
                              "pricePerUnitInUSD": {"amount": price}},
                    "user": {"username": f"seller{i}"}} for i, price in enumerate(prices)]
        response = MagicMock(ok=True, status=200)
        response.dispose = AsyncMock()
        response.json = AsyncMock(return_value={"results": records})
        page.request.get = AsyncMock(return_value=response)
        with patch.object(config, "CRAWL_EXCLUDE_STORES", ""), \
             patch.object(config, "ELD_OFFER_PAGE_SIZE", 8):
            rows = await scrape_eldorado_page(page, "https://www.eldorado.gg/currency/g/220")
        self.assertEqual([row["seller_name"] for row in rows],
                         [f"seller{i}" for i in (3, 2, 7, 1, 4, 6, 0, 5)])
        self.assertEqual(rows[0]["price"], "0.000000001")
        self.assertTrue(all(row["stock"] == "1" for row in rows))
        page.goto.assert_not_called()
        response.dispose.assert_awaited_once()

    async def test_convert_to_usd_before_sorting_without_rounding_tiny_prices(self):
        page = MagicMock()
        records = [
            {"offer": {"pricePerUnit": {"amount": "0.000000003", "currency": "EUR"},
                       "exchangeRate": {"exchangeRate": "2"}}, "user": {"username": "euro"}},
            {"offer": {"pricePerUnitInUSD": {"amount": "0.0000000016"}},
             "user": {"username": "usd"}},
            {"offer": {"pricePerUnit": {"amount": "0.0000000014", "currency": "USD"}},
             "user": {"username": "usd_fallback"}},
        ]
        response = MagicMock(ok=True, status=200)
        response.dispose = AsyncMock()
        response.json = AsyncMock(return_value={"results": records})
        page.request.get = AsyncMock(return_value=response)
        with patch.object(config, "CRAWL_EXCLUDE_STORES", ""):
            rows = await scrape_eldorado_page(page, "https://www.eldorado.gg/currency/g/220")
        self.assertEqual([row["seller_name"] for row in rows], ["usd_fallback", "euro", "usd"])
        self.assertEqual(rows[1]["price"], "1.5E-9")
        page.goto.assert_not_called()


class EldoradoPriceValidationTests(unittest.TestCase):
    def test_invalid_nonpositive_nonfinite_or_non_usd_cannot_rank_as_lowest(self):
        for price in (None, "", "bad", "0", "-1", "NaN", "Infinity", "-Infinity"):
            with self.subTest(price=price), self.assertRaises(ValueError):
                sort_eldorado_prices([{"price": price, "currency": "USD"}])
        with self.assertRaises(ValueError):
            sort_eldorado_prices([{"price": "1", "currency": "EUR"}])


if __name__ == "__main__":
    unittest.main()
