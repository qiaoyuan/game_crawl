"""浏览器管理"""

from playwright.async_api import async_playwright
from g2g import config


async def create_browser(p, headless=None):
    """创建浏览器实例（含反检测）。引擎/无头模式由 config 环境变量控制。"""
    browser = await p.chromium.launch(**config.browser_launch_kwargs(headless))
    return browser


async def create_context(browser, use_session=True):
    """创建浏览器上下文，可选加载已保存的会话"""
    import os

    kwargs = {
        "viewport": config.VIEWPORT,
        "user_agent": config.USER_AGENT,
    }

    if use_session and os.path.exists(config.SESSION_FILE):
        kwargs["storage_state"] = config.SESSION_FILE

    context = await browser.new_context(**kwargs)

    # 反自动化检测
    await context.add_init_script("""
        Object.defineProperty(navigator, 'webdriver', { get: () => undefined });
        window.chrome = { runtime: {} };
    """)

    return context


async def save_session(context):
    """保存会话"""
    await context.storage_state(path=config.SESSION_FILE)
    print(f"[✓] 会话已保存")


def has_session() -> bool:
    """检查是否有已保存的会话"""
    import os
    return os.path.exists(config.SESSION_FILE)


def clear_session():
    """清除会话"""
    import os
    if os.path.exists(config.SESSION_FILE):
        os.remove(config.SESSION_FILE)
        print("[*] 已清除会话")
