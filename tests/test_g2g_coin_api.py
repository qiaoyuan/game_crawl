"""G2G 游戏币列表和加强详情走 API 时保留完整快照，异常整单回退到页面。"""
import json
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


def identified_offers(entries):
    """生成不同商户的报价；买家可见店名与 G2G 内部卖家、报价 ID 有区别。"""
    return [dict(offer(name, price), offer_id=f"G1793605252{i:03d}AB", seller_id=str(1001230000 + i))
            for i, (name, price) in enumerate(entries)]


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

    def mock_market(self, listings, detail_replies):
        """仅模拟匿名 HTTP，保留生产解析、价格校验和写入逻辑。"""
        ordered = {shop: iter(prices) for shop, prices in detail_replies.items()}
        by_id = {row["offer_id"]: row for row in listings}
        visited = []

        def fetch(url, *, params, headers, **kwargs):
            path = urlsplit(url).path
            if path == "/v3/offer/search":
                return api_response(listings)
            if path == "/v3/offer/keyword_info":
                return brand_response("AI2")
            offer_id = path.removeprefix("/offer/")
            listing = by_id[offer_id]
            name = listing["username"]
            visited.append(name)
            self.assertEqual(headers["Cache-Control"], "no-cache, no-store")
            self.assertEqual(params["currency"], "USD")
            self.assertEqual(params["country"], "US")
            self.assertEqual(str(params["include_out_of_stock"]), "1")
            reply = next(ordered[name])
            if isinstance(reply, Exception):
                raise reply
            response = api_response([])
            detail = dict(listing, display_price=reply, unit_price=reply, converted_unit_price=reply,
                          offer_type="public", is_supported_locally=True,
                          offer_currency="USD", status="live", is_sold_out=False, is_expired=False,
                          unit_quantity=1, is_subscription=False, pricing_packages=None, package_settings=None,
                          wholesale_details=[], other_wholesale_details=[], other_pricing=[])
            response.json.return_value = {"code": 2000, "payload": detail}
            return response

        self.request.get.side_effect = fetch
        return visited

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

    async def test_unsupported_url_uses_enhanced_dom_with_lowest_sort_and_extra_filters(self):
        async with crawl.CrawlSession() as session:
            url = COIN_URL.replace("&sort=lowest_price", "") + "&server_filter=extra"
            await self.crawl_one(session, url=url,
                                 crawl_type="top3", enhance_stores="bound_shop")
            self.assertEqual(self.dom.await_count, 1)
            enhanced_url = self.dom.await_args.args[1]
            expected = dict(parse_qs(urlsplit(COIN_URL).query), server_filter=["extra"])
            self.assertEqual(parse_qs(urlsplit(enhanced_url).query), expected)
            self.assertTrue(self.dom.await_args.kwargs["refresh_unit_prices"])
            self.assertEqual(self.dom.await_args.kwargs["enhance_stores"], "bound_shop")
            unknown_url = COIN_URL.replace("lowest_price", "popular") + "&server_filter=extra"
            await self.crawl_one(session, url=unknown_url)
            self.assertEqual(parse_qs(urlsplit(self.dom.await_args.args[1]).query), expected)
        self.request.get.assert_not_awaited()
        self.playwright.request.new_context.assert_not_awaited()

    async def test_bound_detail_is_fresh_each_round_and_only_final_rows_are_saved(self):
        listings = identified_offers([("own_SHOP", "bad"), ("ShopA", "0.10"),
                                      ("ShopB", "0.11"), ("Outside", "0.12")])
        visits = self.mock_market(listings, {"ShopB": ["0.17", "0.19"]})
        with patch.object(db, "get_crawl_strategies") as policies:
            async with crawl.CrawlSession() as session:
                for i in range(2):
                    await self.crawl_one(session, target_id=10 + i, crawl_type="1",
                                         enhance_stores="own_shop,shopb,Outside")
        policies.assert_not_called()
        self.assertEqual(visits, ["ShopB", "ShopB"])
        self.dom.assert_not_awaited()
        self.page.assert_not_awaited()
        for i, price in enumerate(("0.17", "0.19")):
            saved = self.save.call_args_list[i].args[2]
            self.assertEqual([row["seller_name"] for row in saved], ["ShopA", "ShopB"])
            self.assertEqual([row["price"] for row in saved], ["0.1", price])
            self.assertEqual(saved[1]["unit_price"], price)
            self.assertEqual(saved[1]["currency"], "USD")
            self.assertEqual(saved[1]["unit_price_source"], "offer_detail_api")
            # save_crawl_data serializes each item in raw_data, so examine the entire nested value.
            encoded = json.dumps(saved)
            for listing in listings:
                self.assertNotIn(listing["offer_id"], encoded)
                self.assertNotIn(listing["seller_id"], encoded)
        self.assertEqual([c.args for c in self.notify.call_args_list], [(10, 8, 2), (11, 8, 2)])

    async def test_unmatched_bound_names_and_empty_strategies_keep_all_list_prices(self):
        listings = identified_offers([("ShopA", "1.2"), ("ShopB", "1.4")])
        visits = self.mock_market(listings, {})
        with patch.object(db, "get_crawl_strategies", return_value=[]) as strategies:
            async with crawl.CrawlSession() as session:
                await self.crawl_one(session, target_id=10, crawl_type=1, enhance_stores="absent")
                await self.crawl_one(session, target_id=11, crawl_type=1, enhance_stores="")
        strategies.assert_called_once_with(11)
        self.assertEqual(visits, [])
        self.dom.assert_not_awaited()
        for call in self.save.call_args_list:
            self.assertEqual([row["price"] for row in call.args[2]], ["1.2", "1.4"])

    async def test_top3_selected_using_list_prices_before_enhancement_preserves_all_rows(self):
        listings = identified_offers([("own_shop", "bad"), ("Blocked", "1"), ("B", "2"),
                                      ("C", "3"), ("D", "4"), ("E", "5")])
        visits = self.mock_market(listings, {"B": ["21"], "C": ["31"], "D": ["41"]})
        policy = {"config": {"blacklist_stores": "Blocked"}, "currency": "USD"}
        with patch.object(db, "get_crawl_strategies", return_value=[policy]), \
             patch.object(config, "CRAWL_OFFER_LIMIT", 10):
            async with crawl.CrawlSession() as session:
                self.assertEqual(await self.crawl_one(session, crawl_type="top3"), 5)
        self.assertEqual(visits, ["B", "C", "D"])
        rows = self.save.call_args.args[2]
        self.assertEqual([r["seller_id"] for r in rows], ["Blocked", "B", "C", "D", "E"])
        self.assertEqual([r["price"] for r in rows], ["1", "21", "31", "41", "5"])
        self.dom.assert_not_awaited()

    async def test_partly_successful_detail_round_discards_api_rows_and_repeats_enhancement_on_page(self):
        listings = identified_offers([("B", "0.3"), ("C", "0.4")])
        visits = self.mock_market(listings, {"B": ["0.6", "0.7"],
                                             "C": [TimeoutError("detail"), TimeoutError("detail")]})
        dom_rows = [{"seller_id": "different_dom_seller", "price": "1.123"}]
        self.dom.side_effect = [dom_rows, TimeoutError("web detail failed")]
        async with crawl.CrawlSession() as session:
            for i in (10, 11):
                target = {"id": i, "game_product_id": 3, "category": "金币", "url": COIN_URL,
                          "crawl_type": 1, "enhance_stores": "B,C"}
                count, failed, _, _ = await crawl.crawl_target(session, 0, target, 1)
                self.assertEqual((count, failed), (1, False) if i == 10 else (0, True))
        self.assertEqual(visits, ["B", "C", "B", "C"])
        self.assertEqual(self.dom.await_count, 2)
        for call in self.dom.await_args_list:
            self.assertTrue(call.kwargs["refresh_unit_prices"])
            self.assertEqual(call.kwargs["enhance_stores"], "B,C")
        self.save.assert_called_once()
        self.assertEqual(self.save.call_args.args[2], dom_rows)
        self.update.assert_called_once()
        self.notify.assert_called_once_with(10, 8, 1)
