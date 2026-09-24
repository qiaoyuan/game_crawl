import asyncio
import contextlib
import io
from pathlib import Path
import sqlite3
import subprocess
import tempfile
import unittest
from unittest.mock import MagicMock, patch

from g2g import config, db
from tools import crawl_from_db


class CrawlWorkerTests(unittest.TestCase):
    def test_shards_cover_active_targets_once(self):
        with sqlite3.connect(":memory:") as database:
            database.execute(
                "CREATE TABLE crawl_target "
                "(id INTEGER, status INTEGER, deleted_at TEXT, crawl_server INTEGER)"
            )
            database.executemany(
                "INSERT INTO crawl_target VALUES (?, ?, ?, ?)",
                [(1, 1, None, 1), (2, 1, None, 1), (5, 1, None, 1),
                 (8, 1, None, 1), (9, 0, None, 1), (10, 1, "deleted", 1),
                 (11, 1, None, 2), (12, 1, None, 2)],
            )
            for server, expected in [(1, {1, 2, 5, 8}), (2, {11, 12})]:
                with self.subTest(server=server), patch.object(config, "CRAWL_SERVER", server):
                    shards = []
                    for index in range(2):
                        connection = MagicMock()
                        cursor = connection.cursor.return_value.__enter__.return_value
                        with patch.object(db, "get_connection", return_value=connection):
                            db.get_pending_targets(index, 2)
                        sql, params = cursor.execute.call_args.args
                        rows = database.execute(sql.replace("%s", "?"), params).fetchall()
                        shards.append({row[0] for row in rows})
                        connection.close.assert_called_once()
                    self.assertEqual(shards[0] | shards[1], expected)
                    self.assertFalse(shards[0] & shards[1])

    def test_invalid_shard_rejected_before_database_access(self):
        for index, count in [(0, 0), (-1, 2), (2, 2)]:
            with self.subTest(index=index, count=count):
                with patch.object(db, "get_connection") as connect:
                    with self.assertRaises(ValueError):
                        db.get_pending_targets(index, count)
                    connect.assert_not_called()
                with contextlib.redirect_stderr(io.StringIO()):
                    with self.assertRaises(SystemExit) as error:
                        crawl_from_db.parse_args([
                            "--worker-index", str(index), "--worker-count", str(count),
                        ])
                    self.assertEqual(error.exception.code, 2)

    def test_empty_shard_does_not_launch_browser(self):
        with patch.object(db, "get_pending_targets", return_value=[]) as targets:
            with patch.object(crawl_from_db, "async_playwright") as browser:
                self.assertEqual(asyncio.run(crawl_from_db.run(1, 2)), 0)
                targets.assert_called_once_with(1, 2)
                browser.assert_not_called()

    def test_launcher_runs_both_workers_and_waits_after_failure(self):
        # Exercise the actual launcher, replacing only server paths and external
        # executables. The fake workers overlap and the second finishes last.
        launcher = (Path(__file__).resolve().parents[1] / "run_crawl_and_consume.sh").read_text()
        for first_exit in (0, 7):
            with self.subTest(first_exit=first_exit), tempfile.TemporaryDirectory() as directory:
                script = launcher.replace("/www/wwwroot/game_crawl", directory)
                script = script.replace("/usr/bin/xvfb-run", "fake_worker")
                prelude = f'''
fake_worker() {{
    local index="${{!#}}"
    echo "start $index" >> "{directory}/events"
    if [ "$index" = 0 ]; then sleep 0.2; else sleep 0.6; fi
    echo "done $index" >> "{directory}/events"
    if [ "$index" = 0 ]; then return {first_exit}; fi
    return 0
}}
'''
                result = subprocess.run(["bash", "-c", prelude + script], text=True,
                                        capture_output=True, timeout=10)
                self.assertEqual(result.returncode, first_exit, result.stderr)
                events = (Path(directory) / "events").read_text().splitlines()
                self.assertEqual(set(events[:2]), {"start 0", "start 1"})
                self.assertEqual(events[2:], ["done 0", "done 1"])
                log = (Path(directory) / "logs/crawl.log").read_text()
                self.assertIn("worker=1 完成，退出码: 0", log)
                self.assertIn("本轮耗时:", log)


if __name__ == "__main__":
    unittest.main()
