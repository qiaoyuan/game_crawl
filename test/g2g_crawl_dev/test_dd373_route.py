import unittest
from unittest.mock import AsyncMock

from g2g.crawl.dd373_route import parse_dd373_url, resolve_dd373_route


URL = "https://www.dd373.com/s-game-c-coin-eu-faction-server.html"


def success(data):
    return {"StatusCode": "0", "StatusData": {"ResultCode": "0", "ResultData": data}}


def route_data():
    return [
        success([{"Identify": "game", "Id": "g"}]),
        success({"GameGoodsType": [{"Identtify": "coin", "Id": "c", "ParentId": "g", "Property": "游戏币"}]}),
        success({"GameOther": [{"Identify": "eu", "Id": "r", "ParentId": "g", "Name": "欧服"}]}),
        success({"GameOther": [{"Identify": "faction", "Id": "f", "ParentId": "r", "Name": "魔族"}]}),
        success({"GameOther": [{"Identify": "server", "Id": "s", "ParentId": "f", "Name": "测试服"}]}),
        success({"GameOther": []}),
    ]


class DD373RouteTests(unittest.IsolatedAsyncioTestCase):
    async def test_short_codes_resolve_in_parent_order(self):
        request = AsyncMock(side_effect=route_data())
        params, region = await resolve_dd373_route(URL, request)
        self.assertEqual(region, "欧服/魔族/测试服")
        self.assertEqual(params, {"GameId": "g", "GameShopTypeId": "c", "GameOtherId": "r_f_s"})
        self.assertEqual([call.args[1] for call in request.await_args_list],
                         [{}, {"parentId": "g"}, {"parentId": "g"}, {"parentId": "r"}, {"parentId": "f"},
                          {"parentId": "s"}])

    async def test_unknown_or_ambiguous_routes_fail(self):
        for condition in ("changed_parent", "duplicate_code", "non_currency", "failed_catalog"):
            rows = route_data()
            if condition == "changed_parent":
                rows[-2]["StatusData"]["ResultData"]["GameOther"][0]["ParentId"] = "other"
            elif condition == "duplicate_code":
                rows[0]["StatusData"]["ResultData"] *= 2
            elif condition == "non_currency":
                rows[1]["StatusData"]["ResultData"]["GameGoodsType"][0]["Property"] = "装备"
            else:
                rows[-1]["StatusCode"] = "1"
            with self.subTest(condition=condition), self.assertRaises(ValueError):
                await resolve_dd373_route(URL, AsyncMock(side_effect=rows))

    async def test_partial_server_selection_rejected(self):
        with self.assertRaisesRegex(ValueError, "具体服务器"):
            await resolve_dd373_route(URL.replace("-server.html", ".html"), AsyncMock(side_effect=route_data()))

    def test_only_unambiguous_scoped_listing_links(self):
        for target in (URL + "?sort=desc", URL.replace("www.dd373.com", "example.org"),
                       URL.replace("-eu-faction-server", ""), URL.replace("-c-", "-x-")):
            with self.subTest(target=target), self.assertRaises(ValueError):
                parse_dd373_url(target)
