import asyncio
import contextlib
import io
import unittest
from datetime import datetime
from unittest.mock import AsyncMock, MagicMock, patch

from g2g import db
from tools import crawl_from_db


class FakeClock:
    def __init__(self):
        self.base = datetime(2026, 10, 12, 12).timestamp()
        self.elapsed = 0.0
        self.wall_offset = 0.0
        self.sleeps = []

    def wall(self):
        return self.base + self.elapsed + self.wall_offset

    async def sleep(self, seconds):
        if seconds < 0:
            raise AssertionError("negative sleep")
        self.sleeps.append(seconds)
        self.elapsed += seconds


class CrawlResidentTests(unittest.TestCase):
    def setUp(self):
        self.clock = FakeClock()
        self.starts = []

    def target(self, target_id, interval=0, last_started=None):
        target = {
            "id": target_id, "name": f"target {target_id}",
            "game_product_id": 1, "category": "ELD游戏币",
            "url": f"https://www.eldorado.gg/{target_id}",
            "crawl_interval": interval,
        }
        if last_started is not None:
            target["last_crawl_at"] = datetime.fromtimestamp(self.clock.base + last_started)
        return target

    @contextlib.contextmanager
    def environment(self, pending, fetch=None):
        with contextlib.ExitStack() as stack:
            stack.enter_context(contextlib.redirect_stdout(io.StringIO()))
            stack.enter_context(patch.object(crawl_from_db, "monotonic",
                                            side_effect=lambda: self.clock.elapsed))
            stack.enter_context(patch.object(crawl_from_db.time, "time", side_effect=self.clock.wall))
            stack.enter_context(patch.object(crawl_from_db.asyncio, "sleep", side_effect=self.clock.sleep))
            query = stack.enter_context(patch.object(db, "get_pending_targets", side_effect=pending))
            if fetch is not None:
                stack.enter_context(patch.object(crawl_from_db, "crawl_target", side_effect=fetch))
            yield query

    def fetcher(self, durations=None, failures=None):
        durations, failures = durations or {}, failures or set()

        async def fetch(session, idx, target, total):
            target_id = target["id"]
            started_mono, started_wall = self.clock.elapsed, self.clock.wall()
            self.starts.append((target_id, round(started_mono, 3)))
            self.clock.elapsed += durations.get(target_id, 0.5)
            if target_id in failures:
                return 0, True, None, None
            return 1, False, started_wall, started_mono

        return fetch

    def test_overdue_slow_target_does_not_starve_other_targets(self):
        targets = [self.target(18), self.target(24), self.target(26)]
        with self.environment(lambda *_: targets, self.fetcher({18: 35, 24: 1, 26: 1})):
            self.assertEqual(asyncio.run(crawl_from_db.run_scheduled(0, 2, 100)), 0)
        self.assertEqual(self.starts, [(18, 0), (24, 35), (26, 36),
                                      (18, 37), (24, 72), (26, 73), (18, 74)])
        # The last target may finish beyond the window; no new target starts then.
        self.assertEqual(self.clock.elapsed, 109)

    def test_zero_interval_repeats_every_thirty_seconds_despite_stale_refresh(self):
        targets = [self.target(25)]  # The fake DB intentionally never reflects writes.
        with self.environment(lambda *_: targets, self.fetcher()) as query:
            self.assertEqual(asyncio.run(crawl_from_db.run_scheduled(1, 2, 95)), 0)
        self.assertEqual(self.starts, [(25, 0), (25, 30), (25, 60), (25, 90)])
        self.assertGreater(query.call_count, 4)
        self.assertEqual(query.call_args.args, (1, 2))
        self.assertEqual(self.clock.elapsed, 95)

    def test_new_window_uses_stored_start_for_zero_and_positive_intervals(self):
        targets = [self.target(24, 0, last_started=-10),
                   self.target(26, 60, last_started=-10)]
        with self.environment(lambda *_: targets, self.fetcher()):
            self.assertEqual(asyncio.run(crawl_from_db.run_scheduled(0, 2, 55)), 0)
        self.assertEqual(self.starts, [(24, 20), (24, 50), (26, 50.5)])

    def test_positive_interval_keeps_start_to_start_semantics(self):
        targets = [self.target(24, 60)]
        with self.environment(lambda *_: targets, self.fetcher({24: 7})):
            self.assertEqual(asyncio.run(crawl_from_db.run_scheduled(0, 2, 125)), 0)
        self.assertEqual(self.starts, [(24, 0), (24, 60), (24, 120)])
        self.assertEqual(self.clock.elapsed, 127)

    def test_failure_uses_completion_backoff_and_refresh_does_not_erase_it(self):
        targets = [self.target(24), self.target(26)]
        with self.environment(lambda *_: targets,
                              self.fetcher({24: 7, 26: 1}, failures={24})):
            self.assertEqual(asyncio.run(crawl_from_db.run_scheduled(0, 2, 80)), 1)
        self.assertEqual([start for start in self.starts if start[0] == 24],
                         [(24, 0), (24, 37), (24, 74)])
        self.assertEqual([start for start in self.starts if start[0] == 26],
                         [(26, 7), (26, 44)])

    def test_new_disabled_reassigned_and_changed_interval_targets_refresh(self):
        initial = self.target(24, 60)
        added = self.target(26)

        def pending(*_):
            now = self.clock.elapsed
            if now < 10:
                return [initial]
            if now < 20:
                return [initial, added]
            if now < 40:
                # A DB-query-filtered disabled/deleted/reassigned row disappears.
                return [dict(initial, crawl_interval=30)]
            return [dict(initial, crawl_interval=60)]

        with self.environment(pending, self.fetcher()):
            self.assertEqual(asyncio.run(crawl_from_db.run_scheduled(0, 2, 95)), 0)
        self.assertEqual(self.starts, [(24, 0), (26, 10), (24, 30), (24, 90)])

    def test_all_invalid_or_empty_targets_wait_without_starting_driver(self):
        invalid = [dict(self.target(24), game_product_id=0), dict(self.target(26), url="")]
        with self.environment(lambda *_: invalid if self.clock.elapsed < 5 else []):
            with patch.object(crawl_from_db, "async_playwright") as driver, \
                 patch.object(crawl_from_db, "crawl_target", new_callable=AsyncMock) as fetch:
                self.assertEqual(asyncio.run(crawl_from_db.run_scheduled(0, 2, 12)), 0)
        driver.assert_not_called()
        fetch.assert_not_awaited()
        self.assertEqual(self.clock.sleeps, [5, 5, 2])

    def test_no_new_target_at_window_boundary_and_inflight_target_completes(self):
        with self.environment(lambda *_: [self.target(24)], self.fetcher({24: 12})):
            self.assertEqual(asyncio.run(crawl_from_db.run_scheduled(0, 1, 10)), 0)
        self.assertEqual(self.starts, [(24, 0)])
        self.assertEqual(self.clock.elapsed, 12)

        self.clock.elapsed, self.starts = 0, []
        with self.environment(lambda *_: [self.target(24)], self.fetcher({24: 1})):
            self.assertEqual(asyncio.run(crawl_from_db.run_scheduled(0, 1, 30)), 0)
        self.assertEqual(self.starts, [(24, 0)])
        self.assertEqual(self.clock.elapsed, 30)

    def test_zero_window_delegates_to_oneshot_and_cli_defaults_to_oneshot(self):
        with patch.object(crawl_from_db, "run", new_callable=AsyncMock, return_value=7) as once:
            self.assertEqual(asyncio.run(crawl_from_db.run_scheduled(1, 2, 0)), 7)
        once.assert_awaited_once_with(1, 2)
        self.assertFalse(crawl_from_db.parse_args([]).scheduled)
        self.assertTrue(crawl_from_db.parse_args(["--scheduled"]).scheduled)

    def test_early_wakeup_rechecks_due_time(self):
        normal_sleep = self.clock.sleep
        early_wakes = []

        async def wake_early(seconds):
            if not early_wakes:
                early_wakes.append(seconds)
                await normal_sleep(1)
            else:
                await normal_sleep(seconds)

        self.clock.sleep = wake_early
        with self.environment(lambda *_: [self.target(24, last_started=0)], self.fetcher()):
            self.assertEqual(asyncio.run(crawl_from_db.run_scheduled(0, 1, 31)), 0)
        self.assertEqual(early_wakes, [5])
        self.assertEqual(self.starts, [(24, 30)])

    def test_forward_wall_clock_change_does_not_shorten_local_start_interval(self):
        fetch = self.fetcher()

        async def adjust_clock(*args):
            result = await fetch(*args)
            self.clock.wall_offset += 1000
            return result

        def pending(*_):
            return [self.target(24, last_started=0 if self.starts else None)]

        with self.environment(pending, adjust_clock):
            self.assertEqual(asyncio.run(crawl_from_db.run_scheduled(0, 1, 65)), 0)
        self.assertEqual(self.starts, [(24, 0), (24, 30), (24, 60)])

    def test_backward_wall_clock_change_keeps_local_thirty_second_schedule(self):
        fetch = self.fetcher()
        stored_wall = None

        async def adjust_clock(*args):
            nonlocal stored_wall
            result = await fetch(*args)
            stored_wall = result[2]
            if len(self.starts) == 1:
                self.clock.wall_offset = -1000
            return result

        def pending(*_):
            target = self.target(24)
            if stored_wall is not None:
                target["last_crawl_at"] = datetime.fromtimestamp(stored_wall).replace(microsecond=0)
            return [target]

        with self.environment(pending, adjust_clock):
            self.assertEqual(asyncio.run(crawl_from_db.run_scheduled(0, 1, 65)), 0)
        self.assertEqual(self.starts, [(24, 0), (24, 30), (24, 60)])

    def test_newer_external_success_delays_local_next_execution(self):
        fetch = self.fetcher()
        stored_wall = None
        external_update_applied = False

        async def persist_start(*args):
            nonlocal stored_wall
            result = await fetch(*args)
            stored_wall = result[2]
            return result

        def pending(*_):
            nonlocal stored_wall, external_update_applied
            if self.clock.elapsed >= 10 and not external_update_applied:
                # A later DB success must supersede this worker's previous start.
                stored_wall = self.clock.base + 10
                external_update_applied = True
            target = self.target(24)
            if stored_wall is not None:
                target["last_crawl_at"] = datetime.fromtimestamp(stored_wall).replace(microsecond=0)
            return [target]

        with self.environment(pending, persist_start):
            self.assertEqual(asyncio.run(crawl_from_db.run_scheduled(0, 1, 65)), 0)
        self.assertTrue(external_update_applied)
        self.assertEqual(self.starts, [(24, 0), (24, 40)])

    def test_fast_worker_repeats_independently_with_one_session_and_cleanup_after_query_failures(self):
        session = MagicMock()
        session.__aenter__ = AsyncMock(return_value=session)
        session.__aexit__ = AsyncMock(return_value=False)
        seen_sessions = []
        fetch = self.fetcher({25: 1})

        async def record_session(current, *args):
            seen_sessions.append(current)
            return await fetch(current, *args)

        with self.environment(lambda index, count: [self.target(25)], record_session):
            with patch.object(crawl_from_db, "CrawlSession", return_value=session) as factory:
                self.assertEqual(asyncio.run(crawl_from_db.run_scheduled(1, 2, 65)), 0)
        self.assertEqual(self.starts, [(25, 0), (25, 30), (25, 60)])
        self.assertEqual(seen_sessions, [session] * 3)
        factory.assert_called_once_with()
        session.__aexit__.assert_awaited_once()

        session.__aexit__.reset_mock()
        with self.environment(lambda *_: (_ for _ in ()).throw(RuntimeError("DB unavailable"))) as query:
            with patch.object(crawl_from_db, "CrawlSession", return_value=session):
                self.assertEqual(asyncio.run(crawl_from_db.run_scheduled(1, 2, 65)), 1)
        self.assertEqual(query.call_count, 3)
        session.__aexit__.assert_awaited_once()

    def test_midwindow_query_failure_pauses_stale_snapshot_then_recovers_and_closes(self):
        queries = []
        session = MagicMock()
        session.__aenter__ = AsyncMock(return_value=session)
        session.__aexit__ = AsyncMock(return_value=False)

        def pending(*_):
            queries.append(self.clock.elapsed)
            if len(queries) == 2:
                raise RuntimeError("temporary DB failure")
            return [self.target(24)]

        with self.environment(pending, self.fetcher()):
            with patch.object(crawl_from_db, "CrawlSession", return_value=session):
                self.assertEqual(asyncio.run(crawl_from_db.run_scheduled(0, 2, 70)), 1)
        self.assertEqual(queries[:3], [0, 5, 35])
        self.assertEqual(self.starts, [(24, 0), (24, 35), (24, 65)])
        self.assertEqual(self.clock.elapsed, 70)
        session.__aexit__.assert_awaited_once()

    def test_continuous_query_failure_backs_off_until_window_end_without_driver(self):
        queries = []

        def pending(*_):
            queries.append(self.clock.elapsed)
            raise RuntimeError("DB unavailable")

        with self.environment(pending):
            with patch.object(crawl_from_db, "async_playwright") as driver, \
                 patch.object(crawl_from_db, "crawl_target", new_callable=AsyncMock) as fetch:
                self.assertEqual(asyncio.run(crawl_from_db.run_scheduled(0, 2, 65)), 1)
        self.assertEqual(queries, [0, 30, 60])
        self.assertEqual(self.clock.sleeps, [30, 30, 5])
        self.assertEqual(self.clock.elapsed, 65)
        driver.assert_not_called()
        fetch.assert_not_awaited()

    def test_query_recovery_obeys_disabled_or_reassigned_targets_and_new_rows(self):
        queries = []

        def pending(*_):
            queries.append(self.clock.elapsed)
            if len(queries) == 1:
                return [self.target(24)]
            if len(queries) == 2:
                raise RuntimeError("temporary DB failure")
            # Target 24 is no longer in this shard after recovery.
            return [self.target(26)]

        with self.environment(pending, self.fetcher()):
            self.assertEqual(asyncio.run(crawl_from_db.run_scheduled(0, 2, 70)), 1)
        self.assertEqual(queries[:3], [0, 5, 35])
        self.assertEqual(self.starts, [(24, 0), (26, 35), (26, 65)])

    def test_page_session_reuses_browser_and_closes_contexts_after_each_target(self):
        playwright, manager, browser = MagicMock(), MagicMock(), MagicMock()
        contexts = [MagicMock() for _ in range(3)]
        for context in contexts:
            context.add_cookies = AsyncMock()
            context.add_init_script = AsyncMock()
            context.new_page = AsyncMock(return_value=MagicMock())
            context.close = AsyncMock()
        browser.new_context = AsyncMock(side_effect=contexts)
        browser.close = AsyncMock()
        browser.is_connected.return_value = True
        playwright.request.new_context = AsyncMock()
        playwright.chromium.launch = AsyncMock(return_value=browser)
        manager.__aenter__ = AsyncMock(return_value=playwright)
        manager.__aexit__ = AsyncMock()
        saved_starts = []

        async def scrape(current, url):
            self.assertIs(current, contexts[len(self.starts)].new_page.return_value)
            self.starts.append((24, self.clock.elapsed))
            self.clock.elapsed += 1
            return [{"seller_id": "store", "price": "1", "currency": "USD"}]

        with self.environment(lambda *_: [self.target(24)]):
            with patch.object(crawl_from_db, "async_playwright", return_value=manager) as driver, \
                 patch.object(crawl_from_db, "scrape_eldorado_dom", side_effect=scrape), \
                 patch.object(db, "increment_version", side_effect=[1, 2, 3]), \
                 patch.object(db, "save_crawl_data", return_value=1) as save, \
                 patch.object(db, "update_last_crawl", side_effect=lambda tid, at: saved_starts.append(at)), \
                 patch.object(db, "insert_crawl_notify") as notify:
                self.assertEqual(asyncio.run(crawl_from_db.run_scheduled(0, 2, 65)), 0)
        self.assertEqual(self.starts, [(24, 0), (24, 30), (24, 60)])
        self.assertEqual(saved_starts, [datetime.fromtimestamp(self.clock.base + second)
                                       for second in (0, 30, 60)])
        self.assertEqual(save.call_count, 3)
        self.assertEqual([call.args for call in notify.call_args_list],
                         [(24, 1, 1), (24, 2, 1), (24, 3, 1)])
        driver.assert_called_once_with()
        playwright.request.new_context.assert_not_awaited()
        playwright.chromium.launch.assert_awaited_once()
        for context in contexts:
            context.close.assert_awaited_once()
        browser.close.assert_awaited_once()
        manager.__aexit__.assert_awaited_once()


if __name__ == "__main__":
    unittest.main()
