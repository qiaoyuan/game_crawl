import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import MagicMock, patch

import pymysql

from tools.server_monitor import Collector, cpu_times, positive_seconds, server_identity, write_sample, run


class MonitorTests(unittest.TestCase):
    def test_linux_sample_and_guest_not_double_counted(self):
        with tempfile.TemporaryDirectory() as directory:
            proc = Path(directory)
            (proc / 'stat').write_text('cpu 100 0 100 800 0 0 0 0 50 0\n')
            self.assertEqual(cpu_times(proc), (1000, 800))
            collector = Collector(proc=proc)
            (proc / 'stat').write_text('cpu 125 0 125 850 0 0 0 0 60 0\n')
            (proc / 'meminfo').write_text('MemTotal: 1000 kB\nMemAvailable: 250 kB\nSwapTotal: 0 kB\nSwapFree: 0 kB\n')
            with patch('tools.server_monitor.os.getloadavg', return_value=(2, 1, .5)), patch('tools.server_monitor.os.cpu_count', return_value=2):
                payload = collector.sample('crawler-2', '爬虫2')
            self.assertEqual(payload['cpu_percent'], 50)
            self.assertEqual(payload['memory_percent'], 75)
            self.assertEqual(payload['memory_available_bytes'], 256000)
            self.assertEqual(payload['swap_percent'], 0)
            self.assertEqual(payload['cpu_cores'], 2)
            self.assertEqual(payload['load_1'], 2)
            self.assertTrue(payload['sampled_at'].endswith('+00:00'))
            json.dumps(payload, allow_nan=False)
            with self.assertRaises(ValueError):
                collector.sample('crawler-2', '爬虫2')

    def test_default_identity_uses_existing_shard_number(self):
        with patch.dict('os.environ', {'crawl_server': '3'}, clear=True):
            self.assertEqual(server_identity(), ('crawler-3', '爬虫3'))

    def test_duplicate_does_not_change_history_or_heartbeat(self):
        payload = {'server_code': 'crawler-2', 'name': '爬虫2', 'sampled_at': '2026-01-01T00:00:00+00:00',
                   'cpu_percent': 20, 'memory_percent': 50, 'load_1': 1}
        with patch('tools.server_monitor.pymysql.connect') as connect:
            conn = connect.return_value.__enter__.return_value
            cur = conn.cursor.return_value.__enter__.return_value
            cur.fetchone.return_value = (1767225600, 1767225601)
            write_sample({}, payload)
            conn.commit.assert_not_called()
            self.assertFalse(any('server_monitor_history' in c.args[0] for c in cur.execute.call_args_list))

    def test_write_failure_waits_for_next_sample(self):
        collector, stop = MagicMock(), MagicMock()
        collector.sample.side_effect = [{'sample': 1}, {'sample': 2}]
        stop.wait.side_effect = [False, False, True]
        with patch('tools.server_monitor.time.monotonic', return_value=0), patch(
                'tools.server_monitor.write_sample', side_effect=[pymysql.OperationalError(2003, 'test'), None]) as write:
            run(collector, {}, 'crawler-2', '爬虫2', 10, stop)
        self.assertEqual(write.call_count, 2)
        self.assertEqual(write.call_args.args[-1], {'sample': 2})
        self.assertEqual([c.args[0] for c in stop.wait.call_args_list], [10, 20, 30])

    def test_invalid_intervals(self):
        for value in ('0', '-1', 'nan', 'inf', 'bad'):
            with self.assertRaises(ValueError):
                positive_seconds(value)
        self.assertEqual(positive_seconds('10'), 10)


if __name__ == '__main__':
    unittest.main()
