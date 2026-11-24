"""Persist the sold-list watermark, pending reads, and crash-safe JSONL delivery."""

import fcntl
import json
import os
import re
import sqlite3
import tempfile
from contextlib import ExitStack
from datetime import datetime, timezone
from pathlib import Path


def _json(value):
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), allow_nan=False)


def _candidate(row):
    if not all(isinstance(row.get(field), str) and row[field].strip()
               for field in ("order_id", "game", "url", "ordered_at")):
        raise ValueError("订单行缺少有效的标识、游戏、链接或下单时间")
    date = datetime.fromisoformat(row["ordered_at"].replace("Z", "+00:00"))
    if date.tzinfo is None or date.microsecond:
        raise ValueError("订单时间须为带时区的整秒时间")
    stamp = date.astimezone(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")
    return {**row, "ordered_at": stamp}


class OrderJournal:
    def __init__(self, output_path):
        self.path = Path(output_path).expanduser().resolve()
        self.state_path = Path(str(self.path) + ".sqlite3")
        self.json_path = (self.path.with_suffix(".json") if self.path.suffix == ".jsonl"
                          else Path(str(self.path) + ".json"))

    def __enter__(self):
        with ExitStack() as stack:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            fd = os.open(self.path, os.O_RDWR | os.O_CREAT, 0o600)
            self.output = stack.enter_context(os.fdopen(fd, "r+b"))
            try:
                fcntl.flock(self.output, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                raise RuntimeError("该订单输出文件已由另一个监控进程使用") from None
            os.close(os.open(self.state_path, os.O_RDWR | os.O_CREAT, 0o600))
            self.db = sqlite3.connect(self.state_path)
            stack.callback(self.db.close)
            self.db.executescript("""
                CREATE TABLE IF NOT EXISTS checkpoint (
                    id INTEGER PRIMARY KEY CHECK (id = 1),
                    initialized INTEGER NOT NULL, watermark TEXT NOT NULL,
                    seen_ids TEXT NOT NULL, file_offset INTEGER NOT NULL
                );
                INSERT OR IGNORE INTO checkpoint VALUES (1, 0, '', '[]', 0);
                CREATE TABLE IF NOT EXISTS orders (
                    order_id TEXT PRIMARY KEY, candidate TEXT, result TEXT, exported INTEGER NOT NULL
                );
            """)
            self._flush()
            self._write_json()
            self.resources = stack.pop_all()
        return self

    def __exit__(self, *exc):
        return self.resources.__exit__(*exc)

    def observe(self, rows):
        """Return (created_baseline, queued_count) after one complete list snapshot."""
        rows = [_candidate(row) for row in rows]
        with self.db:
            initialized, watermark, ids = self.db.execute(
                "SELECT initialized, watermark, seen_ids FROM checkpoint WHERE id=1").fetchone()
            seen = set(json.loads(ids))
            queued = 0
            if initialized:
                for row in rows:
                    stamp, key = row["ordered_at"], row["order_id"]
                    if stamp < watermark or (stamp == watermark and key in seen):
                        continue
                    if re.fullmatch(r"aion\s*2", row["game"].strip(), re.I):
                        change = self.db.execute(
                            "INSERT OR IGNORE INTO orders (order_id, candidate, exported) VALUES (?, ?, 0)",
                            (key, _json(row)))
                        queued += change.rowcount
            latest = max((r["ordered_at"] for r in rows), default="")
            if latest >= watermark:
                if latest > watermark:
                    seen.clear()
                seen.update(r["order_id"] for r in rows if r["ordered_at"] == latest)
                watermark = latest
            self.db.execute("UPDATE checkpoint SET initialized=1, watermark=?, seen_ids=? WHERE id=1",
                            (watermark, _json(sorted(seen))))
        return not initialized, queued

    def pending(self):
        """Return known candidates that still need a successful detail read."""
        rows = self.db.execute(
            "SELECT candidate FROM orders WHERE exported=0 AND result IS NULL ORDER BY rowid")
        return [json.loads(candidate) for candidate, in rows]

    def record(self, candidate, fields):
        """Seal a pending candidate and append one line. Repeated calls are idempotent."""
        key = candidate["order_id"]
        with self.db:
            row = self.db.execute(
                "SELECT candidate FROM orders WHERE order_id=? AND exported=0 AND result IS NULL",
                (key,)).fetchone()
            if row:
                saved = json.loads(row[0])
                line = _json({**fields, "order_id": key, "ordered_at": saved["ordered_at"]})
                self.db.execute("UPDATE orders SET result=? WHERE order_id=?", (line, key))
        self._flush()
        if row is not None:
            self._write_json()
        return row is not None

    def _write_json(self):
        """Publish a complete JSON array without exposing a half-written file."""
        self.output.seek(0)
        fd, name = tempfile.mkstemp(dir=self.json_path.parent)
        try:
            with os.fdopen(fd, "wb") as stream:
                stream.write(b"[")
                for index, line in enumerate(self.output):
                    if index:
                        stream.write(b",")
                    stream.write(b"\n" + line.rstrip(b"\n"))
                stream.write(b"\n]\n")
            os.replace(name, self.json_path)
        finally:
            Path(name).unlink(missing_ok=True)

    def _flush(self):
        # Commit the intended line in SQLite before appending. If the process dies
        # before acknowledging the file write, only the matching tail may be completed.
        offset, = self.db.execute("SELECT file_offset FROM checkpoint WHERE id=1").fetchone()
        end = self.output.seek(0, os.SEEK_END)
        if end < offset:
            raise RuntimeError("订单 JSONL 短于已保存的写入位置，请检查输出文件与状态库是否配套")
        for key, result in self.db.execute(
                "SELECT order_id, result FROM orders WHERE result IS NOT NULL AND exported=0 ORDER BY rowid"):
            line = (result + "\n").encode("utf-8")
            self.output.seek(offset)
            written = end - offset
            if written > len(line) or self.output.read(written) != line[:written]:
                raise RuntimeError("订单 JSONL 尾部与待写入记录不一致，请检查输出文件")
            self.output.write(line[written:])
            self.output.flush()
            os.fsync(self.output.fileno())
            offset += len(line)
            with self.db:
                self.db.execute("UPDATE checkpoint SET file_offset=? WHERE id=1", (offset,))
                self.db.execute("UPDATE orders SET candidate=NULL, result=NULL, exported=1 WHERE order_id=?",
                                (key,))
            end = offset
        if end != offset:
            raise RuntimeError("订单 JSONL 有未登记的内容，请使用配套状态库或新的输出文件")
