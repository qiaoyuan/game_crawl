"""
G2G 工具集 - 主入口
用法:
  python main.py export    # 导出 Offers (bulk_export)
  python main.py scrape    # 爬取商户数据
  python main.py login     # 仅登录并保存会话
"""

import sys
import asyncio


def main():
    if len(sys.argv) < 2:
        print(__doc__)
        print("可用命令:")
        print("  export    导出 Offers (bulk_export API)")
        print("  scrape    爬取商户数据 (Offers 列表 + 卖家信息)")
        print("  login     仅登录并保存会话")
        print("  reset     清除已保存的会话")
        return

    command = sys.argv[1]

    if command == "export":
        from tools.export_offers import run
        asyncio.run(run())
    elif command == "scrape":
        from tools.scrape_seller import run
        asyncio.run(run())
    elif command == "login":
        from tools.export_offers import run  # 登录流程一样
        asyncio.run(run())
    elif command == "reset":
        from g2g.browser import clear_session
        clear_session()
        print("[✓] 会话已清除，下次运行需重新登录")
    else:
        print(f"[!] 未知命令: {command}")
        print(__doc__)


if __name__ == "__main__":
    main()
