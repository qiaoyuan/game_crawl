import asyncio
import contextlib
import io
import unittest
from datetime import datetime
from unittest.mock import AsyncMock, MagicMock, patch

from g2g import db
from tools import crawl_from_db


class CrawlIntervalTests(unittest.TestCase):
    def setUp(self):
        self.base = datetime(2026, 10, 12, 12).timestamp()
        self.now = self.base
        self.context, self.browser, playwright = MagicMock(), MagicMock(), MagicMock()
        self.context.add_cookies = AsyncMock()
        self.context.add_init_script = AsyncMock()
        self.context.new_page = AsyncMock()
        self.context.close = AsyncMock()
        self.browser.new_context = AsyncMock(return_value=self.context)
        self.browser.close = AsyncMock()
        playwright.chromium.launch = AsyncMock(return_value=self.browser)
        self.request = MagicMock()
        self.request.dispose = AsyncMock()
        playwright.request.new_context = AsyncMock(return_value=self.request)
        self.manager = MagicMock()
        self.manager.__aenter__ = AsyncMock(return_value=playwright)
        self.manager.__aexit__ = AsyncMock()

    async def advance(self, seconds):
        self.now += seconds

    def scheduled_target(self, target_id, due_in=None, category="ELD游戏币"):
        target = {"id": target_id, "name": f"target {target_id}", "game_product_id": 1,
                  "category": category,
                  "url": f"https://{'www.g2g.com' if category == '物品' else 'www.eldorado.gg'}/{target_id}"}
        if due_in is not None:
            target.update(crawl_interval=60,
                          last_crawl_at=datetime.fromtimestamp(self.base + due_in - 60))
        return target

    def run_with_clock(self, targets, durations):
        starts, stored = [], {}
        output = io.StringIO()

        async def scrape(request_or_page, url):
            target_id = int(url.rsplit("/", 1)[1])
            starts.append((target_id, round(self.now - self.base, 3)))
            self.now += durations[target_id]
            return []

        with contextlib.ExitStack() as stack:
            stack.enter_context(contextlib.redirect_stdout(output))
            stack.enter_context(patch.object(crawl_from_db, "async_playwright", return_value=self.manager))
            stack.enter_context(patch.object(crawl_from_db.time, "time", side_effect=lambda: self.now))
            sleep = stack.enter_context(patch.object(crawl_from_db.asyncio, "sleep", side_effect=self.advance))
            stack.enter_context(patch.object(db, "get_pending_targets", return_value=targets))
            increment = stack.enter_context(patch.object(db, "increment_version", return_value=8))
            save = stack.enter_context(patch.object(db, "save_crawl_data", return_value=0))
            stack.enter_context(patch.object(db, "update_last_crawl", side_effect=stored.__setitem__))
            notify = stack.enter_context(patch.object(db, "insert_crawl_notify"))
            stack.enter_context(patch.object(crawl_from_db, "scrape_eldorado_api", side_effect=scrape))
            stack.enter_context(patch.object(crawl_from_db, "scrape_page", side_effect=scrape))
            self.assertEqual(asyncio.run(crawl_from_db.run()), 0)
        order = [target_id for target_id, _ in starts]
        self.assertEqual([call.args[0] for call in increment.call_args_list], order)
        self.assertEqual([call.args[0] for call in save.call_args_list], order)
        self.assertEqual([call.args[0] for call in notify.call_args_list], order)
        for target_id, started in starts:
            self.assertEqual(stored[target_id], datetime.fromtimestamp(self.base + started).replace(microsecond=0))
        return starts, [call.args[0] for call in sleep.await_args_list], output.getvalue()

    def test_ready_target_after_waiting_targets_runs_before_any_interval_sleep(self):
        targets = [self.scheduled_target(18, category="物品"),
                   self.scheduled_target(24, 55), self.scheduled_target(26, 60),
                   self.scheduled_target(40)]
        starts, sleeps, output = self.run_with_clock(targets, {18: 30, 24: .5, 26: .5, 40: .5})
        self.assertEqual(starts, [(18, 0), (40, 30), (24, 55), (26, 60)])
        self.assertEqual(sleeps, [24.5, 4.5])
        self.assertIn("[4/4] target 40 调度: 优先执行已到期任务", output)
        self.assertIn("当前无已到期任务", output)

    def test_waits_for_nearest_deadline_not_lowest_id_and_skips_distant_target(self):
        targets = [self.scheduled_target(10, 20), self.scheduled_target(12, 5),
                   self.scheduled_target(14, 5), self.scheduled_target(16, 61)]
        starts, sleeps, output = self.run_with_clock(targets, {10: .5, 12: .5, 14: .5})
        self.assertEqual(starts, [(12, 5), (14, 5.5), (10, 20)])
        self.assertEqual(sleeps, [5, 14])
        self.assertIn("[4/4] target 16 跳过: 未到间隔时间", output)

    def test_rechecks_due_time_after_other_target_and_does_not_retry_invalid_target(self):
        invalid = dict(self.scheduled_target(18, 1), game_product_id=0)
        targets = [invalid, self.scheduled_target(20, 5), self.scheduled_target(22),
                   self.scheduled_target(24, 50)]
        starts, sleeps, output = self.run_with_clock(targets, {20: .5, 22: 8})
        self.assertEqual(starts, [(22, 0), (20, 8)])
        self.assertEqual(sleeps, [])
        self.assertIn("[1/4] target 18 跳过: 未关联游戏产品", output)

    def test_scheduler_rechecks_deadline_after_early_wake_and_never_fetches_early(self):
        target = self.scheduled_target(24, 5)
        starts, waits = [], []

        async def wake_early(seconds):
            waits.append(seconds)
            self.now += 1 if len(waits) == 1 else seconds

        async def consume():
            async for _, queued in crawl_from_db.iter_scheduled_targets([(0, target)], 1):
                starts.append((queued["id"], self.now - self.base))

        with patch.object(crawl_from_db.time, "time", side_effect=lambda: self.now), \
             patch.object(crawl_from_db.asyncio, "sleep", side_effect=wake_early), \
             contextlib.redirect_stdout(io.StringIO()):
            asyncio.run(consume())
        self.assertEqual(waits, [5, 4])
        self.assertEqual(starts, [(24, 5)])

    def test_scheduler_thirty_second_boundary_and_invalid_historical_timestamp(self):
        targets = [self.scheduled_target(20, 30), self.scheduled_target(22, 61),
                   dict(self.scheduled_target(24), crawl_interval=60, last_crawl_at="invalid")]
        starts, sleeps, output = self.run_with_clock(targets, {20: 0, 24: 0})
        self.assertEqual(starts, [(24, 0), (20, 30)])
        self.assertEqual(sleeps, [30])
        self.assertEqual(output.count("间隔时间解析异常"), 1)
        self.assertIn("target 22 跳过: 未到间隔时间，还需等待约 31 秒", output)

    def test_eld_targets_run_each_round_despite_fetch_time_and_early_cron(self):
        targets = [{"id": target_id, "game_product_id": 1, "category": "ELD游戏币",
                    "url": f"https://www.eldorado.gg/{target_id}", "crawl_interval": 30}
                   for target_id in (24, 26)]
        stored = {}
        starts = []
        durations = iter((3.3, 2.7, 1, 2, 4, 2))
        rows = [{"seller_id": str(index), "price": "1", "currency": "USD"}
                for index in range(8)]

        async def scrape(page, url):
            starts.append((int(url.rsplit("/", 1)[1]), round(self.now - self.base, 3)))
            self.now += next(durations)
            return rows

        def pending(*args):
            return [dict(target, last_crawl_at=stored.get(target["id"])) for target in targets]

        with contextlib.ExitStack() as stack:
            stack.enter_context(contextlib.redirect_stdout(io.StringIO()))
            stack.enter_context(patch.object(crawl_from_db, "async_playwright", return_value=self.manager))
            stack.enter_context(patch.object(crawl_from_db.time, "time", side_effect=lambda: self.now))
            sleep = stack.enter_context(patch.object(crawl_from_db.asyncio, "sleep", side_effect=self.advance))
            stack.enter_context(patch.object(db, "get_pending_targets", side_effect=pending))
            stack.enter_context(patch.object(db, "increment_version", return_value=8))
            save = stack.enter_context(patch.object(db, "save_crawl_data", return_value=8))
            update = stack.enter_context(patch.object(db, "update_last_crawl", side_effect=stored.__setitem__))
            notify = stack.enter_context(patch.object(db, "insert_crawl_notify"))
            stack.enter_context(patch.object(crawl_from_db, "scrape_eldorado_api", side_effect=scrape))

            for round_start in (0, 29, 60):
                self.now = self.base + round_start
                self.assertEqual(asyncio.run(crawl_from_db.run(0, 2)), 0)
                if round_start == 0:
                    # Persist the start of each successful fetch, not its completion.
                    self.assertEqual(stored, {24: datetime.fromtimestamp(self.base),
                                              26: datetime.fromtimestamp(self.base + 3)})

            self.assertEqual(starts, [(24, 0), (26, 3.3), (24, 30), (26, 33), (24, 60), (26, 64)])
            self.assertEqual(sleep.await_args_list[0].args, (1,))
            self.assertEqual(sleep.await_args_list[1].args, (2,))
            self.assertEqual(sleep.await_count, 2)
            self.assertEqual(save.call_count, 6)
            self.assertEqual(update.call_count, 6)
            self.assertEqual(notify.call_count, 6)
            for call in notify.call_args_list:
                self.assertEqual(call.args[1:], (8, 8))

    def test_interval_zero_first_run_due_and_longer_wait(self):
        cases = [
            (0, datetime.fromtimestamp(self.base + 100), True, None),
            (30, None, True, None),
            (30, datetime.fromtimestamp(self.base - 30), True, None),
            (30, datetime.fromtimestamp(self.base - 27).strftime("%Y-%m-%d %H:%M:%S"), True, 3),
            (60, datetime.fromtimestamp(self.base - 30), True, 30),
            (60, datetime.fromtimestamp(self.base - 29), False, None),
        ]
        for interval, last_crawl_at, expected, wait in cases:
            with self.subTest(interval=interval, last_crawl_at=last_crawl_at):
                self.now = self.base
                with patch.object(crawl_from_db.time, "time", side_effect=lambda: self.now), \
                     patch.object(crawl_from_db.asyncio, "sleep", side_effect=self.advance) as sleep, \
                     contextlib.redirect_stdout(io.StringIO()):
                    result = asyncio.run(crawl_from_db.wait_for_crawl_interval(
                        {"crawl_interval": interval, "last_crawl_at": last_crawl_at}, "target 24"
                    ))
                    self.assertEqual(result, expected)
                    if wait is None:
                        sleep.assert_not_awaited()
                    else:
                        sleep.assert_awaited_once_with(wait)
                        self.assertEqual(self.now - self.base, wait)

    def test_failed_target_does_not_advance_interval_or_send_notification(self):
        target = {"id": 24, "game_product_id": 1, "category": "ELD游戏币",
                  "url": "https://www.eldorado.gg/24", "crawl_interval": 30,
                  "last_crawl_at": datetime.fromtimestamp(self.base - 30)}
        with patch.object(crawl_from_db, "async_playwright", return_value=self.manager), \
             patch.object(crawl_from_db.time, "time", return_value=self.base), \
             patch.object(db, "get_pending_targets", return_value=[target]), \
             patch.object(db, "increment_version", return_value=8), \
             patch.object(db, "save_crawl_data") as save, \
             patch.object(db, "update_last_crawl") as update, \
             patch.object(db, "insert_crawl_notify") as notify, \
             patch.object(crawl_from_db, "scrape_eldorado_api", side_effect=RuntimeError("fetch failed")), \
             contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(asyncio.run(crawl_from_db.run()), 1)
            save.assert_not_called()
            update.assert_not_called()
            notify.assert_not_called()
            self.request.dispose.assert_awaited_once()
            self.browser.close.assert_not_awaited()

    def test_database_writes_provided_start_timestamp(self):
        connection = MagicMock()
        started_at = datetime.fromtimestamp(self.base)
        with patch.object(db, "get_connection", return_value=connection):
            db.update_last_crawl(24, started_at)
        cursor = connection.cursor.return_value.__enter__.return_value
        sql, params = cursor.execute.call_args.args
        self.assertNotIn("NOW()", sql)
        self.assertEqual(params, (started_at, 24))
        connection.commit.assert_called_once()
        connection.close.assert_called_once()


if __name__ == "__main__":
    unittest.main()
