"""在本地页面上核对 Eldorado 订单详情字段，不访问真实交易。"""

import unittest
from unittest.mock import patch

from playwright.async_api import Error as PlaywrightError, async_playwright

from g2g.eldorado.shared import eldorado_order_details as details


class OrderFieldTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        pw = await async_playwright().start()
        self.addAsyncCleanup(pw.stop)
        browser = await pw.chromium.launch()
        self.addAsyncCleanup(browser.close)
        self.page = await browser.new_page()
        self.order_id = "example-order"
        self.url = "https://www.eldorado.gg/order/example-order"

    async def load(self, html):
        await self.page.route("**/*", lambda route: route.fulfill(
            content_type="text/html; charset=utf-8", body=html))
        await self.page.goto(self.url + "?ignored=true")

    async def test_order_id_must_match_url(self):
        await self.load("")
        with self.assertRaisesRegex(PlaywrightError, "订单详情地址"):
            await details.read_order_fields(self.page, "other")

    async def test_reads_visible_pairs_and_preserves_displayed_units_and_signs(self):
        await self.load(_DETAIL)
        self.assertEqual(await details.read_order_fields(self.page, self.order_id), {
            "order_id": self.order_id, "game": "Aion 2", "region": "EU", "faction": "Example faction",
            "server": "Sample 1", "quantity": "12 M", "buyer": "sample-buyer",
            "order_price": "$10.00", "commission": "−$0.50", "you_receive": "$9.50",
        })

    async def test_rejects_mismatched_visible_id_missing_payment_and_wrong_game(self):
        await self.load(_DETAIL)
        with patch.object(details, "_TIMEOUT_MS", 250):
            for source in (
                _DETAIL.replace("Order ID: example-order", "Order ID: different-order"),
                _DETAIL.replace(">Commission<", ">Unrelated fee<"),
                _DETAIL.replace(">Aion 2<", ">Aion 20<"),
            ):
                with self.subTest():
                    await self.page.set_content(source)
                    with self.assertRaises(PlaywrightError):
                        await details.read_order_fields(self.page, self.order_id)


_DETAIL = """
    <eld-order-header><div class="order-id" hidden><span>Order ID: hidden-order</span></div>
        <div class="order-id"><span>Order ID: example-order</span></div></eld-order-header>
    <eld-seller-order-details-card><div class="order-details-card">
        <div><span>Game</span><span>Aion 2</span></div>
        <div><span>Region</span><div><span>EU</span></div></div>
        <div><span>Faction</span><span>Example faction</span></div>
        <div><span>Server</span><span>Sample 1</span></div>
        <div><span>Quantity:</span><div><span>12</span> M</div></div>
        <div><span>Buyer</span><div><a>sample-buyer</a></div></div>
        <div hidden><span>Buyer</span><span>hidden-buyer</span></div>
        <div><span>Unselected</span><div>private</div></div>
    </div></eld-seller-order-details-card>
    <eld-order-seller-payment><div class="prices-container">
        <div><label class="label">Order Price</label><span>$10.00</span></div>
        <div><label class="label">Commission</label><span>−$0.50</span></div>
        <div><label>You receive</label><span>$9.50</span></div>
    </div></eld-order-seller-payment>
"""
