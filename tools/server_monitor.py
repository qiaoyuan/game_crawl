"""Collect Linux host metrics and write current and minute summaries to the crawler database."""

import argparse
from datetime import datetime, timezone
import fcntl
import json
import logging
import math
import os
from pathlib import Path
import shutil
import signal
import threading
import time
import re

import pymysql
from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parents[1]
LOG = logging.getLogger("server_monitor")


def positive_seconds(value):
    number = float(value)
    if not math.isfinite(number) or number < 1:
        raise ValueError("interval must be a finite number >= 1 second")
    return number


def cpu_times(proc=Path('/proc')):
    fields = (proc / 'stat').read_text().splitlines()[0].split()
    if fields[0] != 'cpu' or len(fields) < 5:
        raise ValueError('invalid /proc/stat')
    # Linux guest/guest_nice already belong to user/nice: do not double count.
    values = [int(v) for v in fields[1:9]]
    idle = values[3] + (values[4] if len(values) > 4 else 0)
    return sum(values), idle


class Collector:
    def __init__(self, disk_path='/', proc=Path('/proc')):
        self.proc = proc
        self.disk_path = disk_path
        self.previous = cpu_times(proc)

    def sample(self, server_code, name):
        current = cpu_times(self.proc)
        total = current[0] - self.previous[0]
        idle = current[1] - self.previous[1]
        self.previous = current
        if total <= 0 or idle < 0:
            raise ValueError('CPU counters reset or sampling interval too short')
        memory = {}
        for line in (self.proc / 'meminfo').read_text().splitlines():
            key, value = line.split(':', 1)
            memory[key] = int(value.split()[0]) * 1024
        available = memory['MemAvailable']
        mem_total = memory['MemTotal']
        if mem_total <= 0:
            raise ValueError('invalid MemTotal')
        swap_total = memory['SwapTotal']
        disk = shutil.disk_usage(self.disk_path)
        loads = os.getloadavg()
        return {
            'server_code': server_code,
            'name': name,
            'sampled_at': datetime.now(timezone.utc).isoformat(timespec='seconds'),
            'cpu_cores': os.cpu_count() or 1,
            'cpu_percent': round(max(0, min(100, (total - idle) / total * 100)), 2),
            'load_1': round(loads[0], 2),
            'load_5': round(loads[1], 2),
            'load_15': round(loads[2], 2),
            'memory_total_bytes': mem_total,
            'memory_available_bytes': available,
            'memory_percent': round((1 - available / mem_total) * 100, 2),
            'swap_total_bytes': swap_total,
            'swap_percent': round((1 - memory['SwapFree'] / swap_total) * 100, 2) if swap_total else 0,
            'disk_path': self.disk_path,
            'disk_total_bytes': disk.total,
            'disk_used_bytes': disk.used,
            'disk_percent': round(disk.used / disk.total * 100, 2),
        }


def server_identity():
    number = int(os.getenv('crawl_server', '1'))
    if number < 1:
        raise ValueError('crawl_server must be positive')
    code = os.getenv('SERVER_MONITOR_CODE', '').strip() or f'crawler-{number}'
    name = os.getenv('SERVER_MONITOR_NAME', '').strip() or f'爬虫{number}'
    if not re.fullmatch(r'[a-zA-Z0-9_-]{1,64}', code) or len(name) > 100:
        raise ValueError('invalid server identity')
    return code, name


def database_settings():
    return {
        'host': os.environ['DB_HOST'], 'port': int(os.getenv('DB_PORT', '3306')),
        'user': os.environ['DB_USER'], 'password': os.getenv('DB_PASSWORD', ''),
        'database': os.environ['DB_NAME'], 'charset': 'utf8mb4',
        'connect_timeout': 2, 'read_timeout': 3, 'write_timeout': 3, 'autocommit': False,
    }


