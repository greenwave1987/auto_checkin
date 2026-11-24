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
BASE_URL = "https://api-domains.stackryze.com/subdomains"
RENEW_DAYS = 120
TIMEOUT = 15
BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BASE_DIR)
from engine.notify import TelegramNotifier
try:
    from engine.main import ConfigReader, SecretUpdater, print_dict_tree, test_proxy
except ImportError:
    class ConfigReader:
        def get_value(self, key): return os.environ.get(key)
    class SecretUpdater:
        def __init__(self, name=None, config_reader=None): pass
        def update(self, value): return False
        def load(self): return {}

# ==================== 配置 ====================
PROXY_DSN = os.environ.get("PROXY_DSN", "").strip()

# 固定自己创建有APP的登录入口
BOARD_ENTRY_URL = "https://auth.stackryze.com/en/login"

DEVICE_VERIFY_WAIT = 30  # Mobile验证 默认等 30 秒
TWO_FACTOR_WAIT = int(os.environ.get("TWO_FACTOR_WAIT", "120"))  # 2FA验证 默认等 120 秒

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
    if "@" not in email: return "***"
    name, domain = email.split("@", 1)
    return f"{name[:2]}***{name[-2:]}@{domain}"

def mask_name(name: str):
    if not name: return "***"
    return f"{name[:2]}***{name[-2:]}"

def mask_ip(ip: str):
    return f"***{ip}" if ip else "***"

def mask_password(pwd: str):
    return "*" * 6 + f"({len(pwd)})"

def slim_storage_state(state):
    if not isinstance(state, dict): return state
    if "cookies" in state:
        state["cookies"] = [c for c in state["cookies"] if "stackryze.com" in c.get("domain", "")]
    if "origins" in state:
        new_origins = []
        essential_keys = ["session", "lastLoginUpdateTime", "i18nextLng"]
        for o in state["origins"]:
            storage = o.get("localStorage", [])
            slim_storage = [item for item in storage if item.get("name") in essential_keys]
            o["localStorage"] = slim_storage
            new_origins.append(o)
        state["origins"] = new_origins
    return state


