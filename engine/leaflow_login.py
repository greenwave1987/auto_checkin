# engine/playwright_login.py

import time
from playwright.sync_api import sync_playwright

LOGIN_URL = "https://leaflow.net/login"
DASHBOARD_URL = "https://leaflow.net/dashboard"

def open_browser(proxy_url=None):
    pw = sync_playwright().start()
    proxy_config = {"server": proxy_url} if proxy_url else None
    
    browser = pw.chromium.launch(
        headless=True,
        args=["--no-sandbox", "--disable-dev-shm-usage"],
        proxy=proxy_config
    )
    # 在上下文也配置代理
    ctx = browser.new_context(proxy=proxy_config)
    page = ctx.new_page()
    return pw, browser, ctx, page


def cookies_ok(page):
    print("🔍 校验 cookies")
    page.goto(DASHBOARD_URL, timeout=30000)
    time.sleep(2)
    return "login" not in page.url.lower()


def login_and_get_cookies(page, email, password):
    print(f"🔐 登录: {email}")

    page.goto(LOGIN_URL, timeout=30000)
    page.wait_for_selector("#account")
    page.fill("#account", email)
    time.sleep(2)
    
    page.wait_for_selector("#password")
    page.fill("#password", password)
    time.sleep(2)
    
    page.get_by_role("checkbox",name="保持登录状态").click()
    time.sleep(1)

    page.locator('button[type="submit"]').click()
    page.wait_for_load_state("networkidle")

    if "login" in page.url.lower():
        raise RuntimeError("登录失败")

    print("🎉 登录成功")
    return page.context.cookies()
