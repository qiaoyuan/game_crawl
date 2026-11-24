"""用本地订单列表验证游戏字段、重复链接及详情导航。"""

import unittest

from playwright.async_api import Error as PlaywrightError, async_playwright

from g2g.eldorado.shared.eldorado_sold_list import (SOLD_URL, find_recent_orders, find_sold_orders, is_order_url,
                                    is_sold_url, open_sold_order)


def order_row(game, order_href, buyer="Buyer", style="", date=""):
    return f"""<a class="grid-row" href="{order_href}" style="{style}">
        <div class="grid-cell game-name"><eld-dashboard-orders-list-game>
        <div class="order-list-game"><img alt="{game}"><div class="game-info">
        <h6 style="display:none">mobile</h6><h6>{game}</h6></div></div>
        </eld-dashboard-orders-list-game></div><div class="grid-cell order-user">{buyer}</div>
        <div class="grid-cell date"><p class="mobile-label">Ordered date</p>
        <p class="font-bold">{date}</p></div></a>"""


class SoldListTests(unittest.IsolatedAsyncioTestCase):
    async def test_recent_reads_first_three_raw_rows_in_browser_timezone(self):
        raw = "Nov 23, 2026, 7:58:02\u202fPM"
        sold = "".join([
            '<eld-dropdown><div class="dropdown-trigger" role="combobox">All statuses</div></eld-dropdown>',
            '<eld-dropdown><div class="dropdown-trigger" role="combobox">Recent</div></eld-dropdown>',
            order_row("Hidden", "/order/hidden", style="display:none"),
            order_row("Other", "/order/one?ref=tmp", date=raw),
            order_row("Aion 2", "/order/two", date=raw),
            order_row("Other", "/order/three", date="Nov 23, 2026, 12:01:03 AM"),
            order_row("Aion 2", "/order/four", date="ignored"),
        ])
        async with async_playwright() as p:
            browser = await p.chromium.launch()
            try:
                context = await browser.new_context(timezone_id="Asia/Shanghai")
                page = await context.new_page()
                await page.route("**/*", lambda route: route.fulfill(content_type="text/html; charset=utf-8", body=sold))
                await page.goto(SOLD_URL)
                orders = await find_recent_orders(page)
                self.assertEqual([o["order_id"] for o in orders], ["one", "two", "three"])
                self.assertEqual(orders[0], {"order_id": "one", "game": "Other",
                                 "url": "https://www.eldorado.gg/order/one", "ordered_at_raw": raw,
                                 "ordered_at": "2026-11-23T11:58:02Z", "ordered_at_epoch": 1795435082})
                self.assertEqual(orders[-1]["ordered_at"], "2026-11-22T16:01:03Z")
                sort = page.get_by_role("combobox").nth(1)
                await sort.evaluate("el => el.textContent='Oldest'")
                with self.assertRaisesRegex(PlaywrightError, "Recent"):
                    await find_recent_orders(page)
                await sort.evaluate("el => el.textContent='Recent'")
                await page.locator(".date").nth(2).evaluate("el => el.textContent='Nov 24, 2026, 1:01:01 PM'")
                with self.assertRaisesRegex(PlaywrightError, "从新到旧"):
                    await find_recent_orders(page)
                await page.locator(".date").nth(2).evaluate("el => el.textContent='Nov 31, 2026, 1:01:01 PM'")
                with self.assertRaisesRegex(PlaywrightError, "时间无效"):
                    await find_recent_orders(page)
            finally:
                await browser.close()

    def test_trusted_urls(self):
        self.assertTrue(is_sold_url(SOLD_URL + "?sort=date"))
        self.assertTrue(is_order_url("https://www.eldorado.gg/order/a-1?ref=list"))
        for url in ["https://www.eldorado.gg.evil.test/order/1",
                    "https://www.eldorado.gg/order/1/cancel",
                    "https://www.eldorado.gg/order/", "https://u:p@www.eldorado.gg/order/1"]:
            self.assertFalse(is_order_url(url))

    async def test_matches_game_cell_and_releases_only_owned_page(self):
        sold = "".join([
            order_row("Other game", "/order/wrong", buyer="Aion2"),
            order_row("Aion 2", "/order/one?temporary=y"),
            order_row("Aion2", "/order/one#duplicate"),
            order_row("AION2", "/order/two"),
            order_row("Aion 20", "/order/another"),
            order_row("Aion2", "https://outside.test/order/1"),
            order_row("Aion2", "/order/hidden", style="display:none")
        ])
        async with async_playwright() as p:
            browser = await p.chromium.launch()
            try:
                context = await browser.new_context()

                async def serve(route):
                    if route.request.url.endswith("/two"):
                        await route.fulfill(status=302, headers={"Location": "/login"})
                    else:
                        await route.fulfill(content_type="text/html", body=sold if
                                            is_sold_url(route.request.url) else "<p>Detail</p>")

                await context.route("**/*", serve)
                page = await context.new_page()
                await page.goto(SOLD_URL)
                orders = await find_sold_orders(page)
                self.assertEqual(orders, ["https://www.eldorado.gg/order/one", "https://www.eldorado.gg/order/two"])
                async with open_sold_order(page, orders[0]) as detail:
                    self.assertEqual(detail.url, orders[0])
                with self.assertRaises(PlaywrightError):
                    async with open_sold_order(page, orders[1]):
                        self.fail("Login redirect should not be read as an order")
                self.assertEqual(context.pages, [page])
                self.assertEqual(page.url, SOLD_URL)
                await page.set_content("<p>No orders found</p>")
                self.assertEqual(await find_sold_orders(page), [])
            finally:
                await browser.close()
