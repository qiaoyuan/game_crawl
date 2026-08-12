"""G2G 配置"""

import os

# 账号
EMAIL = "34348492@qq.com"
PASSWORD = "Q$yg2g123"

# URL
LOGIN_URL = "https://www.g2g.com/login"
OFFERS_LIST_URL = "https://www.g2g.com/offers/list"
BASE_URL = "https://www.g2g.com"
API_BASE = "https://sls.g2g.com"

# 卖家
SELLER_ID = "1004238644"

# bulk_export
BULK_EXPORT_URL = f"{API_BASE}/offer/seller/{SELLER_ID}/bulk_export"
EXPORT_PAYLOAD = {
    "relation_id": "lgc_1_27816_dfced32f-2f0a-4df5-a218-1e068cfadffa",
    "offer_status": "all",
    "out_of_stock": False,
}

# 数据库
# DB_HOST = os.environ.get("DB_HOST", "localhost")
# DB_PORT = int(os.environ.get("DB_PORT", "3306"))
# DB_USER = os.environ.get("DB_USER", "root")
# DB_PASSWORD = os.environ.get("DB_PASSWORD", "")
# DB_NAME = os.environ.get("DB_NAME", "game_platform")

# 正式
DB_HOST = os.environ.get("DB_HOST", "43.106.27.46")
DB_PORT = int(os.environ.get("DB_PORT", "3306"))
DB_USER = os.environ.get("DB_USER", "game_platform")
DB_PASSWORD = os.environ.get("DB_PASSWORD", "fkxE887YYAPnJXDW")
DB_NAME = os.environ.get("DB_NAME", "game_platform")

# 路径
ROOT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DOWNLOAD_DIR = os.path.join(ROOT_DIR, "downloads")
SESSION_FILE = os.path.join(ROOT_DIR, "session.json")

# 浏览器
USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/150.0.0.0 Safari/537.36"
)
VIEWPORT = {"width": 1280, "height": 800}

# 浏览器引擎与运行模式（可用环境变量覆盖）
#   BROWSER_CHANNEL: "chrome" 用系统真实 Chrome（反检测更强，需系统装 Chrome）；
#                    留空则用 Playwright 自带 Chromium（playwright install chromium）
#   BROWSER_HEADLESS: 1/true 为无头模式（Linux 服务器无显示器时必须开启）
BROWSER_CHANNEL = os.environ.get("BROWSER_CHANNEL", "chrome").strip() or None
BROWSER_HEADLESS = os.environ.get("BROWSER_HEADLESS", "0").lower() in ("1", "true", "yes")


def browser_launch_kwargs(headless: bool = None, **extra) -> dict:
    """统一的浏览器启动参数。headless 显式传入时优先，否则用环境变量。"""
    kwargs = {"headless": BROWSER_HEADLESS if headless is None else headless}
    if BROWSER_CHANNEL:
        kwargs["channel"] = BROWSER_CHANNEL
    kwargs.update(extra)
    return kwargs

# API 请求头模板
def api_headers(token: str) -> dict:
    return {
        "accept": "application/json, text/plain, */*",
        "accept-language": "zh-CN,zh;q=0.9",
        "authorization": token,
        "cache-control": "no-cache",
        "content-type": "application/json",
        "origin": BASE_URL,
        "pragma": "no-cache",
        "referer": f"{BASE_URL}/",
        "user-agent": USER_AGENT,
    }
