import unittest
from urllib.parse import parse_qs, urlparse
from unittest.mock import AsyncMock, MagicMock, patch

from tools.crawl_from_db import scrape_eldorado_page
from g2g import config


class EldoradoFirstPageTests(unittest.IsolatedAsyncioTestCase):
    async def test_api_only_requests_first_eight_and_preserves_product_filters(self):
        page = MagicMock()
        records = [{"offer": {"id": str(i), "pricePerUnitInUSD": {"amount": i + 1}},
                    "user": {"username": f"seller{i}"}} for i in range(20)]
        response = MagicMock(ok=True, status=200)
        response.json = AsyncMock(return_value={"results": records})
        page.request.get = AsyncMock(return_value=response)
        url = ("https://www.eldorado.gg/poe-2-currency/g/220?"
               "path-of-exile-2-orbs=mirror-of-kalandra&te_v0=Forbidden%20Rites%20Standard"
               "&offerSortingCriterion=Cheapest&pageIndex=2&pageSize=150")
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

    async def test_eld_limit_is_independent_of_g2g_limit(self):
        page = MagicMock()
        response = MagicMock(ok=True, status=200)
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
        response.json = AsyncMock(return_value={"results": [
            {"user": {"username": "Vivi_Store"}, "offer": {"pricePerUnitInUSD": ["invalid"]}},
        ]})
        page.request.get = AsyncMock(return_value=response)
        with patch.object(config, "CRAWL_EXCLUDE_STORES", "Vivi_Store"):
            rows = await scrape_eldorado_page(page, "https://www.eldorado.gg/currency/g/220")
        self.assertEqual(rows, [])
        page.goto.assert_not_called()


if __name__ == "__main__":
    unittest.main()
