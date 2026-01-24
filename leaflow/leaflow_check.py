import time
import random
import base64
from playwright.sync_api import sync_playwright, TimeoutError as PlaywrightTimeoutError

# ==================== 基准数据对接 ====================
BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BASE_DIR)
from engine.notify import TelegramNotifier
try:
    from engine.main import ConfigReader, SecretUpdater,print_dict_tree,test_proxy
except ImportError:
    class ConfigReader:
        def get_value(self, key): return os.environ.get(key)
    class SecretUpdater:
        def __init__(self, name=None, config_reader=None): pass
        def update(self, value): return False

LOGIN_URL = "https://leaflow.net/login"
DASHBOARD_URL = "https://leaflow.net/dashboard"

config = None
_notifier = None

def get_notifier():
    global _notifier, config
    if config is None:
        config = ConfigReader()
    if _notifier is None:
        _notifier = TelegramNotifier(config)
    return _notifier

def open_browser(proxy_url=None, storage=None):
    pw = sync_playwright().start()
    proxy_config = {"server": proxy_url} if proxy_url else None
    browser = pw.chromium.launch(
        headless=True,
        args=[
            '--no-sandbox',
            '--disable-blink-features=AutomationControlled',
            '--disable-infobars',
            '--exclude-switches=enable-automation'
        ],
        proxy=proxy_config
    )
    context = browser.new_context(
        storage_state=storage,
        viewport={"width": 1920, "height": 1080},
        user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                   "(KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36"
    )
    page = context.new_page()
    page.add_init_script("""
        Object.defineProperty(navigator, 'webdriver', {get: () => undefined});
        Object.defineProperty(navigator, 'plugins', {get: () => [1,2,3,4,5]});
        Object.defineProperty(navigator, 'languages', {get: () => ['en-US','en']});
        window.chrome = {runtime:{}};
        const originalQuery = window.navigator.permissions.query;
        window.navigator.permissions.query = (parameters) => (
            parameters.name === 'notifications' ? 
            Promise.resolve({state: Notification.permission}) : 
            originalQuery(parameters)
        );
    """)
    return pw, browser, context, page

def cookies_ok(page):
    try:
        page.goto(DASHBOARD_URL, timeout=30000)
        page.wait_for_load_state("networkidle")
        return "login" not in page.url.lower()
    except Exception:
        return False

def login_and_get_cookies(page, email, password):
    page.goto(LOGIN_URL)
    page.fill("#account", email)
    page.fill("#password", password)

    # 模拟点击“保持登录”
    try:
        el = page.get_by_role("checkbox", name="保持登录状态").first
        el.wait_for(state="visible", timeout=5000)
        time.sleep(random.uniform(0.5, 1.2))
        el.hover()
        time.sleep(random.uniform(0.2, 0.4))
        el.click(force=True)
    except PlaywrightTimeoutError:
        pass

    page.locator('button[type="submit"]').click()
    page.wait_for_load_state("networkidle", timeout=60000)
    time.sleep(3)

    if "login" in page.url.lower():
        raise RuntimeError(f"账号 {email} 登录失败")
    return page.context.storage_state()

def get_balance_info(page):
    page.goto("https://leaflow.net/balance")
    balance_locator = page.locator('p[title="点击显示完整格式"]')
    spent_locator = page.locator('p.text-3xl.font-bold:not([title])')
    current_balance = balance_locator.text_content().strip()
    spent_amount = spent_locator.text_content().strip()
    return f"🏦余额: {current_balance}, 已消费: {spent_amount}"

def main():
    global config
    if config is None:
        config = ConfigReader()

    results = []
    new_cookies = {}

    accounts = config.get_value("LF_INFO")
    proxies = config.get_value("PROXY_INFO")
    notify = get_notifier()
    secret = SecretUpdater("LEAFLOW_LOCALS", config_reader=config)
    lf_locals = secret.load() or {}

    for account, proxy in zip(accounts, proxies):
        username = account['username']
        password = account['password']
        proxy_str = f"{proxy['username']}:{proxy['password']}@{proxy['server']}:{proxy['port']}"

        pw, browser, ctx, page = None, None, None, None
        try:
            pw, browser, ctx, page = open_browser(proxy_url=f"socks5://{proxy_str}",
                                                  storage=lf_locals.get(username))

            # 注入 session 测试
            if lf_locals.get(username) and cookies_ok(page):
                results.append(f"账号 {username} session 有效")
            else:
                # 登录获取新的 storage
                storage = login_and_get_cookies(page, username, password)
                new_cookies[username] = storage
                results.append(f"账号 {username} 已登录获取新 session")

            balance_info = get_balance_info(page)
            results.append(f"{username} {balance_info}")

        except Exception as e:
            results.append(f"账号 {username} 异常: {e}")
        finally:
            if browser:
                browser.close()
            if pw:
                pw.stop()
        return
    # 保存更新后的 session
    if new_cookies:
        # 编码为 base64 保存到 LEAFLOW_LOCALS
        encoded = {k: base64.b64encode(str(v).encode()).decode() for k, v in new_cookies.items()}
        secret.update(encoded)

    notify.send(title="Leaflow 自动签到汇总", content="\n".join(results))

if __name__ == "__main__":
    main()
