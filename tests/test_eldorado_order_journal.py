"""本地验证首次基线、同秒订单和写 JSONL 中断恢复。"""

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from tools.eldorado_order_journal import OrderJournal


def row(key, second, game="Aion 2"):
    return {"order_id": key, "game": game, "url": "https://www.eldorado.gg/order/" + key,
            "ordered_at": f"2026-01-10T10:00:{second:02d}Z"}


class JournalTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / "orders.jsonl"

    def test_baseline_and_same_second_arrival_use_all_games_watermark(self):
        first, other = row("first", 10), row("other", 12, "Other")
        equal, latest = row("equal", 12, "AION2"), row("latest", 13)
        with OrderJournal(self.path) as journal:
            self.assertEqual(journal.observe([first, other]), (True, 0))
            self.assertEqual(journal.observe([other, equal, latest, row("late-old", 11)]), (False, 2))
            self.assertEqual(journal.observe([latest, equal]), (False, 0))
        # Pending reads survive even after their orders leave the visible page.
        with OrderJournal(self.path) as journal:
            self.assertEqual(journal.pending(), [equal, latest])
            self.assertEqual(journal.observe([row("different-game", 15, "Other")]), (False, 0))
            self.assertEqual(journal.observe([row("old", 14), row("new-id", 15)]), (False, 1))
            self.assertTrue(journal.record(latest, {"character": "sample", "game": latest["game"]}))
            self.assertFalse(journal.record(latest, {"character": "other"}))
            self.assertEqual([item["order_id"] for item in journal.pending()], ["equal", "new-id"])
        self.assertEqual(json.loads(self.path.read_text()),
                         {"character": "sample", "game": "Aion 2", "order_id": "latest",
                          "ordered_at": latest["ordered_at"]})

    def test_empty_initial_page_is_a_baseline_and_second_writer_fails(self):
        with OrderJournal(self.path) as journal:
            self.assertEqual(journal.observe([]), (True, 0))
            with self.assertRaises(RuntimeError), OrderJournal(self.path):
                pass
        with OrderJournal(self.path) as journal:
            self.assertEqual(journal.observe([row("new", 0)]), (False, 1))

    def test_invalid_snapshot_rolls_back_pending_orders_and_checkpoint(self):
        with OrderJournal(self.path) as journal:
            journal.observe([])
            with self.assertRaises(ValueError):
                journal.observe([row("first", 12), {**row("bad", 13), "extra": float("nan")}])
            self.assertEqual(journal.pending(), [])
            self.assertEqual(journal.observe([row("first", 12)]), (False, 1))

    def test_interruption_during_append_recovers_full_or_partial_line_once(self):
        for cut_partial in (False, True):
            path = self.path.with_name(f"{cut_partial}.jsonl")
            order = row("candidate", 0)
            with self.subTest(cut_partial=cut_partial):
                with OrderJournal(path) as journal:
                    journal.observe([])
                    journal.observe([order])
                    with patch("tools.eldorado_order_journal.os.fsync", side_effect=OSError):
                        with self.assertRaises(OSError):
                            journal.record(order, {"character": "测试角色"})
                if cut_partial:
                    # Simulate an incomplete UTF-8 character at the end of the file.
                    data = path.read_bytes()
                    path.write_bytes(data[:data.index("测".encode()) + 1])
                with OrderJournal(path) as journal:
                    self.assertEqual(journal.pending(), [])
                    self.assertFalse(journal.record(order, {"character": "incorrect"}))
                lines = path.read_text().splitlines()
                self.assertEqual(len(lines), 1)
                self.assertEqual(json.loads(lines[0])["character"], "测试角色")

    def test_unrelated_existing_jsonl_is_not_overwritten(self):
        self.path.write_text('{"existing":true}\n')
        with self.assertRaises(RuntimeError), OrderJournal(self.path):
            pass
        self.assertEqual(self.path.read_text(), '{"existing":true}\n')


if __name__ == "__main__":
    unittest.main()
