"""普通 G2G 游戏币列表可用 API 时与原快照保持一致，异常回退到页面。"""
import unittest
from contextlib import ExitStack, redirect_stdout
from io import StringIO
from unittest.mock import AsyncMock, MagicMock, patch
from urllib.parse import parse_qs, urlsplit

from g2g import config, db
from tools import crawl_from_db as crawl


COIN_URL = ("https://www.g2g.com/cn/categories/aion-2-kinah/offer/group"
            "?fa=server%3Alumiel&region_id=europe&sort=lowest_price")


def offer(shop, price="1.234567"):
    return {
        "username": shop, "seller_id": "100003", "offer_id": "offer-" + shop,
        "seo_term": "aion-2-kinah", "region_id": "europe", "filter_attributes": {"server": ["lumiel"]},
        "display_currency": "USD", "display_price": price,
        "unit_price": 99, "offer_currency": "SGD",
        "user_level": 2, "satisfaction_rate": 0.9661, "total_success_order": 7,
        "available_qty": 1000, "min_qty": 3, "is_online": True,
        "delivery_speed_details": [{"min": 1, "max": 1000, "delivery_time": 60}],
    }


def api_response(rows, status=200):
    result = MagicMock(ok=status == 200, status=status)
    result.json = AsyncMock(return_value={"code": 2000, "payload": {"results": rows}})
    result.dispose = AsyncMock()
    return result


def brand_response(code):
    result = api_response([])
    result.json.return_value = {"code": 2000, "payload": {"relation_detail": {
        "brand_display_config": {"brand_code": code}, "brand_name": "Long game name",
    }}}
    return result


class CoinSearchParamsTests(unittest.TestCase):
    def test_display_price_uses_seller_usd_override_when_available(self):
        row = offer("SellerA", "0.123456")
        row["other_pricing"] = [{"currency": "USD", "unit_price": "0.100007"}]
        with patch.object(config, "CRAWL_EXCLUDE_STORES", ""):
            raw = crawl.parse_coin_offers({"code": 2000, "payload": {"results": [row]}}, 10)
        self.assertEqual(crawl.parse_other_offer_rows(raw)[0]["price"], "0.100007")

    def test_supported_url_preserves_region_and_multiple_filters(self):
        url = ("https://www.g2g.com/categories/path-of-exile-2-currency/offer/group"
               "?fa=league%3Astandard%7Citem%3Adivine&region_id=realm1&sort=lowest_price")
        with patch.object(config, "CRAWL_OFFER_LIMIT", 10):
            params = crawl.g2g_coin_search_params(url)
        self.assertEqual(params["filter_attr"], "league:standard|item:divine")
        self.assertEqual(params["seo_term"], "path-of-exile-2-currency")
        self.assertEqual(params["region_id"], "realm1")
        self.assertEqual(params["sort"], "lowest_price")
        self.assertEqual(params["country"], "US")
        self.assertEqual(params["currency"], "USD")
        self.assertEqual(str(params["group"]), "0")

    def test_unmapped_query_uses_original_page_and_large_save_limit_still_requests_one_page(self):
        for url in (COIN_URL + "&extra=1", COIN_URL + "&fa=other",
                    COIN_URL.replace("www.g2g.com", "other.test"),
                    COIN_URL.replace("/offer/group", "/offer/detail")):
            with self.subTest(url=url), patch.object(config, "CRAWL_OFFER_LIMIT", 10):
                self.assertIsNone(crawl.g2g_coin_search_params(url))
        with patch.object(config, "CRAWL_OFFER_LIMIT", 21):
            self.assertEqual(int(crawl.g2g_coin_search_params(COIN_URL)["page_size"]), 20)

    def test_lowest_price_sort_even_when_target_has_another_sort_or_omits_it(self):
        with patch.object(config, "CRAWL_OFFER_LIMIT", 10):
            for url in (COIN_URL.replace("&sort=lowest_price", ""), COIN_URL.replace("lowest_price", "popular")):
                with self.subTest(url=url):
                    params = crawl.g2g_coin_search_params(url)
                    self.assertEqual(params["sort"], "lowest_price")
                    self.assertEqual(params["region_id"], "europe")
                    self.assertEqual(params["filter_attr"], "server:lumiel")


class CoinApiCrawlTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        stack = ExitStack()
        self.addCleanup(stack.close)
        stack.enter_context(redirect_stdout(StringIO()))
        stack.enter_context(patch.object(config, "CRAWL_OFFER_LIMIT", 2))
        stack.enter_context(patch.object(config, "CRAWL_EXCLUDE_STORES", "own_shop"))
        self.request = AsyncMock()
        self.playwright = MagicMock()
        self.playwright.request.new_context = AsyncMock(return_value=self.request)
        self.playwright.chromium.launch = AsyncMock()
        stack.enter_context(patch.object(crawl.CrawlSession, "get_playwright",
                                        new=AsyncMock(return_value=self.playwright)))
        self.page = stack.enter_context(patch.object(crawl.CrawlSession, "get_g2g_page",
                                                     new=AsyncMock()))
        self.dom = stack.enter_context(patch.object(crawl, "scrape_other_offer_page",
                                                    new=AsyncMock(return_value=[])))
        stack.enter_context(patch.object(db, "increment_version", return_value=8))
        self.save = stack.enter_context(patch.object(db, "save_crawl_data",
                                                    side_effect=lambda tid, platform, items, **kw: len(items)))
        self.update = stack.enter_context(patch.object(db, "update_last_crawl"))
        self.notify = stack.enter_context(patch.object(db, "insert_crawl_notify"))

    async def crawl_one(self, session, target_id=10, **kwargs):
        target = {"id": target_id, "game_product_id": 1, "category": "游戏币", "url": COIN_URL, **kwargs}
        saved, failed, _, _ = await crawl.crawl_target(session, 0, target, 1)
        self.assertFalse(failed)
        return saved

    async def test_api_snapshot_fills_from_first_page_after_exclusion_without_browser(self):
        first = api_response([offer("own_SHOP", "bad"), offer("SellerB"), offer("SellerC")])
        excluded = api_response([offer("own_shop", "bad")])
        second = api_response([dict(offer("SellerC", "0.95"), available_qty=7)])
        metadata = brand_response("AI2")
        updated_metadata = brand_response("NEW")
        self.request.get.side_effect = [first, metadata, second, updated_metadata, excluded]
        async with crawl.CrawlSession() as session:
            self.assertEqual(await self.crawl_one(session), 2)
            self.assertEqual(await self.crawl_one(session, target_id=11, category="金币"), 1)
            self.assertEqual(await self.crawl_one(session, target_id=12), 0)
        row, filled = self.save.call_args_list[0].args[2]
        self.assertEqual(filled["seller_id"], "SellerC")
        self.assertEqual((row["seller_id"], row["seller_name"]), ("SellerB", "SellerB"))
        self.assertEqual((row["price"], row["currency"]), ("1.234567", "USD"))
        self.assertEqual(row["rating"], "96.61")
        self.assertEqual(row["product_title"], "AI2")
        updated = self.save.call_args_list[1].args[2][0]
        self.assertEqual((filled["price"], updated["price"]), ("1.234567", "0.95"))
        self.assertEqual((updated["seller_id"], updated["stock"], updated["product_title"]), ("SellerC", "7", "NEW"))
        self.assertEqual(self.save.call_args_list[2].args[2], [])
        self.assertEqual([c.args for c in self.notify.call_args_list], [(10, 8, 2), (11, 8, 1), (12, 8, 0)])
        self.dom.assert_not_awaited()
        self.page.assert_not_awaited()
        self.playwright.chromium.launch.assert_not_awaited()
        self.playwright.request.new_context.assert_awaited_once()
        self.request.dispose.assert_awaited_once()
        paths = [urlsplit(c.args[0]).path for c in self.request.get.await_args_list]
        self.assertEqual(paths, ["/v3/offer/search", "/v3/offer/keyword_info"] * 2 + ["/v3/offer/search"])
        for call in self.request.get.await_args_list:
            self.assertEqual(call.kwargs["headers"]["Cache-Control"], "no-cache, no-store")
        for response in (first, metadata, second, updated_metadata, excluded):
            response.dispose.assert_awaited_once()

    async def test_only_first_twenty_compete_and_at_most_ten_are_saved(self):
        own = [offer("own_shop", "bad")] * 10
        others = [dict(offer(f"seller{i}"), user_level=1, available_qty=i) for i in range(10)]
        # Positions 11–19 remain. Position 20 is also excluded, so no row can fill from position 21.
        limited = api_response(own + others[:9] + [offer("own_shop"), offer("outside")])
        abundant = api_response(others + [offer("eleventh")])
        self.request.get.side_effect = [limited, brand_response("AI2"), abundant, brand_response("AI2")]
        with patch.object(config, "CRAWL_OFFER_LIMIT", 25):
            async with crawl.CrawlSession() as session:
                self.assertEqual(await self.crawl_one(session), 9)
                self.assertEqual(await self.crawl_one(session, target_id=11), 10)
        for i, expected in enumerate((others[:9], others)):
            saved = self.save.call_args_list[i].args[2]
            self.assertEqual([r["seller_id"] for r in saved], [o["username"] for o in expected])
        self.dom.assert_not_awaited()

    async def test_failed_or_unusable_search_falls_back_once_per_target(self):
        invalid_json = api_response([])
        invalid_json.json.side_effect = ValueError("invalid JSON")
        wrong_currency = offer("foreign")
        wrong_currency["display_currency"] = "EUR"
        responses = [api_response([], status=503), invalid_json, api_response([]),
                     api_response([offer("bad", "NaN")]), api_response([wrong_currency]),
                     api_response([dict(offer("wrong-realm"), region_id="other")]),
                     api_response([dict(offer("wrong-attribute"), filter_attributes={"server": ["other"]})]),
                     api_response([offer("own_shop"), offer("valid"),
                                   dict(offer("wrong-backfill"), region_id="other")])]
        self.request.get.side_effect = responses
        self.dom.return_value = [{"seller_id": "from_page", "price": "0.50", "currency": "USD"}]
        async with crawl.CrawlSession() as session:
            for i, response in enumerate(responses):
                self.assertEqual(await self.crawl_one(session, target_id=i + 1), 1)
                self.assertEqual(self.request.get.await_count, i + 1)
                self.assertEqual(self.dom.await_count, i + 1)
                self.assertEqual(self.save.call_args.args[2], self.dom.return_value)
                response.dispose.assert_awaited_once()

    async def test_every_requested_attribute_must_match_for_multi_filter_offer(self):
        url = ("https://www.g2g.com/categories/path-of-exile-2-currency/offer/group"
               "?fa=league%3Astandard%7Citem%3Adivine&sort=lowest_price")
        valid = dict(offer("Right"), seo_term="path-of-exile-2-currency",
                     filter_attributes={"league": ["standard"], "item": ["divine"]})
        wrong = dict(valid, filter_attributes={"league": ["standard"], "item": ["other"]})
        self.request.get.side_effect = [api_response([valid]), brand_response("POE2"), api_response([wrong])]
        async with crawl.CrawlSession() as session:
            self.assertEqual(await self.crawl_one(session, url=url), 1)
            self.assertEqual(self.save.call_args.args[2][0]["seller_id"], "Right")
            self.dom.assert_not_awaited()
            self.assertEqual(await self.crawl_one(session, url=url), 0)
            self.dom.assert_awaited_once()

    async def test_both_paths_failing_does_not_publish_an_empty_snapshot(self):
        self.request.get.side_effect = TimeoutError("API timeout")
        self.dom.side_effect = TimeoutError("page timeout")
        target = {"id": 7, "game_product_id": 1, "category": "游戏币", "url": COIN_URL}
        async with crawl.CrawlSession() as session:
            saved, failed, _, _ = await crawl.crawl_target(session, 0, target, 1)
        self.assertEqual(saved, 0)
        self.assertTrue(failed)
        self.request.get.assert_awaited_once()
        self.dom.assert_awaited_once()
        self.save.assert_not_called()
        self.notify.assert_not_called()
        self.update.assert_not_called()

    async def test_enhanced_target_requires_page_detail_and_unsupported_url_skips_api(self):
        async with crawl.CrawlSession() as session:
            await self.crawl_one(session, url=COIN_URL.replace("&sort=lowest_price", ""),
                                 crawl_type="top3", enhance_stores="bound_shop")
            self.assertEqual(self.dom.await_count, 1)
            enhanced_url = self.dom.await_args.args[1]
            self.assertEqual(parse_qs(urlsplit(enhanced_url).query), parse_qs(urlsplit(COIN_URL).query))
            self.assertTrue(self.dom.await_args.kwargs["refresh_unit_prices"])
            self.assertEqual(self.dom.await_args.kwargs["enhance_stores"], "bound_shop")
            unknown_url = COIN_URL.replace("lowest_price", "popular") + "&server_filter=extra"
            await self.crawl_one(session, url=unknown_url)
            expected = dict(parse_qs(urlsplit(COIN_URL).query), server_filter=["extra"])
            self.assertEqual(parse_qs(urlsplit(self.dom.await_args.args[1]).query), expected)
        self.request.get.assert_not_awaited()
        self.playwright.request.new_context.assert_not_awaited()