class AutoLogin:
    def __init__(self, config):
        self.host = urlparse(BOARD_ENTRY_URL).netloc
        self.gh_username = config.get('gh_username')
        self.password = config.get('gh_password') or config.get('password')
        self.proxy_url = None
        
        gh_sess = config.get('gh_session', '')
        if isinstance(gh_sess, str):
            self.gh_session = gh_sess.strip()
        elif isinstance(gh_sess, list):
            self.gh_session = gh_sess[0] if gh_sess else ''
        else:
            self.gh_session = ''

        dt_local_val = config.get('dt_local', '')
        if isinstance(dt_local_val, str):
            self.dt_local = dt_local_val.strip()
        else:
            self.dt_local = dt_local_val

        self.dt_proxy = config.get('dt_proxy', '').strip() if isinstance(config.get('dt_proxy', ''), str) else config.get('dt_proxy')
        self.proxy_url = test_proxy(self.dt_proxy)
        if not self.proxy_url:
            self.dt_proxy = config.get('wz_proxy')
            self.proxy_url = test_proxy(self.dt_proxy)

        self.notify = config.get('notify')
        self.shots = []
        self.logs = []
        self.n = 0

    def log(self, msg, level="INFO"):
        icons = {"INFO": "ℹ️", "SUCCESS": "✅", "ERROR": "❌", "WARN": "⚠️", "STEP": "🔹", "DEBUG": "•"}
        line = f"{icons.get(level, '•')} {msg}"
        print(line, flush=True)
        self.logs.append(line)

    def shot(self, page, name):
        self.n += 1
        f = f"{self.n:02d}_{name}.png"
        try:
            page.screenshot(path=f)
            self.shots.append(f)
        except: pass
        return f

    def click(self, page, desc=""):
        self.log(f"🔍 尝试查找并点击: {desc}", "INFO")

        def click_help_github_button(target_page):
            self.log(f"检测到 GitHub Help 登录页面: {target_page.url}", "INFO")
            try:
                target_page.wait_for_load_state("domcontentloaded", timeout=15000)
                target_page.wait_for_timeout(1500)
            except: pass

            candidates = [
                target_page.get_by_role("button", name=re.compile(r"^Sign in with GitHub$", re.I)),
                target_page.locator('button:has(span[data-component="text"])').filter(has_text=re.compile(r"Sign in with GitHub", re.I)),
                target_page.locator('span[data-component="text"].prc-Button-Label-FWkx3').filter(has_text=re.compile(r"Sign in with GitHub", re.I))
            ]

            for index, locator in enumerate(candidates):
                try:
                    locator.first.wait_for(state="visible", timeout=5000)
                    button = locator.first
                    if index == 2:
                        parent_button = button.locator("xpath=ancestor::button[1]")
                        if parent_button.count() > 0:
                            button = parent_button.first

                    try: button.scroll_into_view_if_needed(timeout=5000)
                    except: pass

                    try: button.click(timeout=15000)
                    except:
                        button.click(timeout=10000, force=True)

                    self.log("已点击 Sign in with GitHub", "SUCCESS")
                    old_url = target_page.url
                    for wait_index in range(30):
                        try: target_page.wait_for_timeout(1000)
                        except: time.sleep(1)
                        if target_page.url != old_url: return True
                        try:
                            pages = target_page.context.pages
                            for opened_page in pages:
                                if opened_page is not target_page and "github.com" in opened_page.url: return True
                        except: pass
                    return True
                except PlaywrightTimeoutError: continue
                except Exception: pass
            return False

        if "help.github.com/request/landing" in page.url:
            return click_help_github_button(page)

        try:
            page.wait_for_load_state("domcontentloaded", timeout=15000)
            page.wait_for_timeout(1500)
        except: pass

        frames = list(dict.fromkeys(page.frames))

        # ★ 修复：兼容新版页面的按钮特征
        selectors = [
            'a[href="/auth/login/github"]',    # 旧版
            'button:has-text("GitHub")',       # 新版可能是个按钮
            'a:has-text("GitHub")',
            '[data-provider="github"]',
            'button:has-text("Continue with GitHub")',
            'button:has-text("GitHub Login")',
            'button.chakra-button',
            'button:has(svg)',
            '//button[.//text()[contains(., "GitHub")]]'
        ]

        for frame in frames:
            for sel in selectors:
                try:
                    el = frame.locator(sel).first
                    el.wait_for(state="visible", timeout=2000)

                    if "github" in sel.lower() and sel not in ('a[href="/auth/login/github"]', '[data-provider="github"]'):
                        try:
                            if "github" not in el.inner_text(timeout=1500).lower(): continue
                        except: continue

                    time.sleep(random.uniform(0.3, 0.7))
                    try: el.scroll_into_view_if_needed(timeout=3000)
                    except: pass
                    
                    el.click(timeout=10000)
                    self.log(f"已点击 stackryze 登录入口: {desc}", "SUCCESS")

                    for wait_index in range(40):
                        try: page.wait_for_timeout(1000)
                        except: time.sleep(1)

                        if "help.github.com/request/landing" in page.url:
                            return click_help_github_button(page)

                        if ("github.com/login" in page.url or "github.com/sessions/" in page.url or "stackryze.com" in page.url):
                            if page.url.endswith("/auth/login"): continue
                            return True
                    return True

                except PlaywrightTimeoutError: pass
                except Exception: pass

        if "help.github.com/request/landing" in page.url:
            return click_help_github_button(page)

        return False

    def get_stackryze_cookies(self):
        if not isinstance(self.dt_local, dict): return []
        cookies = self.dt_local.get("cookies", [])
        if not isinstance(cookies, list): return []
        return [c for c in cookies if isinstance(c, dict) and "domain" in c and "stackryze.com" in c["domain"]]

    def build_session(self, token):
        cookies = self.get_stackryze_cookies()
        try:
            s = requests.Session()
            s.headers.update({
                "authority": self.host,
                "accept": "application/json, text/plain, */*",
                "authorization": "Bearer " + token,
                "referer": f"https://{self.host}/",
                "user-agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/119.0.0.0 Safari/537.36"
            })
            for c in cookies: s.cookies.set(c["name"], c["value"])
            return s
        except: return None

    def parse_cookies(self, cookie_string):
        cookies = {}
        for item in cookie_string.split(";"):
            item = item.strip()
            if "=" not in item: continue
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
        if json: headers["content-type"] = "application/json"
        csrf_token = cookies.get("panel_csrf_token")
        if csrf_token: headers["x-csrf-token"] = csrf_token
        return headers

    def get_domains(self, cookies):
        try:
            response = requests.get(BASE_URL, headers=self.get_headers(cookies), cookies=cookies, timeout=TIMEOUT)
            if not response.ok: return None
            try: data = response.json()
            except ValueError: return None
            if isinstance(data, dict): return data.get("domains", data.get("data", []))
            elif isinstance(data, list): return data
            return []
        except requests.RequestException: return None

    def get_days_left(self, expiry_str):
        if not expiry_str: return None
        try:
            expiry_date = datetime.datetime.strptime(str(expiry_str), "%Y%m%d")
            today = datetime.datetime.now().replace(hour=0, minute=0, second=0, microsecond=0)
            return (expiry_date - today).days
        except: return None

    # ★ 修复：增加 domain_name 参数传递，修复 NameError
    def renew_domain(self, cookies, domain_name, _id):
        url = f"{BASE_URL}/{_id}/renew"
        payload = {"renewal_type": "free", "years": 1}
        try:
            response = requests.post(url, headers=self.get_headers(cookies, json=True), cookies=cookies, json=payload, timeout=TIMEOUT)
            print("\n========== 续费 ==========")
            print("Domain:", domain_name)
            print("HTTP:", response.status_code)
            print("Response:", response.text)
            if response.ok:
                print(f"✅ {domain_name} 续费请求成功")
                return True
            print(f"❌ {domain_name} 续费失败")
            return False
        except requests.RequestException as e:
            print(f"❌ {domain_name} 续费请求异常:", e)
            return False

    def check_and_renew(self, cookies):
        domains = self.get_domains(cookies)
        if domains is None: return "❌ 获取域名列表失败\n"
        if not domains: return "⚠️ 没有找到域名\n"

        return_msg = ""
        renew_success = 0
        renew_failed = 0

        for item in domains:
            if isinstance(item, str):
                domain = item
                _id = item
                expiry_str = None
            else:
                domain = item.get("domain")
                _id = item.get("_id")
                expiry_str = item.get("expiresAt")

            if not domain: continue
            if not expiry_str: continue

            days = self.get_days_left(expiry_str)
            if days is None: continue

            if days < RENEW_DAYS:
                msg = f"⚠️ {domain} 剩余 {days} 天，正在续费..."
                return_msg += msg + "\n"
                if self.renew_domain(cookies, domain, _id):
                    renew_success += 1
                    return_msg += f"✅ {domain} 续费成功\n"
                else:
                    renew_failed += 1
                    return_msg += f"❌ {domain} 续费失败\n"
            else:
                return_msg += f"✅ {domain} 剩余 {days} 天，无需续费\n"

        summary = f"📊 检查完成：{len(domains)} 个域名，续费成功 {renew_success} 个，失败 {renew_failed} 个"
        return return_msg + summary + "\n"

    def get_balance_with_token(self, page):
        self.log("开始执行保活与自动续费检查...", "STEP")
        return_msg = ""
        try:
            result_data = page.evaluate("""
                async () => {
                    try {
                        const getCookie = (name) => {
                            const value = `; ${document.cookie}`;
                            const parts = value.split(`; ${name}=`);
                            if (parts.length === 2) return parts.pop().split(';').shift();
                            return null;
                        };
                        const xsrfToken = getCookie('panel_csrf_token');
                        const commonHeaders = {
                            "accept": "application/json, text/plain, */*",
                            "accept-language": "zh-CN,zh;q=0.9,en;q=0.8",
                            "cache-control": "no-cache",
                            "pragma": "no-cache",
                            "X-Requested-With": "XMLHttpRequest",
                            ...(xsrfToken && { "x-csrf-token": decodeURIComponent(xsrfToken) })
                        };
                        const response = await fetch("https://api-domains.stackryze.com/subdomains", {
                            headers: commonHeaders, referrer: "https://domain.stackryze.com/", method: "GET", credentials: "include"
                        });
                        if (!response.ok) return { success: false, error: `获取列表失败，HTTP 状态码: ${response.status}` };
                        const resData = await response.json();
                        const domains = resData.domains || [];
                        const logResults = [];
                        if (domains.length === 0) return { success: true, logs: ["当前账号下没有域名"] };

                        const today = new Date();
                        for (const item of domains) {
                            const domainName = item.domain;
                            const expiryStr = item.expiry_date;
                            if (!expiryStr || expiryStr.length !== 8) continue;
                            const year = parseInt(expiryStr.substring(0, 4));
                            const month = parseInt(expiryStr.substring(4, 6)) - 1;
                            const day = parseInt(expiryStr.substring(6, 8));
                            const diffDays = Math.ceil((new Date(year, month, day).getTime() - today.getTime()) / (1000 * 60 * 60 * 24));

                            if (diffDays < 120) {
                                logResults.push(`${domainName}: 剩余 ${diffDays} 天 (< 120天)，正在触发续费...`);
                                const renewRes = await fetch(`https://api-domains.stackryze.com/subdomains/${domainName}/renew`, {
                                    headers: { ...commonHeaders, "content-type": "application/json" },
                                    referrer: `https://domain.stackryze.com/`,
                                    body: JSON.stringify({ "renewal_type": "free", "years": 1 }),
                                    method: "POST", credentials: "include"
                                });
                                if (renewRes.ok) logResults.push(`${domainName}: ✅ 续费成功`);
                                else logResults.push(`${domainName}: ❌ 续费失败 [${renewRes.status}]`);
                            } else {
                                logResults.push(`${domainName}: 剩余 ${diffDays} 天，无需续费`);
                            }
                        }
                        return { success: true, logs: logResults };
                    } catch (error) {
                        return { success: false, error: error.message };
                    }
                }
            """)
            if result_data and result_data.get("success"):
                for log_item in result_data.get("logs", []):
                    return_msg += log_item + "\n"
            else:
                return_msg += f"⚠️ 脚本失败: {result_data.get('error')}\n"
        except Exception as e:
            return_msg += f"❌ 续费流程异常: {e}\n"
        return return_msg

    def wait_device(self, page):
        self.log(f"需要设备验证，等待 {DEVICE_VERIFY_WAIT} 秒...", "WARN")
        self.shot(page, "设备验证")
        for i in range(DEVICE_VERIFY_WAIT):
            time.sleep(1)
            if i % 5 == 0:
                if 'verified-device' not in page.url and 'device-verification' not in page.url:
                    return True
                try:
                    page.reload(timeout=10000)
                    page.wait_for_load_state('networkidle', timeout=10000)
                except: pass
        return 'verified-device' not in page.url

    def wait_two_factor_mobile(self, page):
        self.log(f"需要两步验证（GitHub Mobile），等待 {TWO_FACTOR_WAIT} 秒...", "WARN")
        for i in range(TWO_FACTOR_WAIT):
            time.sleep(1)
            if "github.com/sessions/two-factor/" not in page.url: return True
            if "github.com/login" in page.url: return False
            if i % 30 == 0 and i != 0:
                try: page.reload(timeout=30000)
                except: pass
        return False

    def handle_2fa_code_input(self, page):
        self.log("需要输入验证码", "WARN")
        return False # 如需自动填验证码，可在此处恢复你之前的逻辑

    def login_github(self, page, context):
        self.log("登录 GitHub...", "STEP")
        if not getattr(self, 'password', None): return False
        try:
            page.locator('input[name="login"]').click()
            page.locator('input[name="login"]').type(self.gh_username, delay=50)
            page.locator('input[name="password"]').click()
            page.locator('input[name="password"]').type(self.password, delay=50)
            page.locator('input[type="submit"], button[type="submit"]').first.click()
        except: return False

        time.sleep(3)
        page.wait_for_load_state('networkidle', timeout=30000)
        
        if 'verified-device' in page.url or 'device-verification' in page.url:
            if not self.wait_device(page): return False
            time.sleep(2)
            page.wait_for_load_state('networkidle', timeout=30000)

        if 'two-factor' in page.url:
            if 'two-factor/mobile' in page.url:
                if not self.wait_two_factor_mobile(page): return False
            else:
                if not self.handle_2fa_code_input(page): return False
            try: page.wait_for_load_state('networkidle', timeout=30000)
            except: pass
        return True

    def oauth(self, page):
        """处理 OAuth 授权页（就是你截图卡住的那个页面）"""
        if 'github.com/login/oauth/authorize' in page.url:
            self.log("处理 OAuth 授权...", "STEP")
            self.shot(page, "oauth")
            # ★ 修复：新版页面使用的是 "Continue" 按钮
            self.jclick(page, [
                'button[name="authorize"]', 
                'button:has-text("Authorize")', 
                'button:has-text("Continue")',
                'input[type="submit"][value*="Continue"]'
            ], "授权/继续")
            time.sleep(3)
            page.wait_for_load_state('networkidle', timeout=30000)

    def jclick(self, page, selectors, desc=""):
        for sel in selectors:
            try:
                el = page.locator(sel).first
                if el.is_visible(timeout=3000):
                    el.click(timeout=10000)
                    self.log(f"已点击: {desc or sel}", "SUCCESS")
                    return True
            except: continue
        return False

    def check_and_process_domain(self, domain):
        # ★ 修复：新域名 auth.stackryze.com 的判断
        if "help.github.com/request/landing" in domain: return "signin"
        if "stackryze.com" in domain and "login" in domain: return "signin"
        if "github.com/login" in domain or "github.com/sessions/" in domain: return "github_login"
        if "stackryze.com" in domain and "callback" in domain: return "redirect"
        if "stackryze.com" in domain and "my-domains" in domain: return "logged"
        return "invalid"

    def run(self):
        ok, new_local, msg = False, None, ""
        self.log(f"用户名: {mask_name(self.gh_username)}")

        if not self.gh_username: return False, None, "❌ 缺少凭据"

        with sync_playwright() as p:
            # ★ 建议在 GitHub Actions 中加上 "channel": "chrome" 对抗 CF 检测
            launch_args = {
                "headless": True,
                # "channel": "chrome", # 如果你的机器安装了 Chrome 建议开启
                "args": [
                    '--no-sandbox',
                    '--disable-dev-shm-usage',
                    '--headless=new',
                    '--disable-blink-features=AutomationControlled',
                ]
            }

            if self.dt_proxy:
                p_url = self.dt_proxy
                launch_args["proxy"] = {
                    "server": f"http://{p_url['server']}:{p_url['port']}",
                    "username": p_url['username'],
                    "password": p_url['password']
                }

            browser = p.chromium.launch(**launch_args)
            context = browser.new_context(
                storage_state=self.dt_local if self.dt_local else None,
                viewport={'width': 1920, 'height': 1080},
                user_agent='Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36'
            )

            page = context.new_page()
            page.add_init_script("""
                Object.defineProperty(navigator, 'webdriver', { get: () => undefined });
                window.chrome = { runtime: {} };
            """)

            try:
                if self.gh_session:
                    context.add_cookies([
                        {'name': 'user_session', 'value': self.gh_session, 'domain': 'github.com', 'path': '/'},
                        {'name': 'logged_in', 'value': 'yes', 'domain': 'github.com', 'path': '/'}
                    ])

                self.log("步骤1: 打开 stackryze 登录页", "STEP")

                for i in range(10):
                    try:
                        page.goto(BOARD_ENTRY_URL, timeout=60000)
                        page.wait_for_load_state('domcontentloaded', timeout=60000)
                        time.sleep(random.uniform(3, 5))

                        resault = self.check_and_process_domain(page.url)
                        self.log(f"检测结果: {resault}", "INFO")
                        
                        if resault == "invalid":
                            time.sleep(2)
                            continue
                        
                        if resault == "logged":
                            self.log(f"已登录: {page.url}", "SUCCESS")
                            break
                        
                        if resault == "signin":
                            if not self.click(page, desc="GitHub 登录按钮"):
                                time.sleep(3)
                                continue
                            else:
                                for j in range(10):
                                    resault = self.check_and_process_domain(page.url)

                                    if resault == "signin":
                                        self.click(page, desc="GitHub 登录按钮")
                                        continue

                                    if resault == "github_login":
                                        if not self.login_github(page, context):
                                            return False, None, "❌ GitHub 登录失败"
                                        self.oauth(page)  # 登录完成后立刻处理授权页
                                        continue

                                    if resault == "logged":
                                        break

                                    if resault == "redirect":
                                        try:
                                            page.wait_for_url("https://*.stackryze.com", timeout=60000)
                                            break
                                        except PlaywrightTimeoutError:
                                            pass
                                        continue
                                    
                                    time.sleep(2)
                    except Exception as e:
                        self.log(f"异常: {e}", "ERROR")
                        time.sleep(5)

                self.log("步骤3: 更新 local_storage", "STEP")
                storage_state = context.storage_state()
                if storage_state:
                    final_json = json.dumps(slim_storage_state(storage_state), ensure_ascii=False)
                    new_local = base64.b64encode(final_json.encode("utf-8")).decode("utf-8")
                    ok = True

                self.log("步骤4: 查询有效期信息", "STEP")
                msg += self.get_balance_with_token(page)

            except Exception as e:
                msg = f"异常: {e}"
            finally:
                browser.close()
                return ok, new_local, msg

