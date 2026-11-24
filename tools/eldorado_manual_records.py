"""单笔手动采集。订单详情保存首份，聊天保存同一订单最近一次读取的消息。"""

import fcntl
import json
import os
import tempfile
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from uuid import UUID


DEFAULT_ROOT = Path(__file__).resolve().parents[1] / "downloads" / "eldorado_manual"
ORDER_FIELDS = ("order_id", "game", "region", "faction", "server", "quantity",
                "buyer", "order_price", "commission", "you_receive")


def _order_dir(root, order_id):
    try:
        if not isinstance(order_id, str) or str(UUID(order_id)) != order_id:
            raise ValueError
    except ValueError:
        raise ValueError("订单号须为标准小写、带连字符的 UUID") from None
    return Path(root) / order_id


@contextmanager
def _locked_directory(directory, *, create=False):
    if create:
        directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    try:
        fd = os.open(directory, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    except FileNotFoundError:
        raise ValueError("请先保存该订单的详情，再保存聊天") from None
    try:
        # A directory lock survives atomic replacement of either data file.
        fcntl.flock(fd, fcntl.LOCK_EX)
        yield fd
    finally:
        os.close(fd)


def _read_record(path, order_id):
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    except FileNotFoundError:
        return None
    with os.fdopen(fd, encoding="utf-8") as stream:
        first = stream.readline()
        if not first or stream.read(1):
            raise ValueError(f"{path.name} 应仅包含一行 JSON")
    try:
        record = json.loads(first)
    except ValueError:
        raise ValueError(f"{path.name} 的 JSON 无法读取") from None
    if not isinstance(record, dict) or record.get("order_id") != order_id:
        raise ValueError(f"{path.name} 的订单号与目录不一致")
    return record


def _validate_order(fields):
    if not isinstance(fields, dict) or not all(isinstance(fields.get(key), str)
                                              and fields[key].strip() for key in ORDER_FIELDS):
        raise ValueError("订单详情字段不完整")


def _replace_record(path, record, dir_fd):
    # mkstemp creates a private (0600) file on the same filesystem.
    fd, temporary = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            json.dump(record, stream, ensure_ascii=False, separators=(",", ":"), allow_nan=False)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        os.fsync(dir_fd)
    finally:
        Path(temporary).unlink(missing_ok=True)


def find_saved_order(order_id, *, root=DEFAULT_ROOT):
    path = _order_dir(root, order_id) / "order.jsonl"
    record = _read_record(path, order_id)
    if record is None:
        return None
    _validate_order(record)
    return path


def save_order(fields, *, root=DEFAULT_ROOT):
    """Return (path, created). Keep the first successfully captured order forever."""
    _validate_order(fields)
    directory = _order_dir(root, fields["order_id"])
    path = directory / "order.jsonl"
    with _locked_directory(directory, create=True) as dir_fd:
        existing = _read_record(path, fields["order_id"])
        if existing is not None:
            _validate_order(existing)
            return path, False
        _replace_record(path, fields, dir_fd)
        return path, True


def save_chat(order_id, messages, *, root=DEFAULT_ROOT):
    """Return (path, changed). Keep a single latest snapshot beside the original order."""
    directory = _order_dir(root, order_id)
    path = directory / "chat.jsonl"
    if not isinstance(messages, list) or not all(isinstance(item, dict) for item in messages):
        raise ValueError("聊天记录应为消息列表")
    with _locked_directory(directory) as dir_fd:
        order = _read_record(directory / "order.jsonl", order_id)
        if order is None:
            raise ValueError("请先保存该订单的详情，再保存聊天")
        _validate_order(order)
        existing = _read_record(path, order_id)
        if existing is not None and existing.get("messages") == messages:
            return path, False
        snapshot = {"order_id": order_id, "captured_at": datetime.now(timezone.utc).isoformat(),
                    "messages": messages}
        _replace_record(path, snapshot, dir_fd)
        return path, True
