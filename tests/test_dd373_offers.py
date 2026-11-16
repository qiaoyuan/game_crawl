import unittest
from decimal import Decimal
from unittest.mock import AsyncMock, MagicMock, patch

from g2g import dd373


def response(*rows):
    return {"StatusCode": "0", "StatusData": {"ResultCode": "0", "ResultData": list(rows)}}


def offer(**changes):
    return {
        "number": "6", "unit": "万基纳", "amount": "1000.0",
        "singleprice": "0.0289012", "price": "28.90", **changes,
    }


class OfferTests(unittest.TestCase):
    def test_published_lots_stock_and_bundle_price(self):
        row, = dd373.parse_shop_offers(response(offer()), "欧服/魔族/测试服")
        self.assertEqual(row, {
            "seller_name": "", "deal_rate": None, "region": "欧服/魔族/测试服",
            "stock": Decimal("6000"), "sales_unit": "1万基纳", "price": Decimal("0.0289012"),
            "currency": "CNY", "line_quantity": Decimal("1000"),
            "line_stock_amount": Decimal("173.40"), "line_total": Decimal("28.90"), "offer_url": "",
        })

    def test_only_explicit_successful_empty_list_is_empty(self):
        self.assertEqual(dd373.parse_shop_offers(response(), "测试"), [])
        for payload in (None, {}, response() | {"StatusCode": "1"},
                        {"StatusCode": "0", "StatusData": {"ResultCode": "9", "ResultData": []}},
                        {"StatusCode": "0", "StatusData": {"ResultCode": "0"}}):
            with self.subTest(payload=payload), self.assertRaises(ValueError):
                dd373.parse_shop_offers(payload, "测试")

    def test_one_invalid_offer_rejects_whole_snapshot(self):
        for changes in ({"unit": ""}, {"number": "1.2"}, {"price": "0"}, {"singleprice": "NaN"},
                        {"singleprice": "Infinity"}, {"amount": None}, {"number": -1}):
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                dd373.parse_shop_offers(response(offer(), offer(**changes)), "测试")


class ScrapeTests(unittest.IsolatedAsyncioTestCase):
    async def test_rereads_prices_via_official_ordinary_goods_endpoint(self):
        params = {"GameId": "game", "GameOtherId": "server", "GameShopTypeId": "category"}
        route = AsyncMock(return_value=(params, "欧服/魔族/测试服"))
        request = AsyncMock(side_effect=[response(offer()), response(offer(price="28.91"))])
        with (patch.object(dd373, "resolve_dd373_route", route), patch.object(dd373, "_get_json", request),
              patch.object(dd373.config, "DD373_FETCH_DEAL_RATES", False),
              patch.object(dd373, "_fetch_deal_rates") as page):
            first = await dd373.scrape_purchase_offers("listing")
            second = await dd373.scrape_purchase_offers("listing")
        self.assertEqual(first[0]["line_total"], Decimal("28.90"))
        self.assertEqual(second[0]["line_total"], Decimal("28.91"))
        page.assert_not_called()
        self.assertEqual(request.await_count, 2)
        request.assert_awaited_with(dd373.SHOP_LIST_API, params)

    async def test_worker_caches_successful_routes_only_and_refreshes_offers_and_rates(self):
        lookup = AsyncMock(side_effect=[ValueError("目录失败"), ({"id": "a"}, "A"), ({"id": "b"}, "B")])
        request = AsyncMock(side_effect=[response(offer(shopno="a")),
                                         response(offer(shopno="a", price="29.00")), response()])
        cache = {}
        with (patch.object(dd373, "resolve_dd373_route", lookup), patch.object(dd373, "_get_json", request),
              patch.object(dd373.config, "DD373_FETCH_DEAL_RATES", True),
              patch.object(dd373, "_fetch_deal_rates", side_effect=[{"a": Decimal(10)}, {"a": Decimal(15)}]) as page):
            with self.assertRaises(ValueError):
                await dd373.scrape_purchase_offers("url-a", cache)
            self.assertEqual(cache, {})
            first = await dd373.scrape_purchase_offers("url-a", cache)
            second = await dd373.scrape_purchase_offers("url-a", cache)
            await dd373.scrape_purchase_offers("url-b", cache)
        self.assertEqual(list(cache), ["url-a", "url-b"])
        self.assertEqual(lookup.await_count, 3)
        self.assertEqual(request.await_count, 3)
        self.assertEqual(page.call_count, 2)
        self.assertEqual((first[0]["deal_rate"], second[0]["deal_rate"]), (Decimal(10), Decimal(15)))
        self.assertEqual(second[0]["line_total"], Decimal("29.00"))


def card(shopno, label):
    return (f'<div class="border-box"><div class="seller"><span class="font12">{label}</span></div>'
            f'<div class="shop-btn-group" id="buyButton{shopno}">立即购买</div></div>')