def main():
    global config
    if config is None: config = ConfigReader()
    
    accounts = config.get_value("GH_INFO")
    proxies = config.get_value("WZ_INFO")
    if not accounts: return
    if not proxies: proxies = []

    notify = get_notifier()
    gh_secret = SecretUpdater("GH_SESSION", config_reader=config)
    gh_sessions = gh_secret.load() or {}
    secret = SecretUpdater("STACKRYZE_LOCALS", config_reader=config)
    dt_locals = secret.load()
    results = []

    proxy_iter = iter(proxies) if proxies else iter([None] * len(accounts))

    for account, proxy in zip(accounts, proxy_iter):
        username = account['username']
        if username == 'you5102': continue

        dt_info = {
            'gh_username': username,
            'gh_password': account.get('password'),
            'dt_proxy': proxy,
            'notify': notify,
            'wz_proxy': proxies[-1] if proxies else None,
            'gh_session': ''
        }

        if isinstance(gh_sessions, dict):
            gh_session = gh_sessions.get(username, '')
            if isinstance(gh_session, list): gh_session = gh_session[0] if gh_session else ''
            dt_info['gh_session'] = gh_session

        if isinstance(dt_locals, dict):
            dt_local = dt_locals.get(username, '')
            if dt_local:
                try: dt_info['dt_local'] = json.loads(base64.b64decode(dt_local).decode("utf-8"))
                except: pass
        else:
            dt_info['dt_local'] = []

        try:
            auto_login = AutoLogin(dt_info)
            ok, new_local, msg = auto_login.run()

            if ok:
                results.append(f"    ✅ {msg}\n")
                if new_local: dt_locals[username] = new_local
            else:
                results.append(f"    ⚠️ 执行失败:{msg}\n")
        except Exception as e:
            results.append(f"    ❌ 执行异常: {e}\n")
            
    secret.update(dt_locals)
    notify.send(title="stackryze 自动登录保活汇总", content="\n".join(results))

if __name__ == "__main__":
    main()
