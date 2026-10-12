"""按大小轮转生产日志，并清理过期备份；只依赖 Python 标准库。"""

import argparse
import fcntl
import gzip
import os
from pathlib import Path
import re
import shutil
import stat
import tempfile
import time


ACTIVE = r"(?:crawl|crawl_worker_[0-9]+|xvfb_worker_[0-9]+|cron)\.log"
ACTIVE_RE = re.compile(ACTIVE)
BACKUP_RE = re.compile(r"(" + ACTIVE + r")\.[0-9]+(?:\.gz)?")
LEGACY_RE = re.compile(r"xvfb_[0-9]{8}_[0-9]{6}_[0-9]+_worker_[0-9]+\.log")


def positive_int(value):
    number = int(value)
    if number < 1:
        raise argparse.ArgumentTypeError("必须为正整数")
    return number


def nonnegative_int(value):
    number = int(value)
    if number < 0:
        raise argparse.ArgumentTypeError("不能小于 0")
    return number


def regular_file(path):
    # 不跟随符号链接，也不处理子目录。
    return stat.S_ISREG(path.lstat().st_mode)


def rotate(path):
    """压缩成功落盘后原地截断，保留写日志进程使用的 inode。"""
    archive = path.with_name(f"{path.name}.{time.time_ns()}.gz")
    fd, temporary = tempfile.mkstemp(prefix=".cleanup-", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as output:
            with path.open("r+b") as source:
                with gzip.GzipFile(filename="", mode="wb", fileobj=output) as compressed:
                    shutil.copyfileobj(source, compressed)
                output.flush()
                os.fsync(output.fileno())
                os.replace(temporary, archive)
                source.truncate(0)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def cleanup(directory, max_bytes, keep, days, dry_run):
    cutoff = time.time() - days * 86400
    backups = {}
    rotated = set()
    for path in sorted(directory.iterdir()):
        if not regular_file(path):
            continue
        if ACTIVE_RE.fullmatch(path.name) and path.stat().st_size > max_bytes:
            print(f"{'[预览] ' if dry_run else ''}轮转 {path.name}", flush=True)
            if not dry_run:
                rotate(path)
            rotated.add(path.name)

    for path in directory.iterdir():
        if not regular_file(path):
            continue
        match = BACKUP_RE.fullmatch(path.name)
        if match:
            backups.setdefault(match[1], []).append(path)
        elif LEGACY_RE.fullmatch(path.name) and path.stat().st_mtime < cutoff:
            print(f"{'[预览] ' if dry_run else ''}删除旧 Xvfb 日志 {path.name}", flush=True)
            if not dry_run:
                path.unlink()

    for name, paths in backups.items():
        # 预览时也计算即将新增的一个备份占用的名额。
        slots = keep - int(dry_run and name in rotated)
        paths.sort(key=lambda path: (path.stat().st_mtime_ns, path.name), reverse=True)
        for index, path in enumerate(paths):
            if index >= slots or path.stat().st_mtime < cutoff:
                print(f"{'[预览] ' if dry_run else ''}删除备份 {path.name}", flush=True)
                if not dry_run:
                    path.unlink()


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--log-dir", type=Path,
                        default=Path(__file__).resolve().parents[1] / "logs")
    parser.add_argument("--max-mb", type=positive_int, default=20,
                        help="单个当前日志超过此大小时轮转，单位 MiB（默认 20）")
    parser.add_argument("--keep", type=positive_int, default=3,
                        help="每个日志最多保留的备份数（默认 3）")
    parser.add_argument("--days", type=positive_int, default=7,
                        help="删除超过此天数的备份及旧 Xvfb 日志（默认 7）")
    parser.add_argument("--wait-seconds", type=nonnegative_int, default=30,
                        help="等待爬虫运行锁的最长秒数（默认 30）")
    parser.add_argument("--dry-run", action="store_true", help="仅预览，不修改日志")
    args = parser.parse_args(argv)
    if not args.log_dir.exists():
        print(f"日志目录不存在，无需清理：{args.log_dir}")
        return 0

    # 复用启动脚本的运行锁，避免清理时 worker 仍写入；此文件永远不删除。
    lock_path = args.log_dir / "crawl.lock"
    if lock_path.is_symlink():
        raise RuntimeError("拒绝使用符号链接 crawl.lock")
    with lock_path.open("a+b") as lock:
        deadline = time.monotonic() + args.wait_seconds
        while True:
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                if time.monotonic() >= deadline:
                    print("爬虫或其他清理进程仍在运行，本次清理跳过")
                    return 0
                time.sleep(min(0.1, max(0, deadline - time.monotonic())))
        cleanup(args.log_dir, args.max_mb * 1024 * 1024, args.keep,
                args.days, args.dry_run)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
