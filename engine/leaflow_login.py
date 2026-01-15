import time
from playwright.sync_api import sync_playwright, TimeoutError as PlaywrightTimeoutError
from notify import send_notify

LOGIN_URL = "https://leaflow.net/login"
DASHBOARD_URL = "https://leaflow.net/dashboard"

step=0
# ==================================================
# 启动浏览器
# ==================================================
def open_browser(proxy_url=None):
    print("🚀 启动 Playwright 浏览器")
    pw = sync_playwright().start()

    proxy_config = {"server": proxy_url} if proxy_url else None
    if proxy_config:
        print(f"🌐 使用代理: {proxy_url}")
    else:
        print("🌐 未使用代理")

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
        time.sleep(1)

        if "login" in page.url.lower():
            print("❌ Cookie 已失效，跳转到登录页")
            return False

        print("✅ Cookie 有效，已进入 Dashboard")
        return True

    except PlaywrightTimeoutError:
        print("❌ Cookie 校验失败：页面加载超时")
        return False

    except Exception as e:
        print(f"❌ Cookie 校验异常: {e}")
        return False
# ==================================================
# 截屏
# ==================================================
def shot(page, name):
        step += 1
        f = f"{step:02d}_{name}.png"
        try:
            page.screenshot(path=f)
            self.shots.append(f)
        except:
            pass
        return f
# ==================================================
# 登录并获取 cookies
# ==================================================
def login_and_get_cookies(page, email, password):
    print(f"🔐 开始登录账号: {email}")

    try:
        # ------------------------------
        # 打开登录页
        # ------------------------------
        print("🌍 打开登录页面")
        page.goto(LOGIN_URL, timeout=30000)
        page.wait_for_load_state("domcontentloaded")

        # ------------------------------
        # 输入账号
        # ------------------------------
        print(f"✍️ 输入账号")
        page.wait_for_selector("#account", timeout=10000)
        page.fill("#account", email)
        time.sleep(2)

        # ------------------------------
        # 输入密码
        # ------------------------------
        print(f"✍️ 输入密码")
        page.wait_for_selector("#password", timeout=10000)
        page.fill("#password", password)
        time.sleep(2)

        # ------------------------------
        # 勾选“保持登录状态”
        # ------------------------------
        print("☑️ 勾选「保持登录状态」")
        checkbox = page.get_by_role("checkbox", name="保持登录状态")

        try:
            checkbox.click(timeout=5000)
            state = checkbox.get_attribute("aria-checked")
            print(f"   ↳ 当前 checkbox 状态: {state}")
        except PlaywrightTimeoutError:
            print("⚠️ 未找到「保持登录状态」复选框，继续登录")

        shot(page, "准备登录")
        # ------------------------------
        # 点击登录
        # ------------------------------
        print("➡️ 点击登录按钮")
        page.locator('button[type="submit"]').click()

        print("⏳ 等待登录完成")
        page.wait_for_load_state("networkidle", timeout=60000)
        time.sleep(20)
        shot(page, "登录完成")
        tg.photo(shot, "两步验证页面（数字在图里）")

        # ------------------------------
        # 登录结果判断
        # ------------------------------
        current_url = page.url.lower()
        print(f"🔎 当前页面 URL: {current_url}")

        if "login" in current_url:
            raise RuntimeError("登录失败：仍停留在登录页（账号或密码错误）")

        print("🎉 登录成功，获取 cookies")
        return page.context.cookies()

    except PlaywrightTimeoutError as e:
        print("❌ 登录失败：页面加载或元素等待超时")
        print(f"   详细错误: {e}")
        print(f"   当前 URL: {page.url}")
        raise

    except RuntimeError as e:
        print(f"❌ 登录失败：{e}")
        print(f"   当前 URL: {page.url}")
        raise

    except Exception as e:
        print("❌ 登录过程中发生未知异常")
        print(f"   错误信息: {e}")
        print(f"   当前 URL: {page.url}")
        raise
