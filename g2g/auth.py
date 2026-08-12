"""登录认证与会话管理"""

import asyncio
from playwright.async_api import TimeoutError as PlaywrightTimeout
from g2g import config


async def ensure_logged_in(page, context) -> bool:
    """
    确保已登录。
    - 如果页面已在登录后状态，直接返回 True
    - 否则执行登录流程（自动填表 + 手动 OTP）
    返回 True=成功, False=失败
    """
    # 先试试直接访问 offers 页面
    await page.goto(config.OFFERS_LIST_URL, wait_until="domcontentloaded", timeout=60000)
    await asyncio.sleep(5)

    if "login" not in page.url:
        print(f"[✓] 会话有效，已登录")
        return True

    # 需要重新登录
    print("[*] 需要登录...")
    return await do_login(page, context)


async def do_login(page, context) -> bool:
    """执行登录流程"""
    await page.goto(config.LOGIN_URL, wait_until="domcontentloaded", timeout=60000)

    # 等待页面渲染
    print("[*] 等待页面加载...")
    for i in range(60):
        inputs = await page.query_selector_all("input")
        if len(inputs) > 0:
            print(f"  -> 页面加载完成 ({i+1}s)")
            break
        await asyncio.sleep(1)

    # 关闭 Cookie 提示
    await page.evaluate("""
        document.querySelectorAll('[id*="cookie"], [class*="cookie"], [class*="consent"]')
            .forEach(el => { if (el.style) el.style.display = 'none'; });
    """)

    # 填写表单
    try:
        email_input = await page.wait_for_selector('input[type="text"]', state="visible", timeout=5000)
        await email_input.click()
        await email_input.fill(config.EMAIL)
        print("  -> 已填写邮箱")

        pwd_input = await page.wait_for_selector('input[type="password"]', state="visible", timeout=3000)
        await pwd_input.click()
        await pwd_input.fill(config.PASSWORD)
        print("  -> 已填写密码")
    except PlaywrightTimeout:
        print("[!] 未找到登录表单")
        return False

    # 点击登录
    btn = None
    for sel in ['button:has-text("登录")', 'button:has-text("Log In")',
                'button:has-text("Login")', 'button:has-text("Sign In")',
                'button[type="submit"]']:
        try:
            btn = await page.wait_for_selector(sel, state="visible", timeout=2000)
            if btn:
                break
        except PlaywrightTimeout:
            continue

    if btn:
        await btn.click()
    else:
        await pwd_input.press("Enter")
    print("  -> 已提交登录")

    # 等待登录完成（含手动 OTP）
    print("[*] 等待登录完成（如出现 OTP 请手动输入）...")
    otp_notified = False
    for i in range(180):
        await asyncio.sleep(1)
        url = page.url
        if "login" not in url and "signin" not in url:
            print(f"\n[✓] 登录成功! {url}")
            return True

        if not otp_notified:
            page_text = await page.evaluate("document.body.innerText")
            if "OTP" in page_text or "动态密码" in page_text:
                otp_notified = True
                print(f"\n{'='*50}")
                print("⚠️  OTP 验证码弹窗已出现!")
                print("   请查看邮箱，在浏览器中输入验证码")
                print(f"{'='*50}")

        if (i + 1) % 15 == 0:
            print(f"  -> 等待中... ({i+1}s)")

    print("[!] 登录超时")
    return False


def extract_token_from_page(page) -> str | None:
    """从页面 localStorage 中提取 JWT token（同步方式不行，需要 async）"""
    pass  # 用下面的 async 版本


async def get_token(page) -> str | None:
    """从 localStorage / sessionStorage 提取 JWT token"""
    token = await page.evaluate("""
        (() => {
            const pattern = /eyJ[A-Za-z0-9_-]+\\.[A-Za-z0-9_-]+\\.[A-Za-z0-9_-]+/;
            for (let i = 0; i < localStorage.length; i++) {
                const val = localStorage.getItem(localStorage.key(i));
                if (val && val.includes('eyJ')) {
                    try {
                        const str = JSON.stringify(JSON.parse(val));
                        const m = str.match(pattern);
                        if (m) return m[0];
                    } catch(e) {
                        const m = val.match(pattern);
                        if (m) return m[0];
                    }
                }
            }
            for (let i = 0; i < sessionStorage.length; i++) {
                const val = sessionStorage.getItem(sessionStorage.key(i));
                if (val && val.includes('eyJ')) {
                    const m = val.match(pattern);
                    if (m) return m[0];
                }
            }
            return null;
        })()
    """)
    return token
