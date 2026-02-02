import os
import re
import sys
import json
import time
import atexit
import base64
import random
import requests
import datetime
import subprocess

from urllib.parse import urlparse
from playwright.sync_api import sync_playwright
from playwright.sync_api import TimeoutError as PlaywrightTimeoutError

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

# ==================== 配置 ====================
# 代理配置 (留空则不使用)
# 格式: socks5://user:pass@host:port 或 http://user:pass@host:port
PROXY_DSN = os.environ.get("PROXY_DSN", "").strip()

# 固定自己创建有APP的登录入口，若LOGIN_ENTRY_URL = "https://console.run.claw.cloud/signin"在OAuth后会自动跳转到根据IP定位的区域,
BOARD_ENTRY_URL = "https://ap-northeast-1.run.claw.cloud"
LOGIN_ENTRY_URL = f"{BOARD_ENTRY_URL}/signin"
DEVICE_VERIFY_WAIT = 30  # Mobile验证 默认等 30 秒
TWO_FACTOR_WAIT = int(os.environ.get("TWO_FACTOR_WAIT", "120"))  # 2FA验证 默认等 120 秒

# 初始化
_notifier = None
config = None

def get_notifier():
    global _notifier,config
    if config is None:
        config = ConfigReader()
    if _notifier is None:
        _notifier = TelegramNotifier(config)
    return _notifier
# ==================== 工具函数 ====================
def mask_email(email: str):
    if "@" not in email:
        return "***"
    name, domain = email.split("@", 1)
    return f"{name[:2]}***{name[-2:]}@{domain}"

def mask_name(name: str):
    return f"{name[:2]}***{name[-2:]}"


def mask_ip(ip: str):
    return f"***{ip}" if ip else "***"


def mask_password(pwd: str):
    return "*" * 6 + f"({len(pwd)})"

