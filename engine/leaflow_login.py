# engine/leaflow_login.py
import time
from playwright.sync_api import sync_playwright

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

def login_and_get_cookies(page, email, password):
    print(f"🔑 正在登录: {email}...")
    try:
        page.goto("https://leaflow.net/login", timeout=40000)
        page.fill("#account", email)
        page.fill("#password", password)
        page.click('button[type="submit"]')
        
        # 等待跳转到 dashboard 或 url 变化
        page.wait_for_load_state("networkidle")
        time.sleep(3)
        
        if "login" in page.url.lower():
            print("❌ 登录失败，页面仍留在登录页")
            return None
            
        print("✅ 登录成功，提取 Cookies")
        return page.context.cookies()
    except Exception as e:
        print(f"❌ 登录过程出错: {e}")
        # 截图留存以供 Actions Artifact 下载调试
        page.screenshot(path=f"error_{email}.png")
        return None
