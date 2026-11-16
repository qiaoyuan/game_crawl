import contextlib
import io
import unittest
from datetime import datetime
from decimal import Decimal
from unittest.mock import AsyncMock, MagicMock, patch

from g2g import db
from tools import crawl_from_db


def purchase_target():
    return {"id": 8, "game_product_id": 13, "task_type": "1", "version": 7,
            "url": "https://www.dd373.com/s-example.html",
            "category": "游戏币", "crawl_type": 1}


def purchase_row():
    return {"deal_rate": Decimal("95.9"), "region": "示例区服", "stock": 6000,
            "sales_unit": "1万基纳", "line_quantity": 1000,
            "line_stock_amount": Decimal("173.40"), "line_total": Decimal("28.90"),
            "price": Decimal("0.0289"), "currency": "CNY"}


class PurchaseRouteTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        stack = contextlib.ExitStack()
        self.addCleanup(stack.close)
        stack.enter_context(contextlib.redirect_stdout(io.StringIO()))
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
        target = purchase_target()
        self.source.return_value = [purchase_row()]
        self.database.save_purchase_snapshot.return_value = 8
        saved, failed, wall, _ = await crawl_from_db.crawl_target(self.session, 0, target, 1)
        self.assertEqual((saved, failed, target["version"]), (1, False, 8))
        self.source.assert_awaited_once_with(target["url"], route_cache=self.session.dd373_routes)
        self.database.save_purchase_snapshot.assert_called_once_with(
            target, self.source.return_value, datetime.fromtimestamp(wall).replace(microsecond=0),
        )

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
        for task_type in ("2", "unknown", 0):
            target = dict(purchase_target(), task_type=task_type)
            with self.subTest(task_type=task_type):
                result = await crawl_from_db.crawl_target(self.session, 0, target, 1)
                self.assertEqual(result, (0, True, None, None))
        self.source.assert_not_awaited()
        self.database.save_purchase_snapshot.assert_not_called()


class PurchaseTransactionTests(unittest.TestCase):
    def setUp(self):
        self.connection = MagicMock()
        self.cursor = self.connection.cursor.return_value.__enter__.return_value
        self.cursor.rowcount = 1
        connection_patch = patch.object(db, "get_connection", return_value=self.connection)
        connection_patch.start()
        self.addCleanup(connection_patch.stop)

    def test_confirmed_empty_snapshot_updates_version_without_inserting_rows(self):
        version = db.save_purchase_snapshot(purchase_target(), [], datetime.now())
        self.assertEqual(version, 8)
        self.cursor.execute.assert_called_once()
        self.cursor.executemany.assert_not_called()
        self.connection.commit.assert_called_once()

    def test_batch_uses_one_version_and_preserves_rmb_precision(self):
        version = db.save_purchase_snapshot(purchase_target(), [purchase_row()], datetime.now())
        self.assertEqual(version, 8)
        sql, rows = self.cursor.executemany.call_args.args
        columns = sql.split("(", 1)[1].split(")", 1)[0].replace(" ", "").replace("\n", "").split(",")
        self.assertEqual(dict(zip(columns, rows[0], strict=True)), {
            **purchase_row(), "target_id": 8, "game_product_id": 13, "version": 8,
            "seller_name": "", "offer_url": "", "crawled_at": rows[0][-1],
        })
        self.connection.commit.assert_called_once()
        self.connection.close.assert_called_once()

    def test_conflicting_or_failed_write_is_rolled_back(self):
        for conflict in (True, False):
            with self.subTest(conflict=conflict):
                self.connection.reset_mock()
                self.cursor.rowcount = 0 if conflict else 1
                self.cursor.executemany.side_effect = RuntimeError("插入失败")
                with self.assertRaises(RuntimeError):
                    db.save_purchase_snapshot(purchase_target(), [purchase_row()], datetime.now())
                if conflict:
                    self.cursor.executemany.assert_not_called()
                self.connection.rollback.assert_called_once()
                self.connection.commit.assert_not_called()
                self.connection.close.assert_called_once()
