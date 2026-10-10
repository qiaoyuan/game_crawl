import asyncio
import json
import sqlite3
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

from g2g import db
from g2g.crawl_filter import compile_policies, select_top3, stock_number, rating_number
from tools import crawl_from_db


def item(seller, price, **extra):
    return dict(seller_id=seller, seller_name=seller, price=price,
                currency="USD", stock="1K", rating="95.00", **extra)


def policies(dimension=None, currency="USD"):
    return compile_policies([{"config": json.dumps({"dimensions": [dimension or {}]}),
                              "currency": currency}])


class CrawlFilterTests(unittest.TestCase):
    def test_filters_before_top3_using_legacy_minimum_price(self):
        config = {"blacklist_stores": ["Ontheball"], "min_stock": 50,
                  "min_rating": 90, "minimum_price": 0.0004}
        rows = [item("Ontheball", "0.001"), item("floor", "0.0004"),
                item("low-rating", "0.001"), item("low-stock", "0.001"),
                item("d", "0.006"), item("b", "0.004"),
                item("a", "0.003"), item("c", "0.005")]
        rows[2]["rating"] = "89.99"
        rows[3]["stock"] = "49"
        selected = select_top3(rows, policies(config))
        self.assertEqual({r["seller_id"] for r in selected}, {"a", "b", "c"})

    def test_blacklist_wins_and_whitelist_only_bypasses_stock_and_rating(self):
        config = {"blacklist_stores": '["Both"]',
                  "whitelist_stores": "Both\nWhite\nFloor",
                  "min_stock": 50, "min_rating": 90, "filter_price": 1}
        rows = [item("both", 2), item(" White ", 2), item("floor", 1), item("other", 2)]
        for row in rows:
            row["stock"] = None
            row["rating"] = None
        self.assertEqual(select_top3(rows, policies(config)), [rows[1]])

    def test_normalizes_names_html_unicode_whitespace_and_nested_lists(self):
        row = item("123", 2)
        row["seller_name"] = "  ONTHEBALL\u00a0 &amp;\t Co  "
        self.assertEqual(select_top3([row], policies({
            "blacklist_stores": [["ontheball & co"]],
        })), [])

    def test_missing_invalid_stock_or_rating_and_abbreviations(self):
        for raw, expected in [("1 K", 1000), ("2.5M", 2500000),
                              ("1.2B", 1200000000), ("1,234", 1234), ("unknown", None)]:
            self.assertEqual(stock_number({"stock": raw}), expected)
        self.assertEqual(stock_number({"stock": "invalid", "stock_num": 60}), 60)
        self.assertEqual(rating_number("96.00 %"), 96)
        for rating in (None, "invalid", "NaN", "101", "-1"):
            self.assertIsNone(rating_number(rating))
        rows = [item("ok", 2), item("bad", 1)]
        rows[1]["rating"] = None
        self.assertEqual(select_top3(rows, policies({"min_rating": 90})), [rows[0]])

    def test_multiple_strategies_union_is_deduplicated_and_currency_scoped(self):
        rows = [item(str(i), i + 1) for i in range(7)]
        rows[6]["currency"] = "EUR"
        compiled = compile_policies([
            {"config": {"blacklist_stores": ["0", "1", "2"]}, "currency": "USD"},
            {"config": {}, "currency": "USD"},
            {"config": {}, "currency": "EUR"},
        ])
        self.assertEqual(select_top3(rows, compiled), rows)
        self.assertEqual(select_top3(rows, policies()), rows[:3])

    def test_threshold_alias_precedence_and_other_bid_fields_do_not_filter(self):
        rows = [item("a", "1.000001"), item("b", "1"), item("c", "0.5")]
        for field in ("filter_price", "price", "minimum_price", "floor_price"):
            self.assertEqual(select_top3(rows, policies({field: 1})), [rows[0]])
        self.assertEqual(select_top3(rows, policies({"filter_price": 0, "minimum_price": 1})), rows)
        self.assertEqual(select_top3(rows, policies({
            "amplitude": 100, "round_precision": 0, "ceiling_price": 0.1,
        })), rows)

    def test_ties_are_stable_invalid_prices_excluded_and_no_policy_saves_nothing(self):
        rows = [item(str(i), "2") for i in range(4)]
        rows.extend(item("invalid", price) for price in (0, -1, "NaN", None))
        self.assertEqual(select_top3(rows, policies()), rows[:3])
        self.assertEqual(select_top3(rows, []), [])

    def test_config_compatibility_and_invalid_config_rejected(self):
        rows = [item("a", 1), item("b", 2)]
        for config in ({"minimum_price": 1}, {"dimensions": '[{"filter_price":1}]'},
                       {"dimensions": ['{"filter_price":1}']}):
            self.assertEqual(select_top3(rows, compile_policies([{"config": config}])), [rows[1]])
        for config in ("invalid", "[]", {"dimensions": [{"type": "unknown"}]}):
            with self.assertRaises(ValueError):
                compile_policies([{"config": config}])

    def test_strategy_query_only_loads_active_target_and_bound_product_currencies(self):
        with sqlite3.connect(":memory:") as database:
            database.executescript("""
                CREATE TABLE price_strategy(id INTEGER, config TEXT, crawl_target_id INTEGER,
                                            status INTEGER, deleted_at TEXT);
                CREATE TABLE price_strategy_product(price_strategy_id INTEGER, game_product_id INTEGER);
                CREATE TABLE game_product(id INTEGER, currency TEXT, deleted_at TEXT);
                INSERT INTO price_strategy VALUES
                  (1,'{}',10,1,NULL),(2,'{}',10,0,NULL),(3,'{}',10,1,'deleted'),(4,'{}',11,1,NULL);
                INSERT INTO game_product VALUES (1,'USD',NULL),(2,'USD',NULL),(3,'EUR',NULL),(4,'JPY','deleted');
                INSERT INTO price_strategy_product VALUES
                  (1,1),(1,2),(1,3),(1,4),(2,1),(3,1),(4,1);
            """)
            connection = MagicMock()
            cursor = connection.cursor.return_value.__enter__.return_value
            with patch.object(db, "get_connection", return_value=connection):
                db.get_crawl_strategies(10)
            sql, params = cursor.execute.call_args.args
            self.assertCountEqual(database.execute(sql.replace("%s", "?"), params).fetchall(),
                                  [(1, "{}", "EUR"), (1, "{}", "USD")])
            connection.close.assert_called_once()

    def test_run_saves_filtered_items_and_notification_count_default_is_unchanged(self):
        rows = [item(str(i), i + 1) for i in range(6)]
        for crawl_type, strategies, expected in [
            (0, [], rows), ("0", [], rows), (None, [], rows),
            (1, [{"config": {}, "currency": "USD"}], rows[:3]),
            ("1", [{"config": {}, "currency": "USD"}], rows[:3]),
            (1, [], None), (1, [{"config": "invalid"}], None),
            (1, [{"config": {"min_rating": 100}}], []),
            (2, [], None),
            ("default", [], rows), ("top3", [{"config": {}, "currency": "USD"}], rows[:3]),
            ("top3", [], None), ("top3", [{"config": "invalid"}], None),
            ("top3", [{"config": {"min_rating": 100}}], []),
        ]:
            with self.subTest(crawl_type=crawl_type, strategies=strategies):
                browser, context, playwright = MagicMock(), MagicMock(), MagicMock()
                browser.new_context = AsyncMock(return_value=context)
                browser.close = AsyncMock()
                context.add_cookies = AsyncMock()
                context.add_init_script = AsyncMock()
                context.new_page = AsyncMock()
                playwright.chromium.launch = AsyncMock(return_value=browser)
                manager = MagicMock()
                manager.__aenter__ = AsyncMock(return_value=playwright)
                manager.__aexit__ = AsyncMock()
                target = {"id": 10, "game_product_id": 1, "url": "https://example.com",
                          "category": "游戏币", "crawl_type": crawl_type}
                with patch.object(db, "get_pending_targets", return_value=[target]), \
                     patch.object(db, "get_crawl_strategies", return_value=strategies) as load, \
                     patch.object(db, "increment_version", return_value=8) as increment, \
                     patch.object(db, "save_crawl_data", return_value=len(expected or [])) as save, \
                     patch.object(db, "update_last_crawl") as update, \
                     patch.object(db, "insert_crawl_notify") as notify, \
                     patch.object(crawl_from_db, "async_playwright", return_value=manager), \
                     patch.object(crawl_from_db, "scrape_other_offer_page", new=AsyncMock(return_value=rows)) as scrape:
                    asyncio.run(crawl_from_db.run())
                    if crawl_type in (0, "0", None, "default"):
                        load.assert_not_called()
                    if expected is None:
                        save.assert_not_called()
                        increment.assert_not_called()
                        notify.assert_not_called()
                        update.assert_not_called()
                        scrape.assert_not_called()
                    else:
                        save.assert_called_once_with(10, "g2g", expected, game_product_id=1, version=8)
                        notify.assert_called_once_with(10, 8, len(expected))
                        update.assert_called_once_with(10)


if __name__ == "__main__":
    unittest.main()
