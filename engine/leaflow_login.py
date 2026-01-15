import time
from playwright.sync_api import sync_playwright, TimeoutError as PlaywrightTimeoutError
from engine.notify import send_notify

LOGIN_URL = "https://leaflow.net/login"
DASHBOARD_URL = "https://leaflow.net/dashboard"

step = 0  # 全局步骤计数


# ==================================================
# 启动浏览器
# ==================================================
def open_browser(proxy_url=None):
    print("🚀 启动 Playwright 浏览器")
    pw = sync_playwright().start()

    proxy_config = {"server": proxy_url} if proxy_url else None
    print(f"🌐 使用代理: {proxy_url}" if proxy_url else "🌐 未使用代理")

    browser = pw.chromium.launch(
        headless=True,
        args=["--no-sandbox", "--disable-dev-shm-usage"],
        proxy=proxy_config
    )

    ctx = browser.new_context(proxy=proxy_config)
    page = ctx.new_page()

    print("✅ 浏览器启动完成")
    return pw, browser, ctx, page


# ==================================================
# Cookie 校验
# ==================================================
def cookies_ok(page):
    print("🔍 校验 cookies 是否仍然有效")
    try:
        page.goto(DASHBOARD_URL, timeout=30000)
        page.wait_for_load_state("networkidle")

        if "login" in page.url.lower():
            print("❌ Cookie 已失效")
            return False

        print("✅ Cookie 有效")
        return True

    except Exception as e:
        print(f"❌ Cookie 校验失败: {e}")
        return False


# ==================================================
# 截屏（安全版）
# ==================================================
def take_shot(page, name):
    global step
    step += 1
    filename = f"{step:02d}_{name}.png"

    try:
        page.screenshot(path=filename, full_page=True)
        print(f"📸 截图成功: {filename}")
        return filename
    except Exception as e:
        print(f"⚠️ 截图失败: {e}")
        return None


# ==================================================
# 登录并获取 cookies
# ==================================================
def login_and_get_cookies(page, email, password):
    print(f"🔐 开始登录账号: {email}")

    try:
        # 打开登录页
        print("🌍 打开登录页面")
        page.goto(LOGIN_URL, timeout=30000)
        page.wait_for_load_state("domcontentloaded")

        # 输入账号
        print("✍️ 输入账号")
        page.wait_for_selector("#account", timeout=10000)
        page.fill("#account", email)

        # 输入密码
        print("✍️ 输入密码")
        page.wait_for_selector("#password", timeout=10000)
        page.fill("#password", password)

        # 勾选保持登录
        print("☑️ 勾选「保持登录状态」")
        try:
            checkbox = page.get_by_role("checkbox", name="保持登录状态")
            checkbox.click(timeout=5000)
            print(f"   ↳ checkbox 状态: {checkbox.get_attribute('aria-checked')}")
        except PlaywrightTimeoutError:
            print("⚠️ 未找到保持登录复选框，跳过")

        # 截图 & 通知（不影响主流程）
        shot1 = take_shot(page, "准备登录")
        if shot1:
            try:
                send_notify.photo("leaflow_login", "准备登录", shot1)
            except Exception as e:
                print(f"⚠️ 通知发送失败: {e}")

        # 点击登录
        print("➡️ 点击登录按钮")
        page.locator('button[type="submit"]').click()

        print("⏳ 等待登录完成")
        page.wait_for_load_state("networkidle", timeout=60000)
        time.sleep(5)

        shot2 = take_shot(page, "登录完成")
        if shot2:
            try:
                send_notify.photo("leaflow_login", "登录完成", shot2)
            except Exception as e:
                print(f"⚠️ 通知发送失败: {e}")

        # 登录结果判断
        print(f"🔎 当前 URL: {page.url}")
        if "login" in page.url.lower():
            raise RuntimeError("登录失败：仍在登录页")

        print("🎉 登录成功")
        return page.context.cookies()

    except Exception as e:
        print(f"❌ 登录失败: {e}")
        print(f"   当前 URL: {page.url}")
        raise