class DealRateTests(unittest.IsolatedAsyncioTestCase):
    def test_rates_belong_to_one_card_and_one_id(self):
        markup = (card("a", "成交率：0.0%") + card("b", "成交率：100%") +
                  card("same", "成交率：90.0%") + card("same", "无") +
                  card("bad", "成交率：100.1%") + card("none", "信用：") +
                  '<div class="border-box"><div class="shop-btn-group" id="buyButtonamb"></div>'
                  '<div class="shop-btn-group" id="buyButtonamb2"></div>'
                  '<span class="font12">成交率：80%</span></div>' +
                  '<div class="border-box"><div class="shop-btn-group" id="buyButtonouter"></div>' +
                  card("inner", "成交率：45%") + '</div>')
        self.assertEqual(dd373._parse_deal_rates(markup), {
            "a": Decimal(0), "b": Decimal(100), "same": None, "bad": None, "none": None,
            "amb": None, "amb2": None, "outer": None, "inner": Decimal(45),
        })

    async def test_match_by_shopno_after_api_validation_preserving_api_amounts(self):
        items = [offer(shopno="b"), offer(shopno="not-on-page"), offer(shopno="a"),
                 offer(shopno="a"), offer(shopno=None)]
        html = (card("a", "成交率：10%") + card("b", "成交率：97.4%") + card("unrelated", "成交率：50%"))
        listing = 'https://www.dd373.com/s-test.html'
        mock_response = MagicMock(status_code=200, headers={"Content-Type": "text/html"},
                                  content=('<meta charset="utf-8">' + html).encode())
        with (patch.object(dd373.config, "DD373_FETCH_DEAL_RATES", True),
              patch.object(dd373, "resolve_dd373_route", AsyncMock(return_value=({}, "测试"))),
              patch.object(dd373, "_get_json", AsyncMock(return_value=response(*items))),
              patch.object(dd373.requests, "get") as page):
            page.return_value.__enter__.return_value = mock_response
            results = await dd373.scrape_purchase_offers(listing)
        self.assertEqual(page.call_count, 1)
        self.assertEqual(page.call_args.args, (listing,))
        self.assertFalse(page.call_args.kwargs["allow_redirects"])
        self.assertEqual([row["deal_rate"] for row in results], [Decimal("97.4"), None, None, None, None])
        original = dd373.parse_shop_offers(response(*items), "测试")
        self.assertEqual([row | {"deal_rate": None} for row in results], original)

    async def test_challenge_redirect_and_page_failure_preserve_quotes(self):
        row = offer(shopno="a")
        quotes = dd373.parse_shop_offers(response(row), "测试")
        stale_card = card("a", "成交率：100%")
        for status, body in [(200, "<p>Please slide to verify</p>" + stale_card),
                             (200, "<title>登录中心</title>" + stale_card), (302, stale_card)]:
            with self.subTest(status=status), patch.object(dd373.requests, "get") as page:
                page.return_value.__enter__.return_value = MagicMock(
                    status_code=status, headers={"Content-Type": "text/html"}, content=('<meta charset="utf-8">' + body).encode())
                await dd373._add_deal_rates("listing", [row], quotes)
                self.assertIsNone(quotes[0]["deal_rate"])
        with patch.object(dd373.requests, "get", side_effect=dd373.requests.Timeout):
            await dd373._add_deal_rates("listing", [row], quotes)
        self.assertEqual(quotes, dd373.parse_shop_offers(response(row), "测试"))


class PurchaseLimitTests(unittest.IsolatedAsyncioTestCase):
    async def test_limit_request_and_cached_route_are_independent(self):
        cache = {"listing": ({"id": "a"}, "A")}
        payload = response(offer(singleprice="0.042", price="42"), offer(singleprice="0.0289", price="28.9"),
                           offer(singleprice="0.035", price="35"))
        request = AsyncMock(return_value=payload)
        with (patch.object(dd373, "_get_json", request),
              patch.object(dd373.config, "DD373_FETCH_DEAL_RATES", False)):
            for limit, prices in [(1, [42]), (2, [42, "28.9"]), (0, [42, "28.9", 35]), (30, [42, "28.9", 35])]:
                with self.subTest(limit=limit):
                    rows = await dd373.scrape_purchase_offers("listing", cache, limit=limit)
                    self.assertEqual([r["line_total"] for r in rows], [Decimal(p) for p in prices])
                    params = {"id": "a", "limit": limit} if limit else {"id": "a"}
                    request.assert_awaited_with(dd373.SHOP_LIST_API, params)
        self.assertEqual(cache["listing"][0], {"id": "a"})

    async def test_only_parse_and_enrich_selected_offers_but_check_all_ids(self):
        selected = [offer(shopno="duplicate"), offer(shopno="unique")]
        trailing = [None, {"shopno": "duplicate"}, offer(shopno="outside", price="broken")]
        request = AsyncMock(return_value=response(*selected, *trailing))
        cache = {"listing": ({}, "A")}
        with (patch.object(dd373, "_get_json", request),
              patch.object(dd373.config, "DD373_FETCH_DEAL_RATES", True),
              patch.object(dd373, "_fetch_deal_rates", return_value={"unique": Decimal(95)}) as fetch):
            rows = await dd373.scrape_purchase_offers("listing", cache, limit=2)
            self.assertEqual([row["deal_rate"] for row in rows], [None, Decimal(95)])
            fetch.assert_called_once_with("listing", {"unique"})
            with self.assertRaises(ValueError):
                await dd373.scrape_purchase_offers("listing", cache, limit=3)

    async def test_empty_success_and_invalid_limits(self):
        request = AsyncMock(return_value=response())
        with (patch.object(dd373, "_get_json", request),
              patch.object(dd373.config, "DD373_FETCH_DEAL_RATES", True),
              patch.object(dd373, "_fetch_deal_rates") as fetch):
            for limit in (-1, True, 1.5, "2"):
                with self.subTest(limit=limit), self.assertRaises(ValueError):
                    await dd373.scrape_purchase_offers("listing", limit=limit)
            request.assert_not_awaited()
            rows = await dd373.scrape_purchase_offers("listing", {"listing": ({}, "A")}, limit=5)
            self.assertEqual(rows, [])
            fetch.assert_not_called()