class AutoLogin:
    """自动登录，因 GH_SESSIION 每日更新，不考虑登录github，直接注入GH_SESSIION"""
    
    def __init__(self, config):
        self.host = urlparse(BOARD_ENTRY_URL).netloc
        self.gh_username = config.get('gh_username')
        # self.gh_password = config.get('gh_password')
        
        # gh_session 处理类型安全
        gh_sess = config.get('gh_session', '')
        if isinstance(gh_sess, str):
            self.gh_session = gh_sess.strip()
        elif isinstance(gh_sess, list):
            self.gh_session = gh_sess[0] if gh_sess else ''
        else:
            self.gh_session = ''
        
        # cc_local 处理类型安全
        cc_local_val = config.get('cc_local', '')
        if isinstance(cc_local_val, str):
            self.cc_local = cc_local_val.strip()
        else:
            # storage_state 本身是 dict，无需 strip
            self.cc_local = cc_local_val
        
        self.cc_proxy = config.get('cc_proxy', '').strip() if isinstance(config.get('cc_proxy', ''), str) else config.get('cc_proxy')
        self.proxy_url=test_proxy(self.cc_proxy)
        if not self.proxy_url:
            self.cc_proxy = config.get('wz_proxy')
            
        self.notify = config.get('notify')
        # self.secret = SecretUpdater()
        self.shots = []
        self.logs = []
        self.n = 0
        
        # 区域相关
        self.detected_region = 'ap-northeast-1'  # 检测到的区域，如 "us-west-1"
        self.region_base_url = 'https://ap-northeast-1.run.claw.cloud'  # 检测到的区域基础 URL
        self.auth_token,self.app_token,self.lastLogin=self.get_local_token()

        
    def log(self, msg, level="INFO"):
        icons = {"INFO": "ℹ️", "SUCCESS": "✅", "ERROR": "❌", "WARN": "⚠️", "STEP": "🔹"}
        line = f"{icons.get(level, '•')} {msg}"
        print(line, flush=True)
        self.logs.append(line)
    
    def shot(self, page, name):
        self.n += 1
        f = f"{self.n:02d}_{name}.png"
        try:
            page.screenshot(path=f)
            self.shots.append(f)
        except:
            pass
        return f
    
    def jclick(self, page, sels, desc=""):
        for s in sels:
            try:
                el = page.locator(s).first
                if el.is_visible(timeout=3000):
                    # 模拟人类随机延迟
                    time.sleep(random.uniform(0.5, 1.5))
                    el.hover() # 先悬停
                    time.sleep(random.uniform(0.2, 0.5))
                    el.click()
                    self.log(f"已点击: {desc}", "SUCCESS")
                    return True
            except:
                pass
        return False
    def click(self, page, desc=""):
        """
        专用于 Chakra UI / SPA / iframe 登录按钮
        """
        self.log(f"🔍 尝试查找并点击: {desc}", "INFO")
    
        # 1️⃣ 等页面真正稳定（比 networkidle 更可靠）
        try:
            page.wait_for_load_state("domcontentloaded", timeout=15000)
            page.wait_for_timeout(2000)
        except:
            pass
    
        # 2️⃣ 收集主页面 + 所有 iframe
        frames = [page.main_frame]
        frames += page.frames
    
        selectors = [
            # Chakra Button（最稳）
            'button.chakra-button',
    
            # 带 GitHub svg 的按钮（极稳）
            'button:has(svg)',
    
            # XPath 兜底
            '//button[.//text()[contains(., "GitHub")]]',
            '//button[.//*[name()="svg"]]',
        ]
    
        for frame in frames:
            for sel in selectors:
                try:
                    el = frame.locator(sel).first
    
                    el.wait_for(state="visible", timeout=5000)
    
                    # 模拟人类
                    time.sleep(random.uniform(0.5, 1.2))
                    el.hover()
                    time.sleep(random.uniform(0.2, 0.4))
                    el.click(force=True)
    
                    self.log(f"已点击: {desc}", "SUCCESS")
                    return True
    
                except PlaywrightTimeoutError:
                    self.log(f"• 尝试点击失败: {sel}", "DEBUG")
                except Exception as e:
                    self.log(f"• 点击异常: {sel} -> {e}", "DEBUG")
    
        self.log(f"❌ 找不到按钮: {desc}", "ERROR")
        return False

    def get_clawcloud_cookies(self):
        """
        从 storage_state 中提取 domain 包含 claw.cloud 的 cookies
        """
        if not isinstance(self.cc_local, dict):
            return []
    
        cookies = self.cc_local.get("cookies", [])
        if not isinstance(cookies, list):
            return []
    
        return [
            c for c in cookies
            if isinstance(c, dict) and "domain" in c and "claw.cloud" in c["domain"]
        ]
    def get_local_storage_by_origin(self):
        """
        根据 origin 获取对应的 localStorage
        """
        if not isinstance(self.cc_local, dict):
            return []
    
        origins = self.cc_local.get("origins", [])
        if not isinstance(origins, list):
            return []
    
        for o in origins:
            if not isinstance(o, dict):
                continue
            if self.host in o.get("origin"):
                return o.get("localStorage", [])
    
        return []
    def get_local_token(self):
        local_storage=self.get_local_storage_by_origin()
        # 从localStorage中提取token
        auth_token = None
        app_token = None
        lastLogin=None
        for ls in local_storage:
            if ls.get('name')=='lastLoginUpdateTime':
                lastLogin = ls['value']
                continue
            if ls.get('name')=='session':
                session_data = json.loads(ls['value'])
                if isinstance(session_data, dict) and 'state' in session_data:
                    if 'token' in session_data['state']:
                        auth_token = session_data['state']['token']
                    if 'session' in session_data['state'] and 'token' in session_data['state']['session']:
                        app_token = session_data['state']['session']['token']
                
            

        if not auth_token:
            print(f"❌ [错误] 无法从保存的数据中提取 auth_token")

        if not app_token:
            print(f"❌ [错误] 无法从保存的数据中提取 app_token")
        if not lastLogin:
            print(f"❌ [错误] 无法从保存的数据中提取 lastLoginUpdateTime")
            
        return auth_token,app_token,lastLogin
        
    def start_gost_proxy(self, proxy):
        """
        使用 gost 将 socks5(带认证) 转为本地 http 代理
        """
        listen_port = random.randint(20000, 30000)
        listen = f"http://127.0.0.1:{listen_port}"

        auth = ""
        if proxy.get("username") and proxy.get("password"):
            auth = f"{proxy['username']}:{proxy['password']}@"

        remote = f"socks5://{auth}{proxy['server']}:{proxy['port']}"

        cmd = [
            "./gost",
            "-L", listen,
            "-F", remote
        ]

        self.log(f"启动 Gost，listen: {listen}", "INFO")

        proc = subprocess.Popen(
            cmd,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL
        )

        atexit.register(proc.terminate)

        time.sleep(1.5)  # 给 gost 启动时间
        # ----------------------------
        # 2️⃣ 测试隧道是否可用
        # ----------------------------
        res = requests.get("https://api.ipify.org", proxies={"http": remote, "https": remote}, timeout=15)
        self.log(f"隧道就绪，出口 IP: {res.text.strip()}", "INFO")

        return {
            "server": listen,
            "process": proc
        }

    
    def build_session(self,token):
        cookies=self.get_clawcloud_cookies()
        
        try:
            s = requests.Session()
            s.headers.update({
                    "authority": self.host,
                    "accept": "application/json, text/plain, */*",
                    "authorization": token, # 纯 Token 模式
                    "referer": f"https://{self.host}/",
                    "user-agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/119.0.0.0 Safari/537.36"
            })
                
            for c in cookies:
                s.cookies.set(c["name"], c["value"])
            return s
        except Exception as e:
            print(f"⚠️ [build_session 异常] {e}")
            return None

    def get_balance_with_token(self):
        print(f"📊 [步骤 8] 正在查询余额...")
        proxies = None
        if self.proxy_url:
            proxies = {
                "http": self.proxy_url,
                "https": self.proxy_url
            }
            
            self.log(f"启用代理: {self.cc_proxy['server'][:-3]}***")

                
        session=self.build_session(self.app_token)
        try:
            api_url = f"https://{self.host}/api/accountcenter/creditsUsage"
            print(api_url)
            api_url = f"https://ap-northeast-1.run.claw.cloud/api/accountcenter/creditsUsage"
            for retry in range(2):
                res = session.get(api_url, proxies=proxies, timeout=60)
                res.raise_for_status()
                res_data = res.json()
                print(res_data)
                if res_data.get("code") == 200:
                    plan = res_data["data"]["creditsUsage"]["currentPlan"]
                    total, used = plan["total"] / 1000000, plan["used"] / 1000000
                    result = f"💵  {total:.2f} - 📉  {used:.2f} = 🔋 {total-used:.2f} $"
                    print(result)
                    return result
                if res_data.get("code") == 401:

                    result = f"⚠️  code:{res_data.get('code')} ,message:{res_data.get('message')} "
                    print(result)
                    return result
                print(f"  ⏳ [等待重试] 响应: {res_data.get('message')}")
                time.sleep(5)
            
        except Exception as e:
            print(f"⚠️ [提取异常] {e}")
        return None

    
    def mask_url(self,url):
        url = re.sub(r'code=[^&]+', 'code=***', url)
        url = re.sub(r'state=[^&]+', 'state=***', url)
        return url
        
    def detect_region(self, url):
        """
        从 URL 中检测区域信息
        例如: https://us-west-1.run.claw.cloud/... -> us-west-1
        """
        try:
            parsed = urlparse(url)
            host = parsed.netloc  # 如 "us-west-1.run.claw.cloud"
            
            # 检查是否是区域子域名格式
            # 格式: {region}.run.claw.cloud
            if host.endswith('.run.claw.cloud'):
                region = host.replace('.run.claw.cloud', '')
                if region and region != 'console':  # 排除无效情况
                    self.detected_region = region
                    self.region_base_url = f"https://{host}"
                    self.log(f"检测到区域: {region}", "SUCCESS")
                    self.log(f"区域 URL: {self.region_base_url}", "INFO")
                    return region
            
            # 如果是主域名 console.run.claw.cloud，可能还没跳转
            if 'console.run.claw.cloud' in host or 'claw.cloud' in host:
                # 尝试从路径或其他地方提取区域信息
                # 有些平台可能在路径中包含区域，如 /region/us-west-1/...
                path = parsed.path
                region_match = re.search(r'/(?:region|r)/([a-z]+-[a-z]+-\d+)', path)
                if region_match:
                    region = region_match.group(1)
                    self.detected_region = region
                    self.region_base_url = f"https://{region}.run.claw.cloud"
                    self.log(f"从路径检测到区域: {region}", "SUCCESS")
                    return region
            
            self.log(f"未检测到特定区域，使用当前域名: {host}", "INFO")
            # 如果没有检测到区域，使用当前 URL 的基础部分
            self.region_base_url = f"{parsed.scheme}://{parsed.netloc}"
            return None
            
        except Exception as e:
            self.log(f"区域检测异常: {e}", "WARN")
            return None
    
    def get_base_url(self):
        """获取当前应该使用的基础 URL"""
        if self.region_base_url:
            return self.region_base_url
        return LOGIN_ENTRY_URL
    
    def get_session(self, context):
        """提取 Session Cookie"""
        try:
            for c in context.cookies():
                if c['name'] == 'user_session' and 'github' in c.get('domain', ''):
                    return c['value']
        except:
            pass
        return None
    
    def get_storage(self, context):
        """提取 storage_state"""
        try:
            state = context.storage_state()
            self.cc_local = state
            return state
        except Exception as e:
            self.log(f"获取 storage_state 失败: {e}", "WARN")
            return None

    
    def save_cookie(self, value):
        """保存新 Cookie"""
        if not value:
            return
        
        self.log(f"新 Cookie: {value[:15]}...{value[-8:]}", "SUCCESS")
        
        # 自动更新 Secret
        if self.secret.update('GH_SESSION', value):
            self.log("已自动更新 GH_SESSION", "SUCCESS")
            self.notify.send(title="clawcloud 自动登录保活",content="🔑 <b>Cookie 已自动更新</b>\n\nGH_SESSION 已保存")
        else:
            # 通过 Telegram 发送
            self.notify.send(title="clawcloud 自动登录保活",content=f"""🔑 <b>新 Cookie</b>

请更新 Secret <b>GH_SESSION</b> (点击查看):
<tg-spoiler>{value}</tg-spoiler>
""")
            self.log("已通过 Telegram 发送 Cookie", "SUCCESS")
    
    def wait_device(self, page):
        """等待设备验证"""
        self.log(f"需要设备验证，等待 {DEVICE_VERIFY_WAIT} 秒...", "WARN")
        self.shot(page, "设备验证")
        
        self.notify.send(title="clawcloud 自动登录保活",content=f"""⚠️ <b>需要设备验证</b>

请在 {DEVICE_VERIFY_WAIT} 秒内批准：
1️⃣ 检查邮箱点击链接
2️⃣ 或在 GitHub App 批准""")
        
        if self.shots:
            self.notify.send(title="clawcloud 自动登录保活",content="设备验证页面",image_path=self.shots[-1])
        
        for i in range(DEVICE_VERIFY_WAIT):
            time.sleep(1)
            if i % 5 == 0:
                self.log(f"  等待... ({i}/{DEVICE_VERIFY_WAIT}秒)")
                url = page.url
                if 'verified-device' not in url and 'device-verification' not in url:
                    self.log("设备验证通过！", "SUCCESS")
                    self.notify.send(title="clawcloud 自动登录保活",content="✅ <b>设备验证通过</b>")
                    return True
                try:
                    page.reload(timeout=10000)
                    page.wait_for_load_state('networkidle', timeout=10000)
                except:
                    pass
        
        if 'verified-device' not in page.url:
            return True
        
        self.log("设备验证超时", "ERROR")
        self.notify.send(title="clawcloud 自动登录保活",content="❌ <b>设备验证超时</b>")
        return False
    
    def wait_two_factor_mobile(self, page):
        """等待 GitHub Mobile 两步验证批准，并把数字截图提前发到电报"""
        self.log(f"需要两步验证（GitHub Mobile），等待 {TWO_FACTOR_WAIT} 秒...", "WARN")
        
        # 先截图并立刻发出去（让你看到数字）
        shot = self.shot(page, "两步验证_mobile")
        self.notify.send(title="clawcloud 自动登录保活",content=f"""⚠️ <b>需要两步验证（GitHub Mobile）</b>

请打开手机 GitHub App 批准本次登录（会让你确认一个数字）。
等待时间：{TWO_FACTOR_WAIT} 秒""")
        if shot:
            self.notify.send(title="clawcloud 自动登录保活",content="两步验证页面（数字在图里）",mage_path=shot)
        
        # 不要频繁 reload，避免把流程刷回登录页
        for i in range(TWO_FACTOR_WAIT):
            time.sleep(1)
            
            url = page.url
            
            # 如果离开 two-factor 流程页面，认为通过
            if "github.com/sessions/two-factor/" not in url:
                self.log("两步验证通过！", "SUCCESS")
                self.notify.send(title="clawcloud 自动登录保活",content="✅ <b>两步验证通过</b>")
                return True
            
            # 如果被刷回登录页，说明这次流程断了（不要硬等）
            if "github.com/login" in url:
                self.log("两步验证后回到了登录页，需重新登录", "ERROR")
                return False
            
            # 每 10 秒打印一次，并补发一次截图（防止你没看到数字）
            if i % 10 == 0 and i != 0:
                self.log(f"  等待... ({i}/{TWO_FACTOR_WAIT}秒)")
                shot = self.shot(page, f"两步验证_{i}s")
                if shot:
                    self.notify.send(title="clawcloud 自动登录保活",content=f"两步验证页面（第{i}秒）",image_path=shot)
            
            # 只在 30 秒、60 秒... 做一次轻刷新（可选，频率很低）
            if i % 30 == 0 and i != 0:
                try:
                    page.reload(timeout=30000)
                    page.wait_for_load_state('domcontentloaded', timeout=30000)
                except:
                    pass
        
        self.log("两步验证超时", "ERROR")
        self.notify.send(title="clawcloud 自动登录保活",content="❌ <b>两步验证超时</b>")
        return False
    
    def handle_2fa_code_input(self, page):
        """处理 TOTP 验证码输入（通过 Telegram 发送 /code 123456）"""
        self.log("需要输入验证码", "WARN")
        shot = self.shot(page, "两步验证_code")

        # 如果是 Security Key (webauthn) 页面，尝试切换到 Authenticator App
        if 'two-factor/webauthn' in page.url:
            self.log("检测到 Security Key 页面，尝试切换...", "INFO")
            try:
                # 点击 "More options"
                more_options_button = page.locator('button:has-text("More options")').first
                if more_options_button.is_visible(timeout=3000):
                    more_options_button.click()
                    self.log("已点击 'More options'", "SUCCESS")
                    time.sleep(1) # 等待菜单出现
                    self.shot(page, "点击more_options后")

                    # 点击 "Authenticator app"
                    auth_app_button = page.locator('button:has-text("Authenticator app")').first
                    if auth_app_button.is_visible(timeout=2000):
                        auth_app_button.click()
                        self.log("已选择 'Authenticator app'", "SUCCESS")
                        time.sleep(2)
                        page.wait_for_load_state('networkidle', timeout=15000)
                        shot = self.shot(page, "切换到验证码输入页") # 更新截图
            except Exception as e:
                self.log(f"切换验证方式时出错: {e}", "WARN")

        # (保留) 先尝试点击"Use an authentication app"或类似按钮（如果在 mobile 页面）
        try:
            more_options = [
                'a:has-text("Use an authentication app")',
                'a:has-text("Enter a code")',
                'button:has-text("Use an authentication app")',
                'button:has-text("Authenticator app")',
                '[href*="two-factor/app"]'
            ]
            for sel in more_options:
                try:
                    el = page.locator(sel).first
                    if el.is_visible(timeout=2000):
                        el.click()
                        time.sleep(2)
                        page.wait_for_load_state('networkidle', timeout=15000)
                        self.log("已切换到验证码输入页面", "SUCCESS")
                        shot = self.shot(page, "两步验证_code_切换后")
                        break
                except:
                    pass
        except:
            pass

        # 发送提示并等待验证码
        self.notify.send(title="clawcloud 自动登录保活",content=f"""🔐 <b>需要验证码登录</b>

用户{self.gh_username}正在登录，请在 Telegram 里发送：
<code>/code 你的6位验证码</code>

等待时间：{TWO_FACTOR_WAIT} 秒""")
        if shot:
            self.notify.send(title="clawcloud 自动登录保活",content="两步验证页面",image_path=shot)

        self.log(f"等待验证码（{TWO_FACTOR_WAIT}秒）...", "WARN")
        code = '真需要的话使用库自动生成'

        if not code:
            self.log("等待验证码超时", "ERROR")
            self.notify.send(title="clawcloud 自动登录保活",content="❌ <b>等待验证码超时</b>")
            return False

        # 不打印验证码明文，只提示收到
        self.log("收到验证码，正在填入...", "SUCCESS")
        self.notify.send(title="clawcloud 自动登录保活",content="✅ 收到验证码，正在填入...")

        # 常见 OTP 输入框 selector（优先级排序）
        selectors = [
            'input[autocomplete="one-time-code"]',
            'input[name="app_otp"]',
            'input[name="otp"]',
            'input#app_totp',
            'input#otp',
            'input[inputmode="numeric"]'
        ]

        for sel in selectors:
            try:
                el = page.locator(sel).first
                if el.is_visible(timeout=2000):
                    el.click()
                    time.sleep(random.uniform(0.2, 0.5))
                    el.type(code, delay=random.randint(50, 150))
                    self.log(f"已填入验证码", "SUCCESS")
                    time.sleep(1)

                    # 优先点击 Verify 按钮，不行再 Enter
                    submitted = False
                    verify_btns = [
                        'button:has-text("Verify")',
                        'button[type="submit"]',
                        'input[type="submit"]'
                    ]
                    for btn_sel in verify_btns:
                        try:
                            btn = page.locator(btn_sel).first
                            if btn.is_visible(timeout=1000):
                                btn.click()
                                submitted = True
                                self.log("已点击 Verify 按钮", "SUCCESS")
                                break
                        except:
                            pass

                    if not submitted:
                        time.sleep(random.uniform(0.3, 0.8))
                        page.keyboard.press("Enter")
                        self.log("已按 Enter 提交", "SUCCESS")

                    time.sleep(3)
                    page.wait_for_load_state('networkidle', timeout=30000)
                    self.shot(page, "验证码提交后")

                    # 检查是否通过
                    if "github.com/sessions/two-factor/" not in page.url:
                        self.log("验证码验证通过！", "SUCCESS")
                        self.notify.send(title="clawcloud 自动登录保活",content="✅ <b>验证码验证通过</b>")
                        return True
                    else:
                        self.log("验证码可能错误", "ERROR")
                        self.notify.send(title="clawcloud 自动登录保活",content="❌ <b>验证码可能错误，请检查后重试</b>")
                        return False
            except:
                pass

        self.log("没找到验证码输入框", "ERROR")
        self.notify.send(title="clawcloud 自动登录保活",content="❌ <b>没找到验证码输入框</b>")
        return False
    
    def login_github(self, page, context):
        """登录 GitHub"""
        self.log("登录 GitHub...", "STEP")
        self.shot(page, "github_登录页")
        
        try:
            # 模拟人工输入
            user_input = page.locator('input[name="login"]')
            user_input.click()
            time.sleep(random.uniform(0.3, 0.8))
            user_input.type(self.gh_username, delay=random.randint(30, 100))

            time.sleep(random.uniform(0.5, 1.0))

            pass_input = page.locator('input[name="password"]')
            pass_input.click()
            time.sleep(random.uniform(0.3, 0.8))
            pass_input.type(self.password, delay=random.randint(30, 100))

            self.log("已输入凭据")
        except Exception as e:
            self.log(f"输入失败: {e}", "ERROR")
            return False
        
        self.shot(page, "github_已填写")
        
        try:
            page.locator('input[type="submit"], button[type="submit"]').first.click()
        except:
            pass
        
        time.sleep(3)
        page.wait_for_load_state('networkidle', timeout=30000)
        self.shot(page, "github_登录后")
        
        url = page.url
        self.log(f"当前: {url}")
        
        # 设备验证
        if 'verified-device' in url or 'device-verification' in url:
            if not self.wait_device(page):
                return False
            time.sleep(2)
            page.wait_for_load_state('networkidle', timeout=30000)
            self.shot(page, "验证后")
        
        # 2FA
        if 'two-factor' in page.url:
            self.log("需要两步验证！", "WARN")
            self.shot(page, "两步验证")
            
            # GitHub Mobile：等待你在手机上批准
            if 'two-factor/mobile' in page.url:
                if not self.wait_two_factor_mobile(page):
                    return False
                # 通过后等页面稳定
                try:
                    page.wait_for_load_state('networkidle', timeout=30000)
                    time.sleep(2)
                except:
                    pass
            
            else:
                # 其它两步验证方式（TOTP/恢复码等），尝试通过 Telegram 输入验证码
                if not self.handle_2fa_code_input(page):
                    return False
                # 通过后等页面稳定
                try:
                    page.wait_for_load_state('networkidle', timeout=30000)
                    time.sleep(2)
                except:
                    pass
        
        # 错误
        try:
            err = page.locator('.flash-error').first
            if err.is_visible(timeout=2000):
                self.log(f"错误: {err.inner_text()}", "ERROR")
                return False
        except:
            pass
        
        return True
    
    def oauth(self, page):
        """处理 OAuth"""
        if 'github.com/login/oauth/authorize' in page.url:
            self.log("处理 OAuth...", "STEP")
            self.shot(page, "oauth")
            self.jclick(page, ['button[name="authorize"]', 'button:has-text("Authorize")'], "授权")
            time.sleep(3)
            page.wait_for_load_state('networkidle', timeout=30000)
    
    def wait_redirect(self, page, wait=60):
        """等待重定向并检测区域"""
        self.log("等待重定向...", "STEP")
        for i in range(wait):
            url = page.url
            
            # 检查是否已跳转到 claw.cloud
            if 'claw.cloud' in url and 'signin' not in url.lower():
                self.log("重定向成功！", "SUCCESS")
                
                # 检测并记录区域
                self.detect_region(url)
                
                return True
            
            if 'github.com/login/oauth/authorize' in url:
                self.oauth(page)
            
            time.sleep(1)
            if i % 10 == 0:
                self.log(f"  等待... ({i}秒)")
        
        self.log("重定向超时", "ERROR")
        return False
    
    def keepalive(self, page):
        """保活 - 使用检测到的区域 URL"""
        self.log("保活...", "STEP")
        
        # 使用检测到的区域 URL，如果没有则使用默认
        base_url = self.get_base_url()
        self.log(f"使用区域 URL: {base_url}", "INFO")
        
        pages_to_visit = [
            (f"{base_url}/", "控制台"),
            (f"{base_url}/apps", "应用"),
        ]
        
        # 如果检测到了区域，可以额外访问一些区域特定页面
        if self.detected_region:
            self.log(f"当前区域: {self.detected_region}", "INFO")
        
        for url, name in pages_to_visit:
            try:
                page.goto(url, timeout=30000)
                page.wait_for_load_state('networkidle', timeout=15000)
                self.log(f"已访问: {name} ({url})", "SUCCESS")
                
                # 再次检测区域（以防中途跳转）
                current_url = page.url
                if 'claw.cloud' in current_url:
                    self.detect_region(current_url)
                
                time.sleep(2)
            except Exception as e:
                self.log(f"访问 {name} 失败: {e}", "WARN")
        
        self.shot(page, "完成")

    def check_and_process_domain(self, domain):

        # 去掉末尾斜杠
        domain = domain.rstrip('/')
    
        # 检查是否为 signin 页面
        if domain.endswith('.run.claw.cloud/signin'):
            return "signin"
    
        # 检查是否包含 callback（OAuth 重定向）
        if "callback" in domain:
            return "redirect"
    
        # 检查是否是正常已登录的区域域名
        if domain.endswith('.run.claw.cloud'):
            return "logged"
    
        # 其他情况
        return "invalid"
    
    def run(self):
        print("\n" + "="*50)
        print("🚀 ClawCloud 自动登录")
        print("="*50 + "\n")
        ok, new_local,msg = False,  None, f"🚀 ClawCloud 自动登录\n"
        self.log(f"用户名: {self.gh_username}")
        self.log(f"Session: {'有' if self.gh_session else '无'}")
        #self.log(f"密码: {'有' if self.password else '无'}")
        self.log(f"登录入口: {LOGIN_ENTRY_URL}")
        
        if not self.gh_username: #or not self.password:
            self.log("缺少凭据", "ERROR")
           
            return False,  None, f"❌ 缺少凭据"
                    
        with sync_playwright() as p:
            # 代理配置解析
            launch_args = {
                "headless": True,
                "args": [
                    '--no-sandbox',
                    '--disable-blink-features=AutomationControlled',
                    '--disable-infobars',
                    '--exclude-switches=enable-automation',
                ]
            }

            if self.cc_proxy:
                try:
                    p_url = self.cc_proxy
                    # ===== 新增：socks5 带认证 → gost =====
                    if (
                        p_url.get("type") == "socks5"
                        and p_url.get("username")
                        and p_url.get("password")
                    ):
                        gost = self.start_gost_proxy(p_url)
                        launch_args["proxy"] = {
                            "server": gost["server"]
                        }
                        self.log(f"使用 Gost 本地代理: {gost['server']}", "SUCCESS")

                    else:
                        proxy_config = {
                            "server": f"{p_url['type']}://{p_url['server']}:{p_url['port']}"
                        }
                        launch_args["proxy"] = proxy_config
                        self.log(f"启用代理: {proxy_config['server'][:-6]}")

                except Exception as e:
                    self.log(f"代理配置解析失败: {e}", "ERROR")

            """
            与当前时间比较，是否相差 >= 20 天
            ts_ms: 毫秒时间戳
            """
            lastLogin=int(self.lastLogin)
            now_ms = int(time.time() * 1000)
            diff_ms = abs(now_ms - lastLogin)
        
            DAY_MS = 24 * 60 * 60 * 1000
            dt = (
                datetime.datetime.utcfromtimestamp(lastLogin / 1000)
                + datetime.timedelta(hours=8)
            ).replace(second=0, microsecond=0)
            if diff_ms >= 10 * DAY_MS:
                self.log(f"上次登录{dt},已过10天，重新登录！", "WARN")
                msg+= f"上次登录{dt},已过10天，重新登录！"
            else:
                self.log(f"上次登录{dt}！", "INFO")
                msg+=f"上次登录{dt}\n "
                #msg+=self.get_balance_with_token()#七天有效期，失效无法查询
                return True, None,msg
                
                
            
            browser = p.chromium.launch(**launch_args)
            
            
            context = browser.new_context(
                storage_state=self.cc_local,
                viewport={'width': 1920, 'height': 1080},
                user_agent='Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36'
            )
            
            page = context.new_page()
            page.add_init_script("""
                // 基础反检测
                Object.defineProperty(navigator, 'webdriver', {
                    get: () => undefined
                });

                // 模拟插件 (Headless Chrome 默认无插件)
                Object.defineProperty(navigator, 'plugins', {
                    get: () => [1, 2, 3, 4, 5]
                });

                // 模拟语言
                Object.defineProperty(navigator, 'languages', {
                    get: () => ['en-US', 'en']
                });

                // 模拟 window.chrome
                window.chrome = { runtime: {} };

                // 绕过权限检测
                const originalQuery = window.navigator.permissions.query;
                window.navigator.permissions.query = (parameters) => (
                    parameters.name === 'notifications' ?
                    Promise.resolve({ state: Notification.permission }) :
                    originalQuery(parameters)
                );
            """)
            try:
                # 预加载 加载gh_session
                if self.gh_session:
                    try:
                        context.add_cookies([
                            {'name': 'user_session', 'value': self.gh_session, 'domain': 'github.com', 'path': '/'},
                            {'name': 'logged_in', 'value': 'yes', 'domain': 'github.com', 'path': '/'}
                        ])
                        self.log("已加载 Session Cookie", "SUCCESS")
                    except:
                        self.log("加载 Cookie 失败", "WARN")
                        
                # 1. 访问 ClawCloud 登录入口
                self.log("步骤1: 打开 ClawCloud 登录页", "STEP")

                for i in range(10):
                    try:
                        page.goto(BOARD_ENTRY_URL, timeout=60000)
                        page.wait_for_load_state('networkidle', timeout=60000)
                        resault=self.check_and_process_domain(page.url)
                        self.shot(page, "找不到 GitHub 按钮")
                        if resault=="invalid":
                            self.log(f"[1.{i}]: 非域名: {page.url}", "WARN")
                            continue
                        if resault=="logged":
                            self.log(f"[1.{i}]: 已登录: {page.url}", "SUCCESS")
                            break
                        if resault=="signin":
                            self.log(f"[1.{i}]: 需登录: {page.url}", "INFO")
                            # 步骤2: 点击 GitHub
                            self.log(f"步骤2: 点击 GitHub", "STEP")
                            if not self.click(page, desc="GitHub 登录按钮"):
                                shot = self.shot(page, "找不到 GitHub 按钮")
                                if shot:
                                    self.notify.send(title="clawcloud 自动登录保活",content="找不到 GitHub 按钮",image_path=shot)
                                self.log(f"[2.{i}]: 找不到 GitHub 按钮", "WARN")
                                self.shot(page, "找不到 GitHub 按钮")
                                continue
                            else:
                                for j in range(10):
                                    resault=self.check_and_process_domain(page.url)
                                    if resault=="logged":
                                        self.log(f"[2.{i}.{j}]: 已登录: {page.url}", "SUCCESS")
                                        self.shot(page, "找不到 GitHub 按钮")
                                        break
                                    if resault=="redirect":
                                        self.log(f"[2.{i}.{j}]: 正在重定向: {self.mask_url(page.url)}", "INFO")
                                        try:
                                            page.wait_for_url("https://*.run.claw.cloud", timeout=60000)
                                            self.log(f"URL 已跳转: {page.url}", "SUCCESS")
                                            self.shot(page, "找不到 GitHub 按钮")
                                            break
                                        except PlaywrightTimeoutError:
                                            self.log(f"等待 URL 跳转超时: {page.url}", "ERROR")
                                            self.shot(page, "找不到 GitHub 按钮")
                                        continue
                                    if "github.com/login" in page.url:
                                        self.log(f"[2.{i}.{j}]: github登录过期，{page.url}", "ERROR")
                                        self.shot(page, "找不到 GitHub 按钮")
                                        return False,  None, f"github登录过期！"   
                    except:
                        if i <10:
                            self.log(f"[1.{i}]: 未打开登录页，重试", "WARN")
                            time.sleep(random.uniform(10, 15))
                        else:
                            self.log(f"[1.{i}]: 访问 {page.url} 失败！", "ERROR")
                            browser.close()
                            return False,  None, f"访问 {BOARD_ENTRY_URL} 失败！"   
                    
        
                # 检测区域
                self.detect_region(page.url)
                
                # 再次确认区域检测
                if not self.detected_region:
                    self.detect_region(current_url)
                self.shot(page, "找不到 GitHub 按钮")

                
                # 3. 提取并保存新 local_storage
                self.log("步骤3: 更新 local_storage", "STEP")
                storage_state = self.get_storage(context)
                if storage_state:
                    #print_dict_tree(storage_state)
                    storage_state_json = json.dumps(storage_state, ensure_ascii=False)
                    self.cc_local=storage_state_json
                    storage_state_b64 = base64.b64encode(storage_state_json.encode("utf-8")).decode("utf-8")
                    #print(f"STORAGE_STATE_B64={storage_state_b64}")
                    ok=True
                    new_local=storage_state_b64
                else:
                    self.log("未获取到 storage_state", "WARN")
                
                # 4. 查询余额和登录信息
                self.log("步骤4: 查询余额和登录信息", "STEP")
                msg+=self.get_balance_with_token()
                #msg+= "✅ 成功！"
                print("\n" + "="*50)
                print("✅ 成功！")
                if self.detected_region:
                    print(f"📍 区域: {self.detected_region}")
                print("="*50 + "\n")
                if self.shots:
                    self.notify.send(title="clawcloud 自动登录保活",content=f"✅ {self.gh_username}成功！",image_path=self.shots[-1])
            except Exception as e:
                self.log(f"异常: {e}", "ERROR")
                self.shot(page, "异常")
                import traceback
                traceback.print_exc()
                if self.shots:
                    self.notify.send(title="clawcloud 自动登录保活",content=f"❌ {self.gh_username}:{str(e)}",image_path=self.shots[-1])
                msg= f"访问 {page.url} 失败！"   
            finally:
                if browser:
                    browser.close()
                return ok, new_local,msg

