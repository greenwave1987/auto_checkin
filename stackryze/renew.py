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
from urllib.parse import quote

from urllib.parse import urlparse
from playwright.sync_api import sync_playwright
from playwright.sync_api import TimeoutError as PlaywrightTimeoutError

# ==================== 基准数据对接 ====================
TIMEOUT = 15
BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BASE_DIR)
try:
    from engine.notify import TelegramNotifier
except ImportError:
    # 独立运行兜底：无 engine 包时不推送
    class TelegramNotifier:
        def __init__(self, config=None): pass
        def send(self, title="", content="", image_path=None): pass

try:
    from engine.main import ConfigReader, SecretUpdater, print_dict_tree, test_proxy
except ImportError:
    class ConfigReader:
        def get_value(self, key): return os.environ.get(key)
    class SecretUpdater:
        def __init__(self, name=None, config_reader=None): pass
        def load(self): return {}
        def update(self, value): return False
    def test_proxy(proxy):
        """兜底：不做连通性检测，直接返回原值（dict 或 DSN 字符串）"""
        return proxy if proxy else None

# ==================== 配置 ====================
# 代理配置 (留空则不使用)
# 格式: socks5://user:pass@host:port 或 http://user:pass@host:port
PROXY_DSN = os.environ.get("PROXY_DSN", "").strip()

# 登录入口（点 SSO 后会跳 auth.stackryze.com 再 OAuth GitHub）
BOARD_ENTRY_URL = "https://domain.stackryze.com/login"

# SSO 按钮文本（<span>Continue with Stackryze SSO</span>）
SSO_BTN_TEXT = "Continue with Stackryze SSO"
# auth.stackryze.com 登录页（authRequest=V2_xxx，id 不固定，正则匹配）
AUTH_LOGIN_RE = re.compile(r"^https://auth\.stackryze\.com/en/login\?authRequest=V2_\d+")

# 登录成功判定：回跳 domain.stackryze.com 且不在 /login 页
APP_HOST = "domain.stackryze.com"

DEVICE_VERIFY_WAIT = 30  # Mobile验证 默认等 30 秒
TWO_FACTOR_WAIT = int(os.environ.get("TWO_FACTOR_WAIT", "120"))  # 2FA验证 默认等 120 秒

# Cloudflare 盾最长等待秒数（真实 Chrome 一般 10s 内通过）
CF_WAIT = int(os.environ.get("CF_WAIT", "90"))

NOTIFY_TITLE = "stackryze 自动登录保活"

# 初始化
_notifier = None
config = None

def get_notifier():
    global _notifier, config
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


def slim_storage_state(state):
    """
    精简 storage_state，只保留核心登录凭据，防止超过 GitHub 64KB 限制
    """
    if not isinstance(state, dict):
        return state

    # 1. 精简 Cookies：只保留 .stackryze.com 和 github.com 的
    if "cookies" in state:
        state["cookies"] = [
            c for c in state["cookies"]
            if isinstance(c, dict) and (
                "stackryze.com" in c.get("domain", "")
                or "github.com" in c.get("domain", "")
            )
        ]

    # 2. 精简 Origins：只保留 stackryze 相关域，剔除没用的 UI 状态
    if "origins" in state:
        state["origins"] = [
            o for o in state["origins"]
            if isinstance(o, dict) and "stackryze.com" in o.get("origin", "")
        ]

    return state


def build_proxy_config(proxy):
    """
    把代理配置统一转成 Playwright 的 proxy 参数
    支持: dict{server,port,username,password} 或 DSN 字符串 http(s)/socks5://user:pass@host:port
    """
    if not proxy:
        return None
    try:
        if isinstance(proxy, dict):
            server = proxy.get("server") or proxy.get("host") or ""
            if server and "://" not in server:
                server = f"http://{server}"
            if server and proxy.get("port"):
                server = f"{server}:{proxy['port']}"
            cfg = {"server": server}
            if proxy.get("username"):
                cfg["username"] = proxy["username"]
            if proxy.get("password"):
                cfg["password"] = proxy["password"]
            return cfg
        # 字符串 DSN
        dsn = proxy if "://" in proxy else "http://" + proxy
        u = urlparse(dsn)
        cfg = {"server": f"{u.scheme or 'http'}://{u.hostname}:{u.port or 80}"}
        if u.username:
            cfg["username"] = u.username
        if u.password:
            cfg["password"] = u.password
        return cfg
    except Exception as e:
        print(f"⚠️ [build_proxy_config 异常] {e}")
        return None


