"""本地浏览器验证从售出页刷新、建立起点，到新订单落盘的完整流程。"""

import asyncio
import contextlib
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import urlsplit

from playwright.async_api import async_playwright

from g2g.eldorado.monitor.eldorado_order_journal import OrderJournal
from g2g.eldorado.monitor.monitor_eldorado import EventHub, monitor_sold


def row(order_id, game, second):
    return (f'<a class="grid-row" href="/order/{order_id}">'
            f'<div class="game-name"><h6>{game}</h6></div>'
            f'<div class="date"><p class="font-bold">Nov 23, 2026, 8:00:{second:02d} PM</p></div></a>')


_DETAIL = """
<eld-order-header><div class="order-id"><span>Order ID: new</span></div></eld-order-header>
<eld-seller-order-details-card><div class="order-details-card">
    <div><span>Game</span><span>Aion 2</span></div>
    <div><span>Region</span><span>EU</span></div>
    <div><span>Faction</span><span>Example</span></div>
    <div><span>Server</span><span>Test 1</span></div>
    <div><span>Quantity</span><span>10 M</span></div>
    <div><span>Buyer</span><span>example-buyer</span></div>
</div></eld-seller-order-details-card>
<eld-order-seller-payment><div class="prices-container">
    <div><label class="label">Order Price</label><span>$10.00</span></div>
    <div><label class="label">Commission</label><span>−$0.50</span></div>
    <div><label>You receive</label><span>$9.50</span></div>
</div></eld-order-seller-payment>
"""


class NewOrderMonitorTests(unittest.IsolatedAsyncioTestCase):
    async def test_baseline_then_export_one_new_aion_order_from_first_row(self):
        baseline = row("old", "Aion 2", 10) + row("old-other", "Another game", 9)
        updated = (row("new", "AION2", 14) + row("first", "Another game", 13)
                   + row("third", "Aion 20", 13) + row("fourth", "Aion 2", 12))
        baseline_repeated, stable_after_save = asyncio.Event(), asyncio.Event()
        show_new = False
        detail_paths = []
        baseline_reads = reads_after_save = 0

        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "new_orders.jsonl"

            async def serve(route):
                nonlocal baseline_reads, reads_after_save
                path = urlsplit(route.request.url).path
                if path == "/dashboard/orders/sold":
                    body = updated if show_new else baseline
                    if not show_new:
                        baseline_reads += 1
                        if baseline_reads >= 2:
                            baseline_repeated.set()
                    elif output.stat().st_size:
                        reads_after_save += 1
                        if reads_after_save >= 2:
                            stable_after_save.set()
                elif path.startswith("/order/"):
                    detail_paths.append(path)
                    body = _DETAIL
                else:
                    await route.abort()
                    return
                await route.fulfill(content_type="text/html; charset=utf-8", body=body)

            async with async_playwright() as playwright, await playwright.chromium.launch() as browser:
                context = await browser.new_context(timezone_id="Asia/Shanghai")
                await context.route("**/*", serve)
                with OrderJournal(output) as journal:
                    task = asyncio.create_task(monitor_sold(browser, EventHub(), SimpleNamespace(interval=0.15),
                                                            journal=journal))
                    try:
                        await asyncio.wait_for(baseline_repeated.wait(), timeout=6)
                        self.assertEqual(detail_paths, [])
                        self.assertEqual(output.read_bytes(), b"")
                        show_new = True
                        await asyncio.wait_for(stable_after_save.wait(), timeout=8)
                    finally:
                        task.cancel()
                        with contextlib.suppress(asyncio.CancelledError):
                            await task

            self.assertEqual(detail_paths, ["/order/new"])
            lines = output.read_text(encoding="utf-8").splitlines()
            self.assertEqual(len(lines), 1)
            self.assertEqual(json.loads(lines[0]), {
                "order_id": "new", "ordered_at": "2026-11-23T12:00:14Z", "game": "Aion 2",
                "region": "EU", "faction": "Example", "server": "Test 1", "quantity": "10 M",
                "buyer": "example-buyer", "order_price": "$10.00", "commission": "−$0.50",
                "you_receive": "$9.50",
            })