def main():
    global config
    if config is None:
        config = ConfigReader()
    useproxy = True
    newcookies={}
    results = []

    # 读取账号信息
    accounts = config.get_value("GH_INFO")
    
    # 读取代理信息
    proxies = config.get_value("PROXY_INFO")

    # 初始化 get_notifier
    notify=get_notifier()
    # 初始化 SecretUpdater，会自动根据当前仓库用户名获取 token
    gh_secret = SecretUpdater("GH_SESSION", config_reader=config)
    # 读取
    gh_sessions = gh_secret.load() or {}
    
    # 初始化 SecretUpdater，会自动根据当前仓库用户名获取 token
    secret = SecretUpdater("CLAWCLOUD_LOCALS", config_reader=config)
    # 读取
    cc_locals = secret.load() or {}
    

    if not accounts:
        print("❌ 错误: 未配置 LEAFLOW_ACCOUNTS")
        return
    if not proxies:
        print("📢 警告: 未配置 proxy ，将直连")
        useproxy = False

    print(f"📊 检测到 {len(accounts)} 个账号和 {len(proxies)} 个代理")

    # 使用 zip 实现一一对应
    for account, proxy  in zip(accounts, proxies):
        username=account['username']

        print(f"\n🚀 开始处理账号: {mask_name(username)}\n  🌐 使用代理: {proxy['server'][:-4]}***\n")
        results.append(f"🚀 账号：{mask_name(username)}\n    🌐 使用代理: {proxy['server'][:-4]}***\n")
        cc_info={}
        cc_info['gh_username'] = username
        #cc_info['gh_password'] = account.get('password')
        cc_info['cc_proxy'] = proxy
        cc_info['notify'] = notify
        cc_info['wz_proxy'] = proxies[-1]

        if isinstance(gh_sessions, dict):
            gh_session = gh_sessions.get(username,'')
            if isinstance(gh_session, list):
                gh_session = gh_session[0] if gh_session else ''
            cc_info['gh_session'] = gh_session
        else:
            print(f"⚠️ gh_sessions 格式错误！")
            cc_info['gh_session'] = ''

        if not gh_session:
            print(f"⚠️ 缺少对应账号的 gh_session ，退出！")
            continue
        
        if isinstance(cc_locals, dict):
            # 预加载localStorage数据，验证有效不再使用 gh_session
            storage_state = None
            cc_local=cc_locals.get(username,'')
            if cc_local:
                try:
                    cc_info['cc_local'] =  json.loads(base64.b64decode(cc_local).decode("utf-8"))
                    print("✅ 已加载 storage_state")
                except Exception as e:
                    print(f"❌ 加载 storage_state 失败: {e}")
        else:
            print(f"⚠️ cc_locals 格式错误！")
            cc_info['cc_local'] = []

        try:

            auto_login= AutoLogin(cc_info)
            ok, new_local,msg = auto_login.run()
    
            if ok:
                print(f"    ✅ 执行成功")
                results.append(f"    ✅ {msg}")
                if new_local:
                    print(f"    ✅ 保存新 new_local")
                    cc_locals[username]=new_local
            else:
                print(f"    ⚠️ 执行失败，不保存 cookie")
                results.append(f"    ⚠️ 执行失败:{msg}")
    
        except Exception as e:
            print(f"    ❌ 执行异常: {e}")
            results.append(f"    ❌ 执行异常: {e}")
        #break
    # 写入
    secret.update(cc_locals)
    # 发送结果
    notify.send(
        title="clawcloud 自动登录保活汇总",
        content="\n".join(results)
    )


if __name__ == "__main__":
    main()
