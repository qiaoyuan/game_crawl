import contextlib
import io
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

from g2g import config, db
from tools import crawl_from_db


class ExcludedStoreTests(unittest.IsolatedAsyncioTestCase):
    def test_matches_multiple_names_and_ids_without_matching_substrings(self):
        rows = [
            {"seller_id": "vivi_store", "seller_name": "Alias"},
            {"seller_id": "123", "seller_name": "Other\u00a0 &amp;\t Own"},
            {"seller_id": "Vivi_Store_Backup", "seller_name": "Other competitor"},
            {"seller_id": None, "seller_name": None},
        ]
        for value in ("Vivi_Store， Other & Own", '["Vivi_Store", "Other & Own"]'):
            with self.subTest(value=value), patch.object(config, "CRAWL_EXCLUDE_STORES", value):
                self.assertEqual(crawl_from_db.filter_excluded_stores(rows), rows[2:])
        with patch.object(config, "CRAWL_EXCLUDE_STORES", ""):
            self.assertIs(crawl_from_db.filter_excluded_stores(rows), rows)

    async def test_excluded_g2g_rows_are_not_parsed_enhanced_saved_or_notified(self):
        for crawl_type, stores in ((0, "Vivi_Store"), (1, "Vivi_Store,competitor"), (1, "")):
            with self.subTest(crawl_type=crawl_type, stores=stores):
                page = MagicMock()
                page.goto = AsyncMock()
                page.query_selector_all = AsyncMock(return_value=[None] * 10)
                # These invalid prices/ratings would fail parsing if own shops were not skipped first.
                page.evaluate = AsyncMock(return_value=[
                    {"seller_id": "vivi_store", "unit_price_raw": ["invalid"], "rating": ["invalid"]},
                    {"seller_id": "123", "seller_name": "Other Own", "unit_price_raw": ["invalid"]},
                    {"seller_id": "competitor", "unit_price_raw": "2", "currency_label": "USD"},
                ])
                browser, context, playwright = MagicMock(), MagicMock(), MagicMock()
                browser.new_context = AsyncMock(return_value=context)
                browser.close = AsyncMock()
                context.close = AsyncMock()
                context.add_cookies = AsyncMock()
                context.add_init_script = AsyncMock()
                context.new_page = AsyncMock(return_value=page)
                playwright.chromium.launch = AsyncMock(return_value=browser)
                manager = MagicMock()
                manager.__aenter__ = AsyncMock(return_value=playwright)
                manager.__aexit__ = AsyncMock()
                target = {"id": 10, "game_product_id": 1, "url": "https://example.com",
                          "category": "游戏币", "crawl_type": crawl_type, "enhance_stores": stores}
                with contextlib.ExitStack() as stack:
                    stack.enter_context(contextlib.redirect_stdout(io.StringIO()))
                    stack.enter_context(patch.object(config, "CRAWL_EXCLUDE_STORES", "Vivi_Store,Other Own"))
                    stack.enter_context(patch.object(config, "CRAWL_OFFER_LIMIT", 10))
                    stack.enter_context(patch.object(crawl_from_db, "async_playwright", return_value=manager))
                    stack.enter_context(patch.object(db, "get_pending_targets", return_value=[target]))
                    stack.enter_context(patch.object(db, "get_crawl_strategies", return_value=[{"config": {}, "currency": "USD"}]))
                    stack.enter_context(patch.object(db, "increment_version", return_value=8))
                    save = stack.enter_context(patch.object(db, "save_crawl_data", side_effect=lambda *args, **kwargs: len(args[2])))
                    stack.enter_context(patch.object(db, "update_last_crawl"))
                    notify = stack.enter_context(patch.object(db, "insert_crawl_notify"))
                    detail = stack.enter_context(patch.object(crawl_from_db, "_refresh_other_offer_price", new=AsyncMock()))

                    self.assertEqual(await crawl_from_db.run(), 0)
                    self.assertEqual([row["seller_id"] for row in save.call_args.args[2]], ["competitor"])
                    notify.assert_called_once_with(10, 8, 1)
                    if crawl_type == 0:
                        detail.assert_not_awaited()
                    else:
                        detail.assert_awaited_once()
                        self.assertEqual(detail.await_args.args[1]["seller_id"], "competitor")


if __name__ == "__main__":
    unittest.main()