class AutoLogin:
    """自动登录，因 GH_SESSION 每日更新，不考虑登录github，直接注入GH_SESSION"""

    def __init__(self, config):
        self.host = urlparse(BOARD_ENTRY_URL).netloc
        self.gh_username = config.get('gh_username')
        # self.gh_password = config.get('gh_password')
        self.proxy_url = None
        # gh_session 处理类型安全
        gh_sess = config.get('gh_session', '')
        if isinstance(gh_sess, str):
            self.gh_session = gh_sess.strip()
        elif isinstance(gh_sess, list):
            self.gh_session = gh_sess[0] if gh_sess else ''
        else:
            self.gh_session = ''

        # dt_local 处理类型安全
        dt_local_val = config.get('dt_local', '')
        if isinstance(dt_local_val, str):
            self.dt_local = dt_local_val.strip()
        else:
            # storage_state 本身是 dict，无需 strip
            self.dt_local = dt_local_val

        self.dt_proxy = config.get('dt_proxy', '').strip() if isinstance(config.get('dt_proxy', ''), str) else config.get('dt_proxy')
        self.proxy_url = test_proxy(self.dt_proxy)
        if not self.proxy_url:
            self.dt_proxy = config.get('wz_proxy')
            self.proxy_url = test_proxy(self.dt_proxy)

        self.notify = config.get('notify')
        self.secret = SecretUpdater("GH_SESSION", config_reader=config)
        self.shots = []
        self.logs = []
        self.n = 0

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

    def click(self, page, desc="", selectors=None):
        """
        专用于 SPA / React 页面按钮（主页面 + 所有 iframe 全扫描）
        """
        self.log(f"🔍 尝试查找并点击: {desc}", "INFO")

        # 1️⃣ 等页面真正稳定
        try:
            page.wait_for_load_state("domcontentloaded", timeout=15000)
            page.wait_for_timeout(2000)
        except:
            pass

        # 2️⃣ 收集主页面 + 所有 iframe
        frames = [page.main_frame]
        frames += page.frames

        if selectors is None:
            selectors = [
                # Stackryze SSO 按钮（span 文本）
                f'span:has-text("{SSO_BTN_TEXT}")',
                f'button:has-text("{SSO_BTN_TEXT}")',
                f'a:has-text("{SSO_BTN_TEXT}")',
                # GitHub 按钮（auth.stackryze.com，React onClick）
                'button[type="button"]:has-text("GitHub")',
                'button:has-text("GitHub")',
                'a:has-text("GitHub")',
                # XPath 兜底
                f'//span[normalize-space()="{SSO_BTN_TEXT}"]',
                '//button[normalize-space()="GitHub"]',
            ]

        for frame in frames:
            for sel in selectors:
                try:
                    el = frame.locator(sel).first
                    el.wait_for(state="visible", timeout=2000)

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

    def get_stackryze_cookies(self):
        """
        从 storage_state 中提取 domain 包含 stackryze.com 的 cookies
        """
        if not isinstance(self.dt_local, dict):
            return []

        cookies = self.dt_local.get("cookies", [])
        if not isinstance(cookies, list):
            return []

        return [
            c for c in cookies
            if isinstance(c, dict) and "domain" in c and "stackryze.com" in c["domain"]
        ]

    def get_local_storage_by_origin(self):
        """
        根据当前 host 动态匹配对应的 localStorage 数据
        """
        if not isinstance(self.dt_local, dict):
            # 增加容错：如果 dt_local 是 JSON 字符串则解析
            if isinstance(self.dt_local, str) and self.dt_local.strip().startswith('{'):
                try:
                    self.dt_local = json.loads(self.dt_local)
                except:
                    return []
            else:
                return []

        origins = self.dt_local.get("origins", [])
        if not isinstance(origins, list):
            return []

        for o in origins:
            origin_url = o.get("origin", "")
            if self.host in origin_url:
                self.log(f"✅ 成功匹配存储域: {origin_url}", "SUCCESS")
                return o.get("localStorage", [])

        return []

    def parse_cookies(self, cookie_string):
        cookies = {}
        for item in cookie_string.split(";"):
            item = item.strip()
            if "=" not in item:
                continue
            key, value = item.split("=", 1)
            cookies[key.strip()] = value.strip()
        return cookies

    def get_headers(self, cookies, json=False):
        headers = {
            "accept": "*/*",
            "accept-language": "zh-CN,zh;q=0.9,en-US;q=0.8,en;q=0.7",
            "cache-control": "no-cache",
            "pragma": "no-cache",
            "User-Agent": "Mozilla/5.0",
        }

        if json:
            headers["content-type"] = "application/json"

        csrf_token = cookies.get("panel_csrf_token") or cookies.get("csrf_token")

        if csrf_token:
            headers["x-csrf-token"] = csrf_token

        return headers

    def mask_url(self, url):
        url = re.sub(r'code=[^&]+', 'code=***', url)
        url = re.sub(r'state=[^&]+', 'state=***', url)
        url = re.sub(r'authRequest=V2_\d+', 'authRequest=V2_***', url)
        return url

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
            self.dt_local = state
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
            self.notify.send(title=NOTIFY_TITLE, content="🔑 <b>Cookie 已自动更新</b>\n\nGH_SESSION 已保存")
        else:
            # 通过 Telegram 发送
            self.notify.send(title=NOTIFY_TITLE, content=f"""🔑 <b>新 Cookie</b>

请更新 Secret <b>GH_SESSION</b> (点击查看):
<tg-spoiler>{value}</tg-spoiler>
""")
            self.log("已通过 Telegram 发送 Cookie", "SUCCESS")

    def wait_cloudflare(self, page):
        """等待 Cloudflare 盾通过（SSO 按钮渲染出来即算通过）"""
        for i in range(CF_WAIT):
            if page.locator(f'text={SSO_BTN_TEXT}').count() > 0:
                return True
            time.sleep(1)
            if i % 15 == 0 and i:
                self.log(f"  等待 Cloudflare/页面渲染... ({i}s)")
        return False

    def wait_device(self, page):
        """等待设备验证"""
        self.log(f"需要设备验证，等待 {DEVICE_VERIFY_WAIT} 秒...", "WARN")
        self.shot(page, "设备验证")

        self.notify.send(title=NOTIFY_TITLE, content=f"""⚠️ <b>需要设备验证</b>

请在 {DEVICE_VERIFY_WAIT} 秒内批准：
1️⃣ 检查邮箱点击链接
2️⃣ 或在 GitHub App 批准""")

        if self.shots:
            self.notify.send(title=NOTIFY_TITLE, content="设备验证页面", image_path=self.shots[-1])

        for i in range(DEVICE_VERIFY_WAIT):
            time.sleep(1)
            if i % 5 == 0:
                self.log(f"  等待... ({i}/{DEVICE_VERIFY_WAIT}秒)")
                url = page.url
                if 'verified-device' not in url and 'device-verification' not in url:
                    self.log("设备验证通过！", "SUCCESS")
                    self.notify.send(title=NOTIFY_TITLE, content="✅ <b>设备验证通过</b>")
                    return True
                try:
                    page.reload(timeout=10000)
                    page.wait_for_load_state('networkidle', timeout=10000)
                except:
                    pass

        if 'verified-device' not in page.url and 'device-verification' not in page.url:
            return True

        self.log("设备验证超时", "ERROR")
        self.notify.send(title=NOTIFY_TITLE, content="❌ <b>设备验证超时</b>")
        return False

    def wait_two_factor_mobile(self, page):
        """等待 GitHub Mobile 两步验证批准，并把数字截图提前发到电报"""
        self.log(f"需要两步验证（GitHub Mobile），等待 {TWO_FACTOR_WAIT} 秒...", "WARN")

        # 先截图并立刻发出去（让你看到数字）
        shot = self.shot(page, "两步验证_mobile")
        self.notify.send(title=NOTIFY_TITLE, content=f"""⚠️ <b>需要两步验证（GitHub Mobile）</b>

请打开手机 GitHub App 批准本次登录（会让你确认一个数字）。
等待时间：{TWO_FACTOR_WAIT} 秒""")
        if shot:
            self.notify.send(title=NOTIFY_TITLE, content="两步验证页面（数字在图里）", image_path=shot)

        # 不要频繁 reload，避免把流程刷回登录页
        for i in range(TWO_FACTOR_WAIT):
            time.sleep(1)

            url = page.url

            # 如果离开 two-factor 流程页面，认为通过
            if "github.com/sessions/two-factor/" not in url:
                self.log("两步验证通过！", "SUCCESS")
                self.notify.send(title=NOTIFY_TITLE, content="✅ <b>两步验证通过</b>")
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
                    self.notify.send(title=NOTIFY_TITLE, content=f"两步验证页面（第{i}秒）", image_path=shot)

            # 只在 30 秒、60 秒... 做一次轻刷新（可选，频率很低）
            if i % 30 == 0 and i != 0:
                try:
                    page.reload(timeout=30000)
                    page.wait_for_load_state('domcontentloaded', timeout=30000)
                except:
                    pass

        self.log("两步验证超时", "ERROR")
        self.notify.send(title=NOTIFY_TITLE, content="❌ <b>两步验证超时</b>")
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
                    time.sleep(1)  # 等待菜单出现
                    self.shot(page, "点击more_options后")

                    # 点击 "Authenticator app"
                    auth_app_button = page.locator('button:has-text("Authenticator app")').first
                    if auth_app_button.is_visible(timeout=2000):
                        auth_app_button.click()
                        self.log("已选择 'Authenticator app'", "SUCCESS")
                        time.sleep(2)
                        page.wait_for_load_state('networkidle', timeout=15000)
                        shot = self.shot(page, "切换到验证码输入页")  # 更新截图
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
        self.notify.send(title=NOTIFY_TITLE, content=f"""🔐 <b>需要验证码登录</b>

用户{self.gh_username}正在登录，请在 Telegram 里发送：
<code>/code 你的6位验证码</code>

等待时间：{TWO_FACTOR_WAIT} 秒""")
        if shot:
            self.notify.send(title=NOTIFY_TITLE, content="两步验证页面", image_path=shot)

        self.log(f"等待验证码（{TWO_FACTOR_WAIT}秒）...", "WARN")
        code = ''  # 真需要的话使用库自动生成

        if not code:
            self.log("等待验证码超时", "ERROR")
            self.notify.send(title=NOTIFY_TITLE, content="❌ <b>等待验证码超时</b>")
            return False

        # 不打印验证码明文，只提示收到
        self.log("收到验证码，正在填入...", "SUCCESS")
        self.notify.send(title=NOTIFY_TITLE, content="✅ 收到验证码，正在填入...")

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
                    self.log("已填入验证码", "SUCCESS")
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
                        self.notify.send(title=NOTIFY_TITLE, content="✅ <b>验证码验证通过</b>")
                        return True
                    else:
                        self.log("验证码可能错误", "ERROR")
                        self.notify.send(title=NOTIFY_TITLE, content="❌ <b>验证码可能错误，请检查后重试</b>")
                        return False
            except:
                pass

        self.log("没找到验证码输入框", "ERROR")
        self.notify.send(title=NOTIFY_TITLE, content="❌ <b>没找到验证码输入框</b>")
        return False

    def login_github(self, page, context):
        """登录 GitHub（session 过期且配置了密码时的兜底）"""
        if not getattr(self, 'gh_password', None):
            self.log("GitHub 登录过期且未配置密码，无法兜底登录", "ERROR")
            return False

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
            pass_input.type(self.gh_password, delay=random.randint(30, 100))

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
        self.log(f"当前: {self.mask_url(url)}")

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
                try:
                    page.wait_for_load_state('networkidle', timeout=30000)
                    time.sleep(2)
                except:
                    pass
            else:
                # 其它两步验证方式（TOTP/恢复码等），尝试通过 Telegram 输入验证码
                if not self.handle_2fa_code_input(page):
                    return False
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
        """处理 OAuth（含 Continue as <用户名> 确认按钮 + Authorize 按钮）"""
        if 'github.com/login/oauth/authorize' in page.url:
            self.log("处理 OAuth...", "STEP")
            self.shot(page, "oauth")

            # 1️⃣ "Continue as <用户名>" 确认按钮（已登录状态下的授权确认页）
            try:
                cont = page.locator(
                    'input[aria-label^="Continue as"], '
                    'input.btn.btn-sm.btn-primary.m-0[value="Continue"]'
                ).first
                if cont.is_visible(timeout=3000):
                    self.log(f"检测到确认按钮: {cont.get_attribute('aria-label')}", "INFO")
                    time.sleep(random.uniform(0.5, 1.0))
                    cont.click()
                    self.log("已点击 Continue 确认按钮", "SUCCESS")
                    time.sleep(3)
                    page.wait_for_load_state('networkidle', timeout=30000)
                    return
            except:
                pass

            # 2️⃣ 常规 Authorize 按钮
            self.click(page, "授权", selectors=[
                'button[name="authorize"]',
                'button:has-text("Authorize")',
                'input[type="submit"][value="Authorize"]',
            ])
            time.sleep(3)
            page.wait_for_load_state('networkidle', timeout=30000)

    def check_and_process_domain(self, domain):
        self.log(f"检查网址: {self.mask_url(domain)}")

        # GitHub 登录/OAuth 相关页（login/oauth/session 才算流程页，help.github.com 等不算）
        if re.search(r"github\.com/(login|oauth|sessions)", domain):
            return "github"
        if 'github.com' in domain:
            return "invalid"

        # auth.stackryze.com 登录页（等待点 GitHub）
        if 'auth.stackryze.com' in domain:
            return "authlogin"

        # 检查是否包含 callback（OAuth 重定向）
        if "callback" in domain:
            return "redirect"

        # 检查是否是正常已登录的应用页
        if APP_HOST in domain and '/login' not in domain.lower():
            return "logged"

        # 登录入口页
        if domain.rstrip('/').endswith(f'{APP_HOST}/login'):
            return "signin"

        # 其他情况
        return "invalid"

    def handle_github_branch(self, context):
        """GitHub 分支处理：登录过期/设备验证/2FA/OAuth/popup，返回 True=继续流程"""
        # 监控 context 内所有页面（GitHub 按钮可能开 popup）
        gh_pages = [pg for pg in context.pages if 'github.com' in pg.url]
        if not gh_pages:
            return True

        page = gh_pages[0]
        url = page.url

        # 登录页 → session 失效
        if re.match(r"https://github\.com/(login|session)", url):
            self.shot(page, "github登录过期")
            self.log("GitHub 登录过期！", "ERROR")
            # 有密码则兜底登录
            if getattr(self, 'gh_password', None) and self.login_github(page, context):
                self.oauth(page)
                return True
            return False

        # 设备验证
        if 'verified-device' in url or 'device-verification' in url:
            return self.wait_device(page)

        # 两步验证
        if 'two-factor' in url:
            self.log("需要两步验证！", "WARN")
            if 'two-factor/mobile' in url:
                return self.wait_two_factor_mobile(page)
            return self.handle_2fa_code_input(page)

        # OAuth 授权页
        self.oauth(page)
        return True

    def wait_redirect(self, page, context, wait=90):
        """等待重定向回 Stackryze 应用"""
        self.log("等待重定向...", "STEP")
        for i in range(wait):
            resault = self.check_and_process_domain(page.url)

            if resault == "logged":
                self.log("重定向成功！", "SUCCESS")
                return True

            if resault == "github":
                if not self.handle_github_branch(context):
                    return False

            if 'github.com/login/oauth/authorize' in page.url:
                self.oauth(page)

            time.sleep(1)
            if i % 10 == 0:
                self.log(f"  等待... ({i}秒)")

        self.log("重定向超时", "ERROR")
        return False

    def run(self):

        ok, new_local, msg = False, None, ""
        self.log(f"用户名: {mask_name(self.gh_username)}")
        self.log(f"Session: {'有' if self.gh_session else '无'}")
        self.log(f"登录入口: {BOARD_ENTRY_URL}")
        if self.dt_proxy:
            self.log(f"代理: {str(self.dt_proxy)[:40]}")

        if not self.gh_username:
            self.log("缺少凭据", "ERROR")
            return False, None, "❌ 缺少凭据"

        browser = None
        with sync_playwright() as p:
            # 代理配置解析
            launch_args = {
                "headless": True,
                "args": [
                    '--no-sandbox',
                    '--disable-blink-features=AutomationControlled',
                    '--disable-infobars',
                ]
            }

            proxy_config = build_proxy_config(self.dt_proxy)
            if proxy_config:
                launch_args["proxy"] = proxy_config

            # ⚠️ Cloudflare 会识别 Playwright 自带 Chromium（本地实测 headless/有头都卡盾），
            #    必须用真实 Chrome；失败回退自带 Chromium
            try:
                browser = p.chromium.launch(channel="chrome", **launch_args)
                self.log("使用真实 Chrome（channel=chrome）", "SUCCESS")
            except Exception as e:
                self.log(f"真实 Chrome 不可用({e})，回退自带 Chromium", "WARN")
                browser = p.chromium.launch(**launch_args)

            if self.dt_local:
                context = browser.new_context(
                    storage_state=self.dt_local,
                    viewport={'width': 1920, 'height': 1080},
                    user_agent='Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36'
                )
            else:
                context = browser.new_context(
                    viewport={'width': 1920, 'height': 1080},
                    user_agent='Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36'
                )

            page = context.new_page()
            # ⚠️ 注意：不要加 navigator.webdriver / window.chrome 之类的 init script 反检测注入
            #    实测真实 Chrome + UA 覆盖即可通过 Cloudflare；
            #    init script 反而制造指纹不一致导致被 CF 拒（对比实验验证）

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

                # 1. 访问 stackryze 登录入口
                self.log("步骤1: 打开 stackryze 登录页", "STEP")

                for i in range(10):
                    try:
                        page.goto(BOARD_ENTRY_URL, timeout=60000)
                        page.wait_for_load_state('domcontentloaded', timeout=60000)
                        time.sleep(random.uniform(3, 6))
                        shot = self.shot(page, "打开 stackryze 登录页")
                        if shot:
                            self.notify.send(title=NOTIFY_TITLE, content="打开 stackryze 登录页", image_path=shot)

                        resault = self.check_and_process_domain(page.url)
                        self.log(f"检测结果: {resault}", "INFO")

                        if resault == "invalid":
                            self.log(f"[1.{i}]: 非应用页: {self.mask_url(page.url)}", "WARN")
                            # Cloudflare 盾页也归这里，等盾通过再判定
                            if not self.wait_cloudflare(page):
                                self.log(f"[1.{i}]: Cloudflare 盾未通过", "WARN")
                                continue
                            resault = self.check_and_process_domain(page.url)

                        if resault == "logged":
                            self.log(f"[1.{i}]: 已登录(storage_state 有效): {self.mask_url(page.url)}", "SUCCESS")
                            break

                        if resault == "signin":
                            self.log(f"[1.{i}]: 需登录: {self.mask_url(page.url)}", "INFO")
                            # 步骤2: 点击 SSO
                            self.log("步骤2: 点击 Continue with Stackryze SSO", "STEP")
                            if not self.click(page, desc="SSO 按钮", selectors=[
                                f'span:has-text("{SSO_BTN_TEXT}")',
                                f'button:has-text("{SSO_BTN_TEXT}")',
                                f'a:has-text("{SSO_BTN_TEXT}")',
                                f'//span[normalize-space()="{SSO_BTN_TEXT}"]',
                            ]):
                                self.log(f"[2.{i}]: 找不到 SSO 按钮", "WARN")
                                continue

                            # 等待跳到 auth.stackryze.com?authRequest=V2_xxx
                            try:
                                page.wait_for_url(AUTH_LOGIN_RE, timeout=30000)
                                self.log(f"URL 已跳转: {self.mask_url(page.url)}", "SUCCESS")
                                time.sleep(random.uniform(2, 4))
                            except PlaywrightTimeoutError:
                                self.log(f"等待 auth 页跳转超时: {self.mask_url(page.url)}", "WARN")
                                continue

                            # 步骤3: 点击 GitHub
                            self.log("步骤3: 点击 GitHub", "STEP")
                            reached_github = False
                            for j in range(3):
                                resault = self.check_and_process_domain(page.url)
                                if resault == "authlogin":
                                    # 精确匹配 GitHub 按钮，避免点到页脚 help.github.com 链接
                                    if not self.click(page, desc="GitHub 登录按钮", selectors=[
                                        '//button[normalize-space()="GitHub"]',
                                        'button[type="button"]:has-text("GitHub")',
                                        'button:has-text("GitHub")',
                                    ]):
                                        self.log(f"[3.{i}.{j}]: 找不到 GitHub 按钮", "WARN")
                                        continue
                                    # 等待真正到达 github.com 登录/OAuth 页（国内网络无代理时会卡住）
                                    try:
                                        page.wait_for_url(re.compile(r"github\.com/(login|oauth|session)"), timeout=30)
                                        self.log(f"已到达 GitHub: {self.mask_url(page.url)}", "SUCCESS")
                                        reached_github = True
                                        break
                                    except PlaywrightTimeoutError:
                                        self.log(f"[3.{i}.{j}]: 点击后未跳到 GitHub（当前: {self.mask_url(page.url)}），重试", "WARN")
                                elif resault in ("github", "logged"):
                                    reached_github = resault == "github"
                                    break
                                else:
                                    self.log(f"[3.{i}.{j}]: 未预期状态: {self.mask_url(page.url)}", "WARN")
                                    break

                            # 步骤4: 处理 GitHub 登录/授权 + 等待回跳
                            self.log("步骤4: 处理 GitHub 登录/授权并等待回跳", "STEP")
                            if not self.wait_redirect(page, context, wait=90):
                                self.log(f"[4.{i}]: GitHub 处理或回跳失败", "ERROR")
                                continue
                            break

                        else:
                            # github/callback 等中间态，等它自然跳转
                            self.log(f"[1.{i}]: 中间态，等待跳转...", "INFO")
                            time.sleep(3)

                    except Exception as e:
                        if i < 10:
                            self.log(f"异常: {e}", "ERROR")
                            self.log(f"[1.{i}]: 未打开登录页，重试", "WARN")
                            time.sleep(random.uniform(10, 15))
                        else:
                            self.log(f"[1.{i}]: 访问 {self.mask_url(page.url)} 失败！", "ERROR")
                            browser.close()
                            return False, None, f"访问 {BOARD_ENTRY_URL} 失败！"

                # 3. 提取并保存新 local_storage
                self.log("步骤3: 更新 local_storage", "STEP")
                storage_state = self.get_storage(context)

                if storage_state:
                    self.log("开始为数据瘦身...")

                    # ⚠️ 直接传 dict
                    slimmest_local = slim_storage_state(storage_state)

                    # 现在才转 json
                    final_json = json.dumps(slimmest_local, ensure_ascii=False)

                    self.log(f"瘦身完成，最终数据大小: {len(final_json) / 1024:.2f} KB")

                    # 再 base64
                    storage_state_b64 = base64.b64encode(
                        final_json.encode("utf-8")
                    ).decode("utf-8")

                    ok = True
                    new_local = storage_state_b64

                else:
                    self.log("未获取到 storage_state", "WARN")

                print("\n" + "=" * 50)
                print("=" * 50 + "\n")
                if self.shots:
                    self.notify.send(title=NOTIFY_TITLE, content=f"✅ {self.gh_username}成功！", image_path=self.shots[-1])
            except Exception as e:
                self.log(f"异常: {e}", "ERROR")
                self.shot(page, "异常")
                import traceback
                traceback.print_exc()
                if self.shots:
                    self.notify.send(title=NOTIFY_TITLE, content=f"❌ {self.gh_username}:{str(e)}", image_path=self.shots[-1])
                msg = f"访问 {self.mask_url(page.url)} 失败！"
            finally:
                if browser:
                    browser.close()
                return ok, new_local, msg


