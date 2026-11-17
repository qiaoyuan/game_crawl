import contextlib
import io
import unittest
from datetime import datetime
from decimal import Decimal
from unittest.mock import AsyncMock, MagicMock, patch

from g2g import db
from tools import crawl_from_db


def purchase_target():
    return {"id": 8, "game_product_id": 13, "task_type": "2", "version": 7,
            "url": "https://www.dd373.com/s-game-c-coin-realm.html",
            "category": "物品", "crawl_type": 1}


def purchase_row():
    return {"deal_rate": Decimal("95.9"), "region": "示例区服", "stock": 6000,
            "sales_unit": "1万基纳", "line_quantity": 1000,
            "line_stock_amount": Decimal("173.40"), "line_total": Decimal("173.40"),
            "price": Decimal("0.0289"), "currency": "CNY"}


class PurchaseRouteTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        stack = contextlib.ExitStack()
        self.addCleanup(stack.close)
        self.output = stack.enter_context(contextlib.redirect_stdout(io.StringIO()))
        self.scrape_offers = crawl_from_db.dd373.scrape_purchase_offers
        self.source = stack.enter_context(patch.object(
            crawl_from_db.dd373, "scrape_purchase_offers", new_callable=AsyncMock,
        ))
        self.database = stack.enter_context(patch.object(crawl_from_db, "db"))
        self.no_driver = stack.enter_context(patch.object(crawl_from_db, "async_playwright"))
        self.session = crawl_from_db.CrawlSession()

    def tearDown(self):
        self.database.get_crawl_strategies.assert_not_called()
        self.database.increment_version.assert_not_called()
        self.database.save_crawl_data.assert_not_called()
        self.database.update_last_crawl.assert_not_called()
        self.database.insert_crawl_notify.assert_not_called()
        self.no_driver.assert_not_called()

    async def test_purchase_route_isolated_from_competitor_workflow(self):
        self.source.return_value = [purchase_row()]
        self.database.save_purchase_snapshot.return_value = 8
        for task_type in (2, "2"):
            with self.subTest(task_type=task_type):
                target = dict(purchase_target(), task_type=task_type)
                saved, failed, wall, _ = await crawl_from_db.crawl_target(self.session, 0, target, 1)
                self.assertEqual((saved, failed, target["version"]), (1, False, 8))
                self.source.assert_awaited_with(target["url"], route_cache=self.session.dd373_routes, limit=10)
                self.database.save_purchase_snapshot.assert_called_with(
                    target, self.source.return_value, datetime.fromtimestamp(wall).replace(microsecond=0),
                )
                self.assertIn("task_type=2（收货）", self.output.getvalue())

    async def test_purchase_passes_each_targets_limit_as_an_integer(self):
        for value, expected in [(10, 10), ("10", 10), (30, 30), ("015", 15)]:
            with self.subTest(value=value):
                target = dict(purchase_target(), limit=value)
                self.source.return_value = []
                saved, failed, *_ = await crawl_from_db.crawl_target(self.session, 0, target, 1)
                self.assertEqual((saved, failed), (0, False))
                self.source.assert_awaited_with(target["url"], route_cache=self.session.dd373_routes, limit=expected)

    async def test_invalid_purchase_limit_fails_before_fetch_or_write(self):
        for value in (0, 9, None, "", -1, "-1", "1.0", 1.5, True):
            with self.subTest(value=value):
                result = await crawl_from_db.crawl_target(self.session, 0, dict(purchase_target(), limit=value), 1)
                self.assertEqual(result, (0, True, None, None))
        self.source.assert_not_awaited()
        self.database.save_purchase_snapshot.assert_not_called()

    async def test_failed_fetch_leaves_no_snapshot(self):
        self.source.side_effect = RuntimeError("取价失败")
        result = await crawl_from_db.crawl_target(self.session, 0, purchase_target(), 1)
        self.assertEqual(result, (0, True, None, None))
        self.source.assert_awaited_once()
        self.database.save_purchase_snapshot.assert_not_called()

    async def test_route_cache_is_scoped_to_the_worker_session(self):
        self.source.return_value = []
        target = purchase_target()
        self.database.save_purchase_snapshot.side_effect = [8, 9, 10]
        async with self.session:
            await crawl_from_db.crawl_target(self.session, 0, target, 1)
            first = self.source.await_args.kwargs["route_cache"]
            first[target["url"]] = ({}, "示例区服")
            await crawl_from_db.crawl_target(self.session, 0, target, 1)
            self.assertIs(first, self.source.await_args.kwargs["route_cache"])
        self.assertEqual(first, {})
        async with crawl_from_db.CrawlSession() as new_session:
            await crawl_from_db.crawl_target(new_session, 0, target, 1)
            self.assertIsNot(first, self.source.await_args.kwargs["route_cache"])

    async def test_misconfigured_targets_cannot_use_competitor_pipeline(self):
        dd373_url = purchase_target()["url"]
        g2g_url = "https://www.g2g.com/example"
        for task_type, category, url in (
            (1, "物品", dd373_url), ("1", "DD373游戏币", g2g_url),
            (3, "物品", g2g_url), ("unknown", "物品", g2g_url),
        ):
            target = dict(purchase_target(), task_type=task_type, category=category, url=url)
            with self.subTest(task_type=task_type, category=category, url=url):
                result = await crawl_from_db.crawl_target(self.session, 0, target, 1)
                self.assertEqual(result, (0, True, None, None))
        self.source.assert_not_awaited()
        self.database.save_purchase_snapshot.assert_not_called()

    async def test_purchase_with_g2g_url_rejects_configuration_without_network_or_browser(self):
        self.source.side_effect = self.scrape_offers
        target = dict(purchase_target(), url="https://www.g2g.com/example")
        with patch.object(crawl_from_db.dd373, "_get_json", new_callable=AsyncMock) as request:
            result = await crawl_from_db.crawl_target(self.session, 0, target, 1)
        self.assertEqual(result, (0, True, None, None))
        self.assertIn("task_type=2（收货）", self.output.getvalue())
        self.assertIn("DD373 普通商品列表原始链接", self.output.getvalue())
        self.source.assert_awaited_once_with(target["url"], route_cache=self.session.dd373_routes, limit=10)
        request.assert_not_awaited()
        self.database.save_purchase_snapshot.assert_not_called()


