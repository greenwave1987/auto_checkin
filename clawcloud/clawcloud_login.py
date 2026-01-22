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
# 代理配置 (留空则不使用)
# 格式: socks5://user:pass@host:port 或 http://user:pass@host:port
PROXY_DSN = os.environ.get("PROXY_DSN", "").strip()

# 固定自己创建有APP的登录入口，若SIGNIN_URL = "https://console.run.claw.cloud/signin"在OAuth后会自动跳转到根据IP定位的区域,
LOGIN_ENTRY_URL = "https://ap-northeast-1.run.claw.cloud/login"
SIGNIN_URL = f"{LOGIN_ENTRY_URL}/signin"
DEVICE_VERIFY_WAIT = 30  # Mobile验证 默认等 30 秒
TWO_FACTOR_WAIT = int(os.environ.get("TWO_FACTOR_WAIT", "120"))  # 2FA验证 默认等 120 秒



def main():
    config = ConfigReader()
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
        # 1. 使用固定的 User-Agent 和特定的启动参数避开检测
        browser = p.chromium.launch(headless=True, args=[
            '--no-sandbox', 
            '--disable-setuid-sandbox',
            '--disable-blink-features=AutomationControlled' # 隐藏自动化特征
        ])
        
        # 2. 设置更像真实用户的上下文
        context = browser.new_context(
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
            viewport={'width': 1920, 'height': 1080},
            locale="en-US"
        )
        
        # 3. 注入 GitHub Session
        context.add_cookies([{'name': 'user_session', 'value': gh_session, 'domain': 'github.com', 'path': '/'}])
        page = context.new_page()
        
        status_msg = ""
        shot_path = "error_debug.png"

        try:
            print(f"🚀 访问 Claw Cloud 登录入口...")
            # 使用 wait_until="commit" 快速响应，避免因 Region Error 导致的无限等待
            page.goto(SIGNIN_URL, wait_until="domcontentloaded", timeout=60000)
            time.sleep(5)

            # 4. 核心逻辑：检测是否直接遇到了 REGION_NOT_AVAILABLE
            if page.locator("text=REGION_NOT_AVAILABLE").is_visible():
                print("❌ 检测到 REGION_NOT_AVAILABLE 报错。尝试刷新页面强制重分配...")
                page.reload()
                time.sleep(5)

            # 5. 执行 GitHub 登录点击
            if "/signin" in page.url:
                print("🔹 点击 GitHub 登录按钮...")
                page.locator('button:has-text("GitHub"), [data-provider="github"]').first.click()
                
            # 6. 监控 URL 变化，直到进入具体的区域子域名
            print("⏳ 等待重定向至区域控制台...")
            success = False
            for _ in range(20):
                curr_url = page.url
                if 'cf_chl_rt_tk' in curr_url:
                    print(f"触发人机验证:{curr_url}")
                    time.sleep(5)
                # 成功的标准：包含 claw.cloud 且不是 signin/callback/login 页面
                if "claw.cloud" in curr_url and all(x not in curr_url for x in ["signin", "callback", "login"]):
                    success = True
                    break
                # 如果中途再次弹出错误弹窗，尝试点击关闭或再次点击登录
                if page.locator(".ant-notification-notice-message:has-text('Error')").is_visible():
                     page.locator(".ant-notification-notice-close").first.click()
                     time.sleep(1)
                time.sleep(2)

            if success:
                # 7. 提取 Cookie
                cookies = context.cookies()
                # 这里的域名过滤要放宽，捕获所有相关节点的 cookie
                claw_cookies = [f"{c['name']}={c['value']}" for c in cookies if "claw.cloud" in c['domain']]
                cookie_str = "; ".join(claw_cookies)
                
                if cookie_str:
                    secret_manager.update(cookie_str)
                    status_msg = f"✅ 登录成功！区域: {page.url.split('.')[0].replace('https://','')}"
                else:
                    status_msg = "❌ 登录成功但未提取到 Cookie"
            else:
                status_msg = f"❌ 登录失败，最终停留在: {page.url}"
            
            print(status_msg)

        except Exception as e:
            status_msg = f"❌ 运行异常: {str(e)}"
            print(status_msg)
        finally:
            # 截图并发送 TG
            page.screenshot(path=shot_path)
            if tg_token and tg_chat_id:
                try:
                    url = f"https://api.telegram.org/bot{tg_token}/sendPhoto"
                    with open(shot_path, 'rb') as f:
                        requests.post(url, data={'chat_id': tg_chat_id, 'caption': status_msg}, files={'photo': f}, timeout=30)
                except: pass
            browser.close()

if __name__ == "__main__":
    main()
