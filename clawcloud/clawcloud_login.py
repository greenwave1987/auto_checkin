import os
import sys
import time
from urllib.parse import urlparse
from playwright.sync_api import sync_playwright

# ==================== 基准数据对接 ====================
BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BASE_DIR)
try:
    from engine.main import ConfigReader, SecretUpdater
except ImportError:
    class ConfigReader:
        def get_value(self, key): return os.environ.get(key)
    class SecretUpdater:
        def __init__(self, name=None, config_reader=None): pass
        def update(self, name, value): return False

# ==================== 配置 ====================
SIGNIN_URL = "https://console.run.claw.cloud/signin"

def main():
    config = ConfigReader()
    # 1. 从环境变量读取 GH_SESSION (GitHub 登录凭证)
    gh_session = os.environ.get("GH_SESSION")
    if not gh_session:
        print("❌ 错误: 环境变量中未找到 GH_SESSION，无法执行")
        return

    # 初始化更新器，准备更新 CLAW_COOKIE
    secret_manager = SecretUpdater("CLAW_COOKIE", config_reader=config)

    with sync_playwright() as p:
        # 不使用代理启动
        browser = p.chromium.launch(headless=True, args=['--no-sandbox'])
        context = browser.new_context(viewport={'width': 1280, 'height': 720})
        
        # 2. 注入 GitHub Session
        context.add_cookies([{
            'name': 'user_session', 
            'value': gh_session, 
            'domain': 'github.com', 
            'path': '/'
        }])

        page = context.new_page()
        try:
            print(f"🚀 正在尝试通过 Session 登录 Claw Cloud...")
            page.goto(SIGNIN_URL, timeout=60000)
            time.sleep(3)

            # 3. 检查是否跳到了 GitHub 登录页
            if "github.com/login" in page.url:
                print("⚠️ Session 已失效，GitHub 要求重新登录。正在退出...")
                return

            # 如果在登录页，点击 GitHub 按钮触发 OAuth
            if "/signin" in page.url:
                page.locator('button:has-text("GitHub"), [data-provider="github"]').first.click()
                time.sleep(8)

            # 4. 获取 Claw Cloud 重定向后的 Cookie
            if "claw.cloud" in page.url and "signin" not in page.url:
                print(f"✅ 登录成功，当前 URL: {page.url}")
                
                # 提取 claw.cloud 的所有 cookies 并拼成字符串
                cookies = context.cookies()
                claw_cookies = [f"{c['name']}={c['value']}" for c in cookies if "claw.cloud" in c['domain']]
                cookie_str = "; ".join(claw_cookies)

                if cookie_str:
                    # 5. 只上传更新最后完成重定向的 claw_cookie
                    if secret_manager.update("CLAW_COOKIE", cookie_str):
                        print("✅ 已成功更新 CLAW_COOKIE 至环境变量")
                    else:
                        print("❌ CLAW_COOKIE 更新失败")
            else:
                print(f"❌ 最终状态校验失败，停留在: {page.url}")

        except Exception as e:
            print(f"❌ 运行异常: {str(e)}")
        finally:
            browser.close()

if __name__ == "__main__":
    main()
