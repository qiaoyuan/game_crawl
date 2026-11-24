from pathlib import Path


def chrome_debug_endpoint(active_port_file):
    """从 Chrome 本机调试信息读取地址；内容具有访问权限，不得记录。"""
    try:
        lines = Path(active_port_file).read_text(encoding="utf-8").splitlines()
        port = int(lines[0])
        path = lines[1]
        if not 1 <= port <= 65535 or not path.startswith("/devtools/browser/"):
            raise ValueError
        if any(c in path for c in "?# \t\r\n"):
            raise ValueError
    except (OSError, UnicodeError, IndexError, ValueError):
        raise ValueError("无法读取 Chrome 调试连接信息；请确认已打开远程调试。") from None
    return f"ws://127.0.0.1:{port}{path}"
