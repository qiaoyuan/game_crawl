import fcntl
import gzip
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

from tools import cleanup_logs


class CleanupLogsTests(unittest.TestCase):
    def test_rotation_preserves_inode_contents_and_open_append_writer(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            path = root / "crawl_worker_0.log"
            content = b"worker output\n" * 100
            path.write_bytes(content)
            inode = path.stat().st_ino
            with path.open("ab") as writer:
                cleanup_logs.cleanup(root, 100, 3, 7, False)
                self.assertEqual(path.stat().st_ino, inode)
                self.assertEqual(path.read_bytes(), b"")
                archives = list(root.glob("*.gz"))
                self.assertEqual(len(archives), 1)
                with gzip.open(archives[0], "rb") as archived:
                    self.assertEqual(archived.read(), content)
                writer.write(b"next round\n")
            self.assertEqual(path.read_bytes(), b"next round\n")

    def test_retention_scope_age_count_and_dry_run(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            names = ["crawl.log.1", "crawl.log.2.gz", "crawl.log.3.gz", "crawl.log.4.gz",
                     "crawl_worker_1.log.1", "xvfb_20261008_074635_1590736_worker_0.log",
                     "crawl.lock", "crawl_wait.lock", "other.log.1", "cron.log",
                     "xvfb_worker_0.log", "crawl.log.bak"]
            now = time.time()
            for index, name in enumerate(names):
                path = root / name
                path.write_bytes(b"keep or remove")
                os.utime(path, (now - index * 60, now - index * 60))
            for name in names[4:]:
                os.utime(root / name, (now - 8 * 86400, now - 8 * 86400))
            (root / "crawl_worker_99.log").symlink_to(root / "other.log.1")
            (root / "nested").mkdir()
            (root / "nested/crawl.log.1").write_text("untouched")
            before = {p.name: p.lstat() for p in root.iterdir()}
            cleanup_logs.cleanup(root, 100, 3, 7, True)
            self.assertEqual(before, {p.name: p.lstat() for p in root.iterdir()})
            cleanup_logs.cleanup(root, 100, 3, 7, False)
            removed = set(names[3:6])
            for name in names:
                self.assertEqual((root / name).exists(), name not in removed, name)
            self.assertTrue((root / "crawl_worker_99.log").is_symlink())
            self.assertEqual((root / "nested/crawl.log.1").read_text(), "untouched")

    def test_failed_archive_does_not_truncate_original(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "crawl.log"
            path.write_bytes(b"must survive")
            with patch.object(cleanup_logs.os, "replace", side_effect=OSError("disk error")):
                with self.assertRaises(OSError):
                    cleanup_logs.rotate(path)
            self.assertEqual(path.read_bytes(), b"must survive")
            self.assertEqual(list(Path(directory).iterdir()), [path])

    def test_cli_preview_and_rotation_count_include_new_backup(self):
        launcher = Path(__file__).resolve().parents[1] / "cleanup_logs.sh"
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            content = b"x" * (1024 * 1024 + 1)
            (root / "crawl.log").write_bytes(content)
            for index in range(1, 4):
                path = root / f"crawl.log.{index}"
                path.write_text("old backup")
                old = time.time() - index * 60
                os.utime(path, (old, old))
            command = ["bash", str(launcher), "--log-dir", directory, "--max-mb", "1"]
            preview = subprocess.run(command + ["--dry-run"], capture_output=True,
                                     text=True, timeout=5)
            self.assertEqual(preview.returncode, 0, preview.stderr)
            self.assertIn("[预览] 轮转 crawl.log", preview.stdout)
            self.assertIn("[预览] 删除备份 crawl.log.3", preview.stdout)
            self.assertEqual((root / "crawl.log").read_bytes(), content)
            self.assertTrue((root / "crawl.log.3").exists())
            actual = subprocess.run(command, capture_output=True, text=True, timeout=5)
            self.assertEqual(actual.returncode, 0, actual.stderr)
            self.assertEqual((root / "crawl.log").read_bytes(), b"")
            self.assertFalse((root / "crawl.log.3").exists())
            self.assertEqual(len(list(root.glob("crawl.log.*"))), 3)

    def test_busy_crawler_prevents_cleanup_with_real_process_lock(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            log = root / "crawl_worker_0.log"
            log.write_bytes(b"x" * (1024 * 1024 + 1))
            with (root / "crawl.lock").open("a+b") as lock:
                fcntl.flock(lock, fcntl.LOCK_EX)
                result = subprocess.run(
                    [sys.executable, cleanup_logs.__file__, "--log-dir", directory,
                     "--max-mb", "1", "--wait-seconds", "0"],
                    capture_output=True, text=True, timeout=5,
                )
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertIn("本次清理跳过", result.stdout)
                self.assertEqual(log.stat().st_size, 1024 * 1024 + 1)
                self.assertEqual(len(list(root.iterdir())), 2)


if __name__ == "__main__":
    unittest.main()