class CompetitorRouteTests(unittest.IsolatedAsyncioTestCase):
    async def test_zero_and_legacy_targets_keep_competitor_collection_and_notification(self):
        for fields in ({"task_type": 1}, {"task_type": "1"}, {}):
            with self.subTest(fields=fields), patch.object(crawl_from_db, "db") as database:
                target = {"id": 5, "game_product_id": 13, "category": "物品", "limit": 1,
                          "url": "https://www.g2g.com/example", **fields}
                session = AsyncMock()
                database.increment_version.return_value = 4
                database.save_crawl_data.return_value = 1
                items = [{"price": "0.12", "currency": "USD"}]
                with patch.object(crawl_from_db, "scrape_page", return_value=items) as scrape, \
                        contextlib.redirect_stdout(io.StringIO()) as output:
                    result = await crawl_from_db.crawl_target(session, 0, target, 1)
                self.assertEqual(result[:2], (1, False))
                self.assertIn("task_type=1（竞品）", output.getvalue())
                scrape.assert_awaited_once_with(session.get_g2g_page.return_value, target["url"])
                database.save_crawl_data.assert_called_once_with(5, "g2g", items, game_product_id=13, version=4)
                database.insert_crawl_notify.assert_called_once_with(5, 4, 1)
                database.save_purchase_snapshot.assert_not_called()


class PurchaseTransactionTests(unittest.TestCase):
    def setUp(self):
        self.connection = MagicMock()
        self.cursor = self.connection.cursor.return_value.__enter__.return_value
        self.cursor.fetchone.return_value = dict(purchase_target())
        connection_patch = patch.object(db, "get_connection", return_value=self.connection)
        connection_patch.start()
        self.addCleanup(connection_patch.stop)

    def test_confirmed_empty_snapshot_updates_version_without_inserting_rows(self):
        version = db.save_purchase_snapshot(purchase_target(), [], datetime.now())
        self.assertEqual(version, 8)
        lock, update, notify = self.cursor.execute.call_args_list
        self.assertIn("INSERT INTO crawl_notify", notify.args[0])
        self.assertEqual(notify.args[1], (8, 8, 0))
        self.assertIn("FOR UPDATE", lock.args[0])
        self.assertIn("UPDATE crawl_target SET version", update.args[0])
        self.cursor.executemany.assert_not_called()
        self.connection.commit.assert_called_once()

    def test_batch_preserves_offer_order_version_and_rmb_precision(self):
        offers = [purchase_row(), dict(purchase_row(), price=Decimal("0.01875"), line_total=Decimal("112.50")),
                  dict(purchase_row(), stock=1000, line_total=Decimal("28.90"))]
        version = db.save_purchase_snapshot(purchase_target(), offers.copy(), datetime.now())
        self.assertEqual(version, 8)
        sql, rows = self.cursor.executemany.call_args.args
        columns = sql.split("(", 1)[1].split(")", 1)[0].replace(" ", "").replace("\n", "").split(",")
        self.assertEqual([dict(zip(columns, row, strict=True)) for row in rows], [{
            **offer, "target_id": 8, "game_product_id": 13, "version": 8,
            "seller_name": "", "offer_url": "", "crawled_at": rows[0][-1],
        } for offer in offers])
        self.connection.commit.assert_called_once()
        self.connection.close.assert_called_once()

    def test_conflicting_or_failed_write_is_rolled_back(self):
        for conflict in (True, False):
            with self.subTest(conflict=conflict):
                self.connection.reset_mock()
                self.cursor.fetchone.return_value = None if conflict else dict(purchase_target())
                self.cursor.executemany.side_effect = RuntimeError("插入失败")
                with self.assertRaises(RuntimeError):
                    db.save_purchase_snapshot(purchase_target(), [purchase_row()], datetime.now())
                if conflict:
                    self.cursor.executemany.assert_not_called()
                self.connection.rollback.assert_called_once()
                self.connection.commit.assert_not_called()
                self.connection.close.assert_called_once()

    def test_changed_purchase_limit_is_checked_under_lock_before_version_or_rows(self):
        for original, current, changed in [
            ({}, {"limit": 2}, True), ({"limit": 3}, {"limit": 0}, True),
            ({"limit": "02"}, {"limit": 2}, False), ({}, {"limit": 0}, False),
        ]:
            with self.subTest(original=original, current=current):
                self.connection.reset_mock()
                self.cursor.fetchone.return_value = dict(purchase_target(), **current)
                target = dict(purchase_target(), **original)
                if changed:
                    with self.assertRaisesRegex(RuntimeError, "配置或版本已变更"):
                        db.save_purchase_snapshot(target, [purchase_row()], datetime.now())
                    self.cursor.execute.assert_called_once()
                    self.assertIn("FOR UPDATE", self.cursor.execute.call_args.args[0])
                    self.cursor.executemany.assert_not_called()
                    self.connection.rollback.assert_called_once()
                    self.connection.commit.assert_not_called()
                else:
                    self.assertEqual(db.save_purchase_snapshot(target, [purchase_row()], datetime.now()), 8)
                    self.connection.commit.assert_called_once()
