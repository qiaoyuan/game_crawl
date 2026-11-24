import json
import stat
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import patch
from uuid import uuid4

from tools.eldorado_manual_records import save_chat, save_order


class ManualRecordsTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name) / "manual"
        self.key = str(uuid4())
        self.fields = {"order_id": self.key, "game": "Aion 2", "region": "Region X",
                       "faction": "Faction X", "server": "Server X", "quantity": "1 M",
                       "buyer": "sample buyer", "order_price": "$2.00",
                       "commission": "-$0.20", "you_receive": "$1.80"}

    def test_concurrent_saves_preserve_unique_original_and_replace_only_changed_chat(self):
        def capture(i):
            return save_order({**self.fields, "buyer": f"sample {i}"}, root=self.root)

        with ThreadPoolExecutor(max_workers=4) as pool:
            results = list(pool.map(capture, range(8)))
        first, = [i for i, (_, created) in enumerate(results) if created]
        order_file = self.root / self.key / "order.jsonl"
        self.assertTrue(all(path == order_file for path, _ in results))
        original = order_file.read_bytes()
        self.assertEqual(len(original.splitlines()), 1)
        self.assertEqual(json.loads(original)["buyer"], f"sample {first}")

        messages = [{"text": "message\nwith newline"}]
        path, changed = save_chat(self.key, messages, root=self.root)
        self.assertTrue(changed)
        self.assertEqual(path, order_file.parent / "chat.jsonl")
        before = path.read_bytes()
        self.assertEqual(save_chat(self.key, list(messages), root=self.root), (path, False))
        self.assertEqual(path.read_bytes(), before)
        updated = messages + [{"text": "reply"}]
        self.assertEqual(save_chat(self.key, updated, root=self.root), (path, True))
        self.assertEqual(len(path.read_bytes().splitlines()), 1)
        record = json.loads(path.read_bytes())
        self.assertEqual(record["messages"], updated)
        self.assertEqual(record["order_id"], self.key)
        self.assertEqual(order_file.read_bytes(), original)
        for saved in (path, order_file):
            self.assertEqual(stat.S_IMODE(saved.stat().st_mode), 0o600)

    def test_invalid_order_id_and_missing_or_mismatched_order_block_chat(self):
        for key in ("../x", self.key.replace("-", ""), "{" + self.key + "}"):
            with self.subTest(key=key), self.assertRaises(ValueError):
                save_order({**self.fields, "order_id": key}, root=self.root)
        with self.assertRaises(ValueError):
            save_chat(self.key, [], root=self.root)
        path, _ = save_order(self.fields, root=self.root)
        for invalid in ({"order_id": self.key}, {**self.fields, "order_id": str(uuid4())}):
            path.write_text(json.dumps(invalid) + "\n")
            with self.assertRaises(ValueError):
                save_chat(self.key, [], root=self.root)
        self.assertFalse((path.parent / "chat.jsonl").exists())

    def test_interrupted_chat_save_keeps_previous_complete_capture(self):
        save_order(self.fields, root=self.root)
        path, _ = save_chat(self.key, [], root=self.root)
        original = path.read_bytes()
        with patch("tools.eldorado_manual_records.os.replace", side_effect=OSError):
            with self.assertRaises(OSError):
                save_chat(self.key, [{"text": "new"}], root=self.root)
        self.assertEqual(path.read_bytes(), original)
        self.assertEqual({p.name for p in path.parent.iterdir()}, {"order.jsonl", "chat.jsonl"})


if __name__ == "__main__":
    unittest.main()