def main():
    global config
    if config is None:
        config = ConfigReader()
    useproxy = True
    newcookies = {}
    results = []

    # 读取账号信息
    accounts = config.get_value("GH_INFO")

    # 读取代理信息
    proxies = config.get_value("WZ_INFO")

    # env 兜底：单账号模式（GH_INFO 未配置时从 GH_USERNAME/GH_SESSION 组装）
    if isinstance(accounts, str):
        try:
            accounts = json.loads(accounts)
        except:
            accounts = None
    if not accounts:
        gh_username = os.environ.get("GH_USERNAME", "").strip()
        gh_session = os.environ.get("GH_SESSION", "").strip()
        if gh_username and gh_session:
            accounts = [{"username": gh_username}]
        else:
            print("❌ 错误: 未配置 GH_INFO（或 GH_USERNAME/GH_SESSION）")
            return
    if isinstance(proxies, str):
        try:
            proxies = json.loads(proxies)
        except:
            proxies = None
    if not proxies and PROXY_DSN:
        proxies = [PROXY_DSN]  # env 单代理模式
    if not proxies:
        print("📢 警告: 未配置 proxy ，将直连")
        useproxy = False

    print(f"📊 检测到 {len(accounts)} 个账号和 {len(proxies or [])} 个代理")

    # 初始化 get_notifier
    notify = get_notifier()
    # 初始化 SecretUpdater，会自动根据当前仓库用户名获取 token
    gh_secret = SecretUpdater("GH_SESSION", config_reader=config)
    # 读取
    gh_sessions = gh_secret.load() or {}

    # 初始化 SecretUpdater（storage_state 持久化，下次运行免 GitHub 登录）
    secret = SecretUpdater("STACKRYZE_LOCALS", config_reader=config)
    # 读取
    dt_locals = secret.load() or {}

    # 使用 zip 实现一一对应
    for account, proxy in zip(accounts, proxies or [None] * len(accounts)):
        username = account['username']
        print("\n" + "=" * 50)
        print(f"\n🚀 开始处理账号: {mask_name(username)}\n")
        print("=" * 50 + "\n")

        results.append(f"🚀 账号：{mask_name(username)}\n")
        dt_info = {}
        dt_info['gh_username'] = username
        # dt_info['gh_password'] = account.get('password')
        dt_info['dt_proxy'] = proxy
        dt_info['notify'] = notify
        dt_info['wz_proxy'] = (proxies or [])[-1] if proxies else None

        if isinstance(gh_sessions, dict):
            gh_session = gh_sessions.get(username, '')
            if isinstance(gh_session, list):
                gh_session = gh_session[0] if gh_session else ''
            dt_info['gh_session'] = gh_session
        else:
            print(f"⚠️ gh_sessions 格式错误！")
            dt_info['gh_session'] = ''

        # env 单账号兜底
        if not dt_info['gh_session']:
            dt_info['gh_session'] = os.environ.get("GH_SESSION", "").strip()

        if not dt_info['gh_session']:
            print(f"⚠️ 缺少对应账号的 gh_session ，退出！")
            continue

        if isinstance(dt_locals, dict):
            # 预加载localStorage数据，验证有效不再使用 gh_session
            dt_local = dt_locals.get(username, '')
            if dt_local:
                try:
                    dt_info['dt_local'] = json.loads(base64.b64decode(dt_local).decode("utf-8"))
                    print("✅ 已加载 storage_state")
                except Exception as e:
                    print(f"❌ 加载 storage_state 失败: {e}")
        else:
            print(f"⚠️ dt_locals 格式错误！")
            dt_locals = {}
            dt_info['dt_local'] = []

        try:
            auto_login = AutoLogin(dt_info)
            ok, new_local, msg = auto_login.run()

            if ok:
                print(f"    ✅ 执行成功")
                results.append(f"    ✅ {msg or '登录成功'}\n")
                if new_local:
                    print(f"    ✅ 保存新 new_local")
                    dt_locals[username] = new_local
            else:
                print(f"    ⚠️ 执行失败，不保存 cookie")
                results.append(f"    ⚠️ 执行失败:{msg}\n")

        except Exception as e:
            print(f"    ❌ 执行异常: {e}")
            results.append(f"    ❌ 执行异常: {e}")

    # 写入
    # 转换为 JSON 字符串前可以检查下大小
    print(f"dt_locals数据大小: {len(json.dumps(dt_locals)) / 1024:.2f} KB")
    secret.update(dt_locals)
    # 发送结果
    notify.send(
        title="stackryze 自动登录保活汇总",
        content="\n".join(results)
    )


if __name__ == "__main__":
    main()