def write_sample(settings, payload):
    code = payload['server_code']
    sampled_at = int(datetime.fromisoformat(payload['sampled_at']).timestamp())
    metrics = {k: v for k, v in payload.items() if k not in ('server_code', 'name', 'sampled_at')}
    snapshot = json.dumps(metrics, ensure_ascii=False, allow_nan=False)
    cpu, memory, load = metrics['cpu_percent'], metrics['memory_percent'], metrics['load_1']

    # Closing an uncommitted connection rolls back both writes on error.
    with pymysql.connect(**settings) as conn:
        with conn.cursor() as cur:
            cur.execute("""
                INSERT INTO server_monitor (server_code, name) VALUES (%s, %s)
                ON DUPLICATE KEY UPDATE server_code = server_monitor.server_code
            """, (code, payload['name']))
            cur.execute("""
                SELECT sampled_at, UNIX_TIMESTAMP() FROM server_monitor
                WHERE server_code = %s FOR UPDATE
            """, (code,))
            previous, db_now = cur.fetchone()
            if sampled_at > db_now + 60 or sampled_at < db_now - 7 * 86400:
                raise ValueError('sample time outside database clock window')
            if previous is not None and sampled_at <= previous:
                return
            minute = min(sampled_at, db_now) // 60 * 60
            cur.execute("""
                INSERT INTO server_monitor_history
                    (server_code, minute_at, samples, cpu_sum, cpu_peak,
                     memory_sum, memory_peak, load_sum, load_peak)
                VALUES (%s, %s, 1, %s, %s, %s, %s, %s, %s)
                ON DUPLICATE KEY UPDATE
                    samples = samples + 1,
                    cpu_sum = cpu_sum + %s, cpu_peak = GREATEST(cpu_peak, %s),
                    memory_sum = memory_sum + %s, memory_peak = GREATEST(memory_peak, %s),
                    load_sum = load_sum + %s, load_peak = GREATEST(load_peak, %s)
            """, (code, minute, cpu, cpu, memory, memory, load, load,
                  cpu, cpu, memory, memory, load, load))
            cur.execute("""
                UPDATE server_monitor SET latest = %s, sampled_at = %s, last_report_at = %s
                WHERE server_code = %s
            """, (snapshot, sampled_at, db_now, code))
        conn.commit()


def run(collector, settings, code, name, interval, stop):
    # Prime CPU counters at startup, then sample after one real interval.
    deadline = time.monotonic() + interval
    failed = False
    while not stop.wait(max(0, deadline - time.monotonic())):
        try:
            payload = collector.sample(code, name)
            write_sample(settings, payload)
            success = True
        except (pymysql.MySQLError, OSError, ValueError, KeyError) as exc:
            LOG.warning('sample/write failed (%s, code=%s); waiting for next interval',
                        type(exc).__name__, exc.args[0] if exc.args and isinstance(exc.args[0], int) else '-')
            success = False
        if success and failed:
            LOG.info('reporting recovered')
        failed = not success
        deadline += interval
        if deadline <= time.monotonic():
            deadline = time.monotonic() + interval


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--dry-run', action='store_true', help='sample once without network or credentials')
    args = parser.parse_args()
    load_dotenv(ROOT / ('.env.prod' if os.getenv('APP_ENV', 'local').strip().lower() == 'prod' else '.env'))
    logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(message)s')
    try:
        interval = positive_seconds(os.getenv('SERVER_MONITOR_INTERVAL', '10'))
        code, name = server_identity()
        settings = None if args.dry_run else database_settings()
        collector = Collector(os.getenv('SERVER_MONITOR_DISK_PATH', '/'))
        stop = threading.Event()
        for signum in (signal.SIGTERM, signal.SIGINT):
            signal.signal(signum, lambda *_: stop.set())
        if args.dry_run:
            if not stop.wait(interval):
                print(json.dumps(collector.sample(code or 'preview', name or 'preview'), ensure_ascii=False, indent=2))
            return 0
        # Separate lock: never acquire or block the crawler's crawl.lock.
        with (ROOT / '.server_monitor.lock').open('a') as lock:
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                LOG.error('another server monitor is already running')
                return 1
            LOG.info('server monitor started; server=%s interval=%ss', code, interval)
            run(collector, settings, code, name, interval, stop)
        return 0
    except (OSError, ValueError, KeyError) as exc:
        # Do not include config values or raw exception text in logs.
        LOG.error('monitor startup failed (%s); check Linux /proc and monitor configuration', type(exc).__name__)
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
