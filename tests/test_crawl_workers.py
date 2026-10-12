import asyncio
import contextlib
import io
import os
from pathlib import Path
import shlex
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import MagicMock, patch

from g2g import config, db
from tools import crawl_from_db


class CrawlWorkerTests(unittest.TestCase):
    def install_cleanup_runtime(self, directory):
        """Use the real stdlib cleanup entry point in an isolated production tree."""
        root = Path(directory)
        (root / "venv/bin").mkdir(parents=True)
        (root / "venv/bin/python").symlink_to(sys.executable)
        (root / "tools").mkdir()
        shutil.copyfile(Path(__file__).resolve().parents[1] / "tools/cleanup_logs.py",
                        root / "tools/cleanup_logs.py")

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

    def test_launcher_defaults_to_one_and_skips_locked_round(self):
        launcher = (Path(__file__).resolve().parents[1] / "run_crawl_and_consume.sh").read_text()
        for lock_exit, expected_exit in ((0, 0), (1, 0), (2, 2)):
            with self.subTest(lock_exit=lock_exit), tempfile.TemporaryDirectory() as directory:
                self.install_cleanup_runtime(directory)
                script = launcher.replace("/www/wwwroot/game_crawl", directory)
                script = script.replace("/usr/bin/xvfb-run", "fake_worker")
                script = script.replace("/usr/bin/flock", "fake_flock")
                prelude = f"""
unset CRAWL_WORKER_COUNT
fake_flock() {{ return {lock_exit}; }}
fake_worker() {{
    echo "$*" >> "{directory}/events"
}}
"""
                result = subprocess.run(["bash", "-c", prelude + script], text=True,
                                        capture_output=True, timeout=10)
                self.assertEqual(result.returncode, expected_exit, result.stderr)
                events = Path(directory) / "events"
                if lock_exit == 0:
                    calls = events.read_text().splitlines()
                    self.assertEqual(len(calls), 1)
                    self.assertIn("-a -n 90", calls[0])
                    self.assertIn("--worker-count 1 --worker-index 0", calls[0])
                    self.assertIn("--scheduled --worker-count", calls[0])
                else:
                    self.assertFalse(events.exists())

    def test_launcher_waits_on_boundary_and_reports_timeout_or_lock_error(self):
        launcher = (Path(__file__).resolve().parents[1] / "run_crawl_and_consume.sh").read_text()
        for wait_exit, expected_exit in ((0, 0), (1, 0), (2, 2)):
            with self.subTest(wait_exit=wait_exit), tempfile.TemporaryDirectory() as directory:
                self.install_cleanup_runtime(directory)
                script = launcher.replace("/www/wwwroot/game_crawl", directory)
                script = script.replace("/usr/bin/xvfb-run", "fake_worker")
                script = script.replace("/usr/bin/flock", "fake_flock")
                prelude = f'''
unset CRAWL_WORKER_COUNT
fake_flock() {{
    echo "$*" >> "{directory}/locks"
    if [ "$*" = "-n 8" ]; then return 0; fi
    if [ "$*" = "-n 9" ]; then return 1; fi
    return {wait_exit}
}}
fake_worker() {{ echo "$*" >> "{directory}/events"; }}
'''
                result = subprocess.run(["bash", "-c", prelude + script], text=True,
                                        capture_output=True, timeout=5)
                self.assertEqual(result.returncode, expected_exit, result.stderr)
                self.assertEqual((Path(directory) / "locks").read_text().splitlines(),
                                 ["-n 8", "-n 9", "-w 30 9"])
                log = (Path(directory) / "logs/crawl.log").read_text()
                self.assertIn("最多等锁 30 秒", log)
                if wait_exit == 0:
                    self.assertEqual(len((Path(directory) / "events").read_text().splitlines()), 1)
                else:
                    self.assertFalse((Path(directory) / "events").exists())
                    self.assertIn("等锁超过 30 秒" if wait_exit == 1 else "获取爬虫运行锁失败", log)

    def test_launcher_keeps_one_waiter_and_runs_it_after_previous_round(self):
        # Exercise OS flock semantics on Linux/macOS without requiring a flock CLI.
        launcher = (Path(__file__).resolve().parents[1] / "run_crawl_and_consume.sh").read_text()
        with tempfile.TemporaryDirectory() as directory:
            self.install_cleanup_runtime(directory)
            root = Path(directory)
            # The cleanup function must run after workers finish, with the same
            # fd 9 still inherited and excluding a separate OS lock acquisition.
            (root / "tools/cleanup_logs.py").write_text('''
import fcntl
import os
from pathlib import Path

def cleanup(directory, max_bytes, keep, days, dry_run):
    assert (directory, max_bytes, keep, days, dry_run) == (Path("logs"), 20 * 1024 * 1024, 3, 7, False)
    assert os.fstat(9).st_ino == (directory / "crawl.lock").stat().st_ino
    with (directory / "crawl.lock").open("a+b") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            pass
        else:
            raise AssertionError("cleanup lost the crawler lock")
    with Path("events").open("a") as events:
        events.write("cleanup " + os.environ["ROUND_NAME"] + "\\n")
''')
            flock_tool = root / "flock.py"
            flock_tool.write_text('''
import fcntl
import sys
import time

fd = int(sys.argv[-1])
deadline = time.monotonic() + (float(sys.argv[2]) if sys.argv[1] == "-w" else 0)
while True:
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        sys.exit(0)
    except BlockingIOError:
        if time.monotonic() >= deadline:
            sys.exit(1)
        time.sleep(0.01)
''')
            script = launcher.replace("/www/wwwroot/game_crawl", directory)
            script = script.replace("/usr/bin/xvfb-run", "fake_worker")
            script = script.replace("/usr/bin/flock", f"{shlex.quote(sys.executable)} {shlex.quote(str(flock_tool))}")
            prelude = f'''
unset CRAWL_WORKER_COUNT
fake_worker() {{
    echo "start $ROUND_NAME" >> "{directory}/events"
    if [ "$ROUND_NAME" = first ]; then
        while [ ! -e "{directory}/release" ]; do sleep 0.02; done
    fi
    echo "done $ROUND_NAME" >> "{directory}/events"
}}
'''
            processes = []

            def launch(name):
                env = os.environ.copy()
                env["ROUND_NAME"] = name
                process = subprocess.Popen(["bash", "-c", prelude + script], env=env,
                                           stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
                processes.append(process)
                return process

            def wait_for_text(path, expected):
                deadline = time.monotonic() + 5
                while time.monotonic() < deadline:
                    if path.exists() and expected in path.read_text():
                        return
                    time.sleep(0.01)
                self.fail(f"Timed out waiting for {expected!r}")

            try:
                first = launch("first")
                wait_for_text(root / "events", "start first")
                second = launch("second")
                wait_for_text(root / "logs/crawl.log", "最多等锁 30 秒")
                third = launch("third")
                _, errors = third.communicate(timeout=5)
                self.assertEqual(third.returncode, 0, errors)
                self.assertEqual((root / "events").read_text().splitlines(), ["start first"])
                self.assertIn("已有触发正在等候上一调度窗口", (root / "logs/crawl.log").read_text())

                (root / "release").touch()
                for process in (first, second):
                    _, errors = process.communicate(timeout=5)
                    self.assertEqual(process.returncode, 0, errors)
                self.assertEqual((root / "events").read_text().splitlines(),
                                 ["start first", "done first", "cleanup first",
                                  "start second", "done second", "cleanup second"])
                self.assertNotIn("日志维护失败", (root / "logs/crawl.log").read_text())
            finally:
                (root / "release").touch()
                for process in processes:
                    if process.poll() is None:
                        process.terminate()
                    try:
                        process.communicate(timeout=5)
                    except subprocess.TimeoutExpired:
                        process.kill()
                        process.communicate(timeout=5)

    def test_launcher_reads_dotenv_worker_count_and_environment_takes_precedence(self):
        launcher = (Path(__file__).resolve().parents[1] / "run_crawl_and_consume.sh").read_text()
        cases = [
            ("CRAWL_WORKER_COUNT=2\n", None, 2),
            ('export CRAWL_WORKER_COUNT = "2" # workers\r\n', None, 2),
            ("CRAWL_WORKER_COUNT='2'\n", None, 2),
            ("CRAWL_WORKER_COUNT=1\nCRAWL_WORKER_COUNT=2\n", None, 2),
            ("OTHER_SETTING=2\n", None, 1),
            ("CRAWL_WORKER_COUNT=2\n", "1", 1),
            ("CRAWL_WORKER_COUNT=invalid\n", "2", 2),
        ]
        for dotenv, override, count in cases:
            with self.subTest(dotenv=dotenv, override=override), tempfile.TemporaryDirectory() as directory:
                self.install_cleanup_runtime(directory)
                # Include unrelated shell syntax to verify that loading dotenv never executes it.
                marker = Path(directory) / "unexpected"
                (Path(directory) / ".env.prod").write_text(
                    dotenv + f"UNRELATED=$(touch {marker})\n"
                )
                script = launcher.replace("/www/wwwroot/game_crawl", directory)
                script = script.replace("/usr/bin/xvfb-run", "fake_worker")
                script = script.replace("/usr/bin/flock", "fake_flock")
                prelude = f'''
fake_flock() {{ return 0; }}
fake_worker() {{ echo "$*" >> "{directory}/events"; }}
'''
                launcher_env = os.environ.copy()
                launcher_env.pop("CRAWL_WORKER_COUNT", None)
                if override is not None:
                    launcher_env["CRAWL_WORKER_COUNT"] = override
                result = subprocess.run(["bash", "-c", prelude + script], text=True,
                                        capture_output=True, timeout=10, env=launcher_env)
                self.assertEqual(result.returncode, 0, result.stderr)
                calls = (Path(directory) / "events").read_text().splitlines()
                self.assertEqual(len(calls), count)
                self.assertEqual({call.split("--worker-index ")[1] for call in calls},
                                 {str(index) for index in range(count)})
                self.assertTrue(all(f"--worker-count {count}" in call for call in calls))
                self.assertTrue(all("--scheduled --worker-count" in call for call in calls))
                self.assertNotIn("日志维护失败", (Path(directory) / "logs/crawl.log").read_text())
                self.assertFalse(marker.exists())

    def test_launcher_rejects_invalid_dotenv_worker_count_before_launch(self):
        launcher = (Path(__file__).resolve().parents[1] / "run_crawl_and_consume.sh").read_text()
        for value in ("0", "-1", "abc", "", "$(touch unexpected)"):
            with self.subTest(value=value), tempfile.TemporaryDirectory() as directory:
                (Path(directory) / ".env.prod").write_text(f"CRAWL_WORKER_COUNT={value}\n")
                script = launcher.replace("/www/wwwroot/game_crawl", directory)
                launcher_env = os.environ.copy()
                launcher_env.pop("CRAWL_WORKER_COUNT", None)
                result = subprocess.run(["bash", "-c", script], text=True, capture_output=True,
                                        timeout=10, env=launcher_env, cwd=directory)
                self.assertEqual(result.returncode, 2)
                self.assertIn("CRAWL_WORKER_COUNT 必须为正整数", result.stderr)
                self.assertFalse((Path(directory) / "logs").exists())
                self.assertFalse((Path(directory) / "unexpected").exists())

    def test_launcher_runs_both_workers_and_waits_after_failure(self):
        # Exercise the actual launcher, replacing only server paths and external
        # executables. The fake workers overlap and the second finishes last.
        launcher = (Path(__file__).resolve().parents[1] / "run_crawl_and_consume.sh").read_text()
        for first_exit, cleanup_exit in ((0, 0), (7, 0), (0, 9), (7, 9)):
            with self.subTest(first_exit=first_exit, cleanup_exit=cleanup_exit), tempfile.TemporaryDirectory() as directory:
                self.install_cleanup_runtime(directory)
                (Path(directory) / "tools/cleanup_logs.py").write_text(f'''
from pathlib import Path

def cleanup(directory, max_bytes, keep, days, dry_run):
    with Path("events").open("a") as events:
        events.write("cleanup\\n")
    raise SystemExit({cleanup_exit})
''')
                script = launcher.replace("/www/wwwroot/game_crawl", directory)
                script = script.replace("/usr/bin/xvfb-run", "fake_worker")
                script = script.replace("/usr/bin/flock", "fake_flock")
                prelude = f'''
export CRAWL_WORKER_COUNT=2
fake_flock() {{ return 0; }}
fake_worker() {{
    local index="${{!#}}"
    local display=""
    while [ "$#" -gt 0 ]; do
        if [ "$1" = "-n" ]; then display="$2"; break; fi
        shift
    done
    echo "start $index display=$display" >> "{directory}/events"
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
                self.assertEqual(set(events[:2]), {"start 0 display=90", "start 1 display=91"})
                self.assertEqual(events[2:], ["done 0", "done 1", "cleanup"])
                log = (Path(directory) / "logs/crawl.log").read_text()
                self.assertIn("worker=1 完成，退出码: 0", log)
                self.assertIn("调度窗口耗时:", log)
                if cleanup_exit:
                    self.assertIn(f"日志维护失败，退出码: {cleanup_exit}", log)
                else:
                    self.assertNotIn("日志维护失败", log)


if __name__ == "__main__":
    unittest.main()
