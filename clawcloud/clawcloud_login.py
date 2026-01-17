import os
import sys
import time
import requests
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
        def update(self, value): return False

# ==================== 配置 ====================
SIGNIN_URL = "https://console.run.claw.cloud/signin"

def send_tg_photo(token, chat_id, photo_path, caption):
    """发送截图到 Telegram"""
    if not token or not chat_id:
        return
    try:
        url = f"https://api.telegram.org/bot{token}/sendPhoto"
        with open(photo_path, 'rb') as f:
            requests.post(url, data={'chat_id': chat_id, 'caption': caption}, files={'photo': f}, timeout=30)
    except Exception as e:
        print(f"❌ 发送 TG 截图失败: {e}")

def main():
    config = ConfigReader()
    # 读取环境变量和配置
    gh_session = os.environ.get("GH_SESSION")
    bots = config.get_value("BOT_INFO") or [{}]
    bot_info = bots[0] if isinstance(bots, list) else bots
    tg_token = bot_info.get('token')
    tg_chat_id = bot_info.get('id')

    if not gh_session:
        print("❌ 错误: 未找到 GH_SESSION")
        return

    secret_manager = SecretUpdater("CLAW_COOKIE", config_reader=config)
    
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True, args=['--no-sandbox'])
        context = browser.new_context(viewport={'width': 1280, 'height': 800})
        
        # 注入 GitHub Session
        context.add_cookies([{'name': 'user_session', 'value': gh_session, 'domain': 'github.com', 'path': '/'}])
        page = context.new_page()
        
        status_msg = "未知状态"
        shot_path = "last_screen.png"

        try:
            print(f"🚀 访问 Claw Cloud...")
            page.goto(SIGNIN_URL, timeout=60000)
            time.sleep(3)

            if "github.com/login" in page.url:
                status_msg = "⚠️ Session 失效，停留在 GitHub 登录页"
                print(status_msg)
                return

            if "/signin" in page.url:
                page.locator('button:has-text("GitHub"), [data-provider="github"]').first.click()
                print("⏳ 等待 OAuth 重定向...")
                
            # 等待跳出 callback 进入主页
            success = False
            for _ in range(15):
                if "claw.cloud" in page.url and "callback" not in page.url and "signin" not in page.url:
                    success = True
                    break
                time.sleep(1)

            if success:
                status_msg = f"✅ 登录成功: {page.url}"
                cookies = context.cookies()
                claw_cookies = [f"{c['name']}={c['value']}" for c in cookies if "claw.cloud" in c['domain']]
                cookie_str = "; ".join(claw_cookies)
                if cookie_str:
                    secret_manager.update(cookie_str)
                    status_msg += "\n✅ Cookie 已更新"
            else:
                status_msg = f"❌ 登录超时或失败，当前 URL: {page.url}"
            
            print(status_msg)

        except Exception as e:
            status_msg = f"❌ 运行异常: {str(e)}"
            print(status_msg)
        finally:
            # 无论成功失败，执行截图并发送 TG
            try:
                page.screenshot(path=shot_path, full_page=True)
                print(f"📸 截图已保存: {shot_path}")
                send_tg_photo(tg_token, tg_chat_id, shot_path, status_msg)
            except:
                print("❌ 无法截取屏幕")
            browser.close()

if __name__ == "__main__":
    main()
