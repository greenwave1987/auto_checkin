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
# 代理配置 (留空则不使用)
# 格式: socks5://user:pass@host:port 或 http://user:pass@host:port
PROXY_DSN = os.environ.get("PROXY_DSN", "").strip()

# 固定自己创建有APP的登录入口
BOARD_ENTRY_URL = "https://auth.stackryze.com/en/login"

DEVICE_VERIFY_WAIT = 30  # Mobile验证 默认等 30 秒
TWO_FACTOR_WAIT = int(os.environ.get("TWO_FACTOR_WAIT", "120"))  # 2FA验证 默认等 120 秒

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
    if not name: return "***"
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

    # 1. 精简 Cookies：只保留 .stackryze.com 的
    if "cookies" in state:
        state["cookies"] = [
            c for c in state["cookies"]
            if "stackryze.com" in c.get("domain", "")
        ]

    # 2. 精简 Origins：只保留核心 key，剔除没用的 UI 状态
    if "origins" in state:
        new_origins = []
        # 核心关注的 localStorage 键名
        essential_keys = ["session", "lastLoginUpdateTime", "i18nextLng"]

        for o in state["origins"]:
            storage = o.get("localStorage", [])
            # 过滤掉那些几百行长的无用缓存数据
            slim_storage = [
                item for item in storage
                if item.get("name") in essential_keys
            ]
            o["localStorage"] = slim_storage
            new_origins.append(o)

        state["origins"] = new_origins

    return state


class AutoLogin:
    """自动登录，因 GH_SESSIION 每日更新，不考虑登录github，直接注入GH_SESSIION"""

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
        self.secret = SecretUpdater()
        self.shots = []
        self.logs = []
        self.n = 0
        self.region_base_url = 'https://ap-northeast-1.run.stackryze.com'

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
        except:
            pass
        return f

    def click(self, page, desc=""):
        self.log(f"🔍 尝试查找并点击: {desc}", "INFO")

        def click_help_github_button(target_page):
            self.log(f"检测到 GitHub Help 登录页面: {target_page.url}", "INFO")
            try:
                target_page.wait_for_load_state("domcontentloaded", timeout=15000)
                target_page.wait_for_timeout(1500)
            except Exception:
                pass

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

                    self.log(f"找到 GitHub Help 登录按钮，候选定位方式 {index + 1}", "SUCCESS")
                    self.shot(target_page, "GitHub_Help登录按钮")

                    try:
                        button.scroll_into_view_if_needed(timeout=5000)
                        button.click(timeout=15000)
                    except Exception as click_error:
                        self.log(f"普通点击失败，尝试备用点击: {click_error}", "WARN")
                        button.click(timeout=10000, force=True)

                    self.log("已点击 Sign in with GitHub", "SUCCESS")
                    old_url = target_page.url
                    for wait_index in range(30):
                        try:
                            target_page.wait_for_timeout(1000)
                        except Exception:
                            time.sleep(1)

                        if target_page.url != old_url:
                            return True

                        try:
                            pages = target_page.context.pages
                            for opened_page in pages:
                                if opened_page is not target_page and "github.com" in opened_page.url:
                                    return True
                        except Exception:
                            pass

                    return True
                except PlaywrightTimeoutError:
                    continue
                except Exception as e:
                    self.log(f"GitHub Help 按钮定位/点击异常: {e}", "WARN")

            return False

        if "help.github.com/request/landing" in page.url:
            return click_help_github_button(page)

        try:
            page.wait_for_load_state("domcontentloaded", timeout=15000)
            page.wait_for_timeout(1500)
        except Exception:
            pass

        frames = list(dict.fromkeys(page.frames))

        selectors = [
            'a[href="/auth/login/github"]',
            'button:has-text("GitHub")',
            'a:has-text("GitHub")',
            '[data-provider="github"]',
            'button.chakra-button',
            'button:has(svg)',
            '//button[.//text()[contains(., "GitHub")]]',
            '//button[.//*[name()="svg"]]'
        ]

        for frame in frames:
            for sel in selectors:
                try:
                    el = frame.locator(sel).first
                    el.wait_for(state="visible", timeout=2000)

                    if "github" in sel.lower() and sel not in ('a[href="/auth/login/github"]', '[data-provider="github"]'):
                        try:
                            if "github" not in el.inner_text(timeout=1500).lower():
                                continue
                        except Exception:
                            continue

                    time.sleep(random.uniform(0.3, 0.7))
                    try:
                        el.scroll_into_view_if_needed(timeout=3000)
                    except Exception:
                        pass
                    el.click(timeout=10000)

                    self.log(f"已点击 stackryze 登录入口: {desc}", "SUCCESS")

                    for wait_index in range(40):
                        try:
                            page.wait_for_timeout(1000)
                        except Exception:
                            time.sleep(1)

                        if "help.github.com/request/landing" in page.url:
                            return click_help_github_button(page)

                        if ("github.com/login" in page.url or "github.com/sessions/" in page.url or "stackryze.com" in page.url):
                            if page.url.endswith("/auth/login"):
                                continue
                            return True
                    return True

                except PlaywrightTimeoutError:
                    pass
                except Exception:
                    pass

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
            for c in cookies:
                s.cookies.set(c["name"], c["value"])
            return s
        except Exception:
            return None

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
        if json:
            headers["content-type"] = "application/json"
        csrf_token = cookies.get("panel_csrf_token")
        if csrf_token:
            headers["x-csrf-token"] = csrf_token
        return headers

    def get_domains(self, cookies):
        try:
            response = requests.get(BASE_URL, headers=self.get_headers(cookies), cookies=cookies, timeout=TIMEOUT)
            if not response.ok: return None
            try:
                data = response.json()
            except ValueError:
                return None
            if isinstance(data, dict):
                return data.get("domains", data.get("data", []))
            elif isinstance(data, list):
                return data
            return []
        except requests.RequestException:
            return None

    def get_days_left(self, expiry_str):
        if not expiry_str: return None
        try:
            expiry_date = datetime.datetime.strptime(str(expiry_str), "%Y%m%d")
            today = datetime.datetime.now().replace(hour=0, minute=0, second=0, microsecond=0)
            return (expiry_date - today).days
        except Exception:
            return None

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

        print("\n" + "=" * 70)
        print("开始检查域名")
        print("=" * 70)

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

            print("\n----------------------------------------")
            print("域名:", domain, "| ID:", _id, "| 到期:", expiry_str)

            if not expiry_str:
                msg = f"⚠️ {domain}: 没有找到 expiry_date"
                print(msg)
                return_msg += msg + "\n"
                continue

            days = self.get_days_left(expiry_str)
            if days is None: continue

            print("剩余:", days, "天")

            if days < RENEW_DAYS:
                msg = f"⚠️ {domain} 剩余 {days} 天，小于 {RENEW_DAYS} 天，正在续费..."
                print(msg)
                return_msg += msg + "\n"

                # ★ 修复：传入 domain
                if self.renew_domain(cookies, domain, _id):
                    renew_success += 1
                    return_msg += f"✅ {domain} 续费成功\n"
                else:
                    renew_failed += 1
                    return_msg += f"❌ {domain} 续费失败\n"
            else:
                msg = f"✅ {domain} 剩余 {days} 天，无需续费"
                print(msg)
                return_msg += msg + "\n"

        summary = f"📊 检查完成：{len(domains)} 个域名，续费成功 {renew_success} 个，失败 {renew_failed} 个"
        print(summary)
        return_msg += summary + "\n"
        return return_msg

    def jjjget_balance_with_token(self, page):
        self.log("开始执行保活与自动续费检查...", "STEP")
        return_msg = ""
        try:
            self.log("正在从浏览器获取登录 Cookie...", "INFO")
            cookie_string = page.evaluate("() => document.cookie")
            if not cookie_string:
                msg = "❌ 浏览器 document.cookie 为空"
                return return_msg + msg + "\n"
            
            cookies = self.parse_cookies(cookie_string)
            if not cookies:
                return return_msg + "❌ Cookie 解析失败\n"

            required_cookies = ["remember_token", "panel_session", "panel_csrf_token"]
            missing = [name for name in required_cookies if not cookies.get(name)]
            if missing:
                return_msg += "⚠️ Cookie 缺少字段: " + ", ".join(missing) + "\n"

            self.log("正在使用 requests 获取域名列表...", "INFO")
            result = self.check_and_renew(cookies)
            if result: return_msg += result
            time.sleep(2)
        except Exception as e:
            return_msg += f"❌ 续费流程异常: {e}\n"
        self.shot(page, "完成")
        return return_msg

    def get_balance_with_token(self, page):
        self.log("开始执行保活与自动续费检查...", "STEP")
        return_msg = ""
        try:
            self.log("正在获取域名列表并检查过期时间...", "INFO")
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
                            if (!expiryStr || expiryStr.length !== 8) {
                                logResults.push(`${domainName}: 日期格式异常 (${expiryStr})`);
                                continue;
                            }
                            const year = parseInt(expiryStr.substring(0, 4));
                            const month = parseInt(expiryStr.substring(4, 6)) - 1;
                            const day = parseInt(expiryStr.substring(6, 8));
                            const expiryDate = new Date(year, month, day);
                            const diffDays = Math.ceil((expiryDate.getTime() - today.getTime()) / (1000 * 60 * 60 * 24));

                            if (diffDays < 120) {
                                logResults.push(`${domainName}: 剩余 ${diffDays} 天 (< 120天)，正在触发续费...`);
                                const renewRes = await fetch(`https://api-domains.stackryze.com/subdomains/${domainName}/renew`, {
                                    headers: { ...commonHeaders, "content-type": "application/json" },
                                    referrer: `https://domain.stackryze.com/`,
                                    body: JSON.stringify({ "renewal_type": "free", "years": 1 }),
                                    method: "POST", credentials: "include"
                                });
                                if (renewRes.ok) {
                                    logResults.push(`${domainName}: ✅ 续费成功`);
                                } else {
                                    logResults.push(`${domainName}: ❌ 续费失败 [${renewRes.status}]`);
                                }
                            } else {
                                logResults.push(`${domainName}: 剩余 ${diffDays} 天 (>= 120天)，无需续费`);
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
                    self.log(log_item, "INFO")
                    return_msg += log_item + "\n"
                self.log("所有域名轮询检查完毕！", "SUCCESS")
                return_msg += "✅ 所有域名轮询检查完毕！\n"
            else:
                err_msg = result_data.get("error") if result_data else "未知错误"
                self.log(f"执行轮询续费脚本失败: {err_msg}", "WARN")
                return_msg += f"⚠️ 执行轮询续费脚本失败: {err_msg}\n"
            time.sleep(2)
        except Exception as e:
            return_msg += f"❌ 续费流程异常: {e}\n"
        self.shot(page, "完成")
        return return_msg

    def get_storage(self, context):
        try:
            state = context.storage_state()
            self.dt_local = state
            return state
        except Exception as e:
            self.log(f"获取 storage_state 失败: {e}", "WARN")
            return None

    def wait_device(self, page):
        self.log(f"需要设备验证，等待 {DEVICE_VERIFY_WAIT} 秒...", "WARN")
        self.shot(page, "设备验证")
        self.notify.send(title="stackryze 自动登录保活", content=f"⚠️ <b>需要设备验证</b>\n请在 {DEVICE_VERIFY_WAIT} 秒内批准")
        for i in range(DEVICE_VERIFY_WAIT):
            time.sleep(1)
            if i % 5 == 0:
                url = page.url
                if 'verified-device' not in url and 'device-verification' not in url:
                    return True
                try:
                    page.reload(timeout=10000)
                    page.wait_for_load_state('networkidle', timeout=10000)
                except: pass
        return 'verified-device' not in page.url

    def wait_two_factor_mobile(self, page):
        self.log(f"需要两步验证（GitHub Mobile），等待 {TWO_FACTOR_WAIT} 秒...", "WARN")
        shot = self.shot(page, "两步验证_mobile")
        self.notify.send(title="stackryze 自动登录保活", content=f"⚠️ <b>需要两步验证（GitHub Mobile）</b>\n请打开手机批准")
        for i in range(TWO_FACTOR_WAIT):
            time.sleep(1)
            url = page.url
            if "github.com/sessions/two-factor/" not in url:
                return True
            if "github.com/login" in url:
                return False
            if i % 30 == 0 and i != 0:
                try:
                    page.reload(timeout=30000)
                    page.wait_for_load_state('domcontentloaded', timeout=30000)
                except: pass
        return False

    def handle_2fa_code_input(self, page):
        self.log("需要输入验证码", "WARN")
        shot = self.shot(page, "两步验证_code")

        if 'two-factor/webauthn' in page.url:
            try:
                more_options_button = page.locator('button:has-text("More options")').first
                if more_options_button.is_visible(timeout=3000):
                    more_options_button.click()
                    time.sleep(1)
                    auth_app_button = page.locator('button:has-text("Authenticator app")').first
                    if auth_app_button.is_visible(timeout=2000):
                        auth_app_button.click()
                        time.sleep(2)
            except Exception: pass

        self.notify.send(title="stackryze 自动登录保活", content=f"🔐 <b>需要验证码登录</b>\n请在 Telegram 发送 /code 你的6位验证码")
        code = '真需要的话使用库自动生成'
        if not code: return False

        selectors = ['input[autocomplete="one-time-code"]', 'input[name="app_otp"]', 'input[name="otp"]', 'input#app_totp', 'input#otp', 'input[inputmode="numeric"]']
        for sel in selectors:
            try:
                el = page.locator(sel).first
                if el.is_visible(timeout=2000):
                    el.click()
                    el.type(code, delay=random.randint(50, 150))
                    time.sleep(1)
                    submitted = False
                    for btn_sel in ['button:has-text("Verify")', 'button[type="submit"]']:
                        try:
                            btn = page.locator(btn_sel).first
                            if btn.is_visible(timeout=1000):
                                btn.click()
                                submitted = True
                                break
                        except: pass
                    if not submitted: page.keyboard.press("Enter")
                    time.sleep(3)
                    if "github.com/sessions/two-factor/" not in page.url:
                        return True
                    return False
            except: pass
        return False

    def login_github(self, page, context):
        self.log("登录 GitHub...", "STEP")
        self.shot(page, "github_登录页")

        if not getattr(self, 'password', None):
            return False

        try:
            user_input = page.locator('input[name="login"]')
            user_input.click()
            user_input.type(self.gh_username, delay=random.randint(30, 100))
            time.sleep(random.uniform(0.5, 1.0))
            pass_input = page.locator('input[name="password"]')
            pass_input.click()
            pass_input.type(self.password, delay=random.randint(30, 100))
        except Exception:
            return False

        try:
            page.locator('input[type="submit"], button[type="submit"]').first.click()
        except: pass

        time.sleep(3)
        page.wait_for_load_state('networkidle', timeout=30000)
        
        url = page.url
        if 'verified-device' in url or 'device-verification' in url:
            if not self.wait_device(page): return False
            time.sleep(2)
            page.wait_for_load_state('networkidle', timeout=30000)

        if 'two-factor' in page.url:
            if 'two-factor/mobile' in page.url:
                if not self.wait_two_factor_mobile(page): return False
            else:
                if not self.handle_2fa_code_input(page): return False
            try:
                page.wait_for_load_state('networkidle', timeout=30000)
                time.sleep(2)
            except: pass
        return True

    def oauth(self, page):
        if 'github.com/login/oauth/authorize' in page.url:
            self.log("处理 OAuth...", "STEP")
            self.jclick(page, ['button[name="authorize"]', 'button:has-text("Authorize")'], "授权")
            time.sleep(3)
            page.wait_for_load_state('networkidle', timeout=30000)

    def jclick(self, page, selectors, desc=""):
        for sel in selectors:
            try:
                el = page.locator(sel).first
                if el.is_visible(timeout=3000):
                    el.click(timeout=10000)
                    return True
            except Exception: continue
        return False

    def check_and_process_domain(self, domain):
        if "help.github.com/request/landing" in domain: return "signin"
        if domain.endswith('domain.stackryze.com/login'): return "signin"
        if "github.com/login" in domain or "github.com/sessions/" in domain: return "github_login"
        if "stackryze.com" in domain and "callback" in domain: return "redirect"
        if domain.endswith('.stackryze.com/my-domains'): return "logged"
        return "invalid"

    def run(self):
        ok, new_local, msg = False, None, ""
        self.log(f"用户名: {mask_name(self.gh_username)}")
        self.log(f"Session: {'有' if self.gh_session else '无'}")

        if not self.gh_username:
            return False, None, f"❌ 缺少凭据"

        with sync_playwright() as p:
            launch_args = {
                "headless": True,
                # ★ 补丁1：优先使用真实 Chrome 和 new Headless 模式对抗 CF 检测
                "channel": "chrome",
                "args": [
                    '--no-sandbox',
                    '--disable-dev-shm-usage',
                    '--headless=new',
                    '--disable-blink-features=AutomationControlled',
                    '--disable-infobars',
                    '--exclude-switches=enable-automation',
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
            page.add_init_script("""
                Object.defineProperty(navigator, 'webdriver', { get: () => undefined });
                Object.defineProperty(navigator, 'plugins', { get: () => [1, 2, 3, 4, 5] });
                Object.defineProperty(navigator, 'languages', { get: () => ['en-US', 'en'] });
                window.chrome = { runtime: {} };
                const originalQuery = window.navigator.permissions.query;
                window.navigator.permissions.query = (parameters) => (
                    parameters.name === 'notifications' ?
                    Promise.resolve({ state: Notification.permission }) :
                    originalQuery(parameters)
                );
            """)

            try:
                if self.gh_session:
                    try:
                        context.add_cookies([
                            {'name': 'user_session', 'value': self.gh_session, 'domain': 'github.com', 'path': '/'},
                            {'name': 'logged_in', 'value': 'yes', 'domain': 'github.com', 'path': '/'}
                        ])
                        self.log("已加载 Session Cookie", "SUCCESS")
                    except: pass

                self.log("步骤1: 打开 stackryze 登录页", "STEP")

                for i in range(10):
                    try:
                        page.goto(BOARD_ENTRY_URL, timeout=60000)
                        page.wait_for_load_state('domcontentloaded', timeout=60000)
                        time.sleep(random.uniform(15, 20))

                        # ★ 补丁2：CF 验证检测与等待
                        if "Performing security verification" in page.title() or "cloudflare" in page.content().lower():
                            self.log("⚠️ 检测到 Cloudflare 安全验证！", "WARN")
                            shot = self.shot(page, "CF_安全验证拦截")
                            if shot: self.notify.send(title="stackryze 异常", content="遇到 CF 验证拦截，正在尝试等待", image_path=shot)
                            
                            cf_passed = False
                            for cf_wait in range(30):
                                if "Performing security verification" not in page.title():
                                    cf_passed = True
                                    break
                                time.sleep(1)
                                if cf_wait == 15:
                                    try: page.reload()
                                    except: pass
                            if not cf_passed:
                                self.log("❌ CF 验证超时，尝试重新进入循环...", "ERROR")
                                continue

                        shot = self.shot(page, "打开 stackryze 登录页")
                        if shot: self.notify.send(title="stackryze 自动登录保活", content="打开 stackryze 登录页", image_path=shot)

                        self.log("正在等待 GitHub 登录按钮渲染...", "INFO")
                        try:
                            if "help.github.com/request/landing" in page.url:
                                page.get_by_role("button", name=re.compile(r"^Sign in with GitHub$", re.I)).wait_for(state="visible", timeout=15000)
                            else:
                                page.wait_for_selector('a[href="/auth/login/github"]', timeout=60000, state="visible")
                        except Exception as e:
                            self.log(f"等待 GitHub 按钮超时或未显现！: {e}", "WARN")

                        resault = self.check_and_process_domain(page.url)
                        if resault == "invalid": continue
                        if resault == "logged": break
                        if resault == "signin":
                            if not self.click(page, desc="GitHub 登录按钮"):
                                continue
                            else:
                                for j in range(10):
                                    resault = self.check_and_process_domain(page.url)
                                    if resault == "signin":
                                        if not self.click(page, desc="GitHub 登录按钮"): continue
                                        continue
                                    if resault == "github_login":
                                        if not self.login_github(page, context):
                                            return False, None, "❌ GitHub 登录失败"
                                        try: page.wait_for_load_state('networkidle', timeout=30000)
                                        except: pass
                                        self.oauth(page)
                                        continue
                                    if resault == "logged":
                                        break
                                    if resault == "redirect":
                                        not_bound_selector = 'p.text-slate-600:has-text("This GitHub account is not bound")'
                                        try:
                                            if page.locator(not_bound_selector).is_visible(timeout=5000):
                                                return False, None, "❌ GitHub 账号未绑定到平台！"
                                        except: pass
                                        try:
                                            page.wait_for_url("https://*.stackryze.com", timeout=60000)
                                            break
                                        except PlaywrightTimeoutError: pass
                                        continue
                                    time.sleep(2)
                    except Exception as e:
                        if i < 10:
                            time.sleep(random.uniform(10, 15))
                        else:
                            browser.close()
                            return False, None, f"访问 {BOARD_ENTRY_URL} 失败！"

                self.log("步骤3: 更新 local_storage", "STEP")
                storage_state = self.get_storage(context)
                if storage_state:
                    slimmest_local = slim_storage_state(storage_state)
                    final_json = json.dumps(slimmest_local, ensure_ascii=False)
                    storage_state_b64 = base64.b64encode(final_json.encode("utf-8")).decode("utf-8")
                    ok = True
                    new_local = storage_state_b64

                self.log("步骤4: 查询有效期信息", "STEP")
                msg += self.get_balance_with_token(page)

                if self.shots:
                    self.notify.send(title="stackryze 自动登录保活", content=f"✅ {self.gh_username}成功！", image_path=self.shots[-1])
            except Exception as e:
                import traceback
                traceback.print_exc()
                msg = f"访问 {page.url} 失败！"
            finally:
                if browser: browser.close()
                return ok, new_local, msg


def main():
    global config
    if config is None:
        config = ConfigReader()
    newcookies = {}
    results = []

    accounts = config.get_value("GH_INFO")
    proxies = config.get_value("WZ_INFO")

    notify = get_notifier()
    gh_secret = SecretUpdater("GH_SESSION", config_reader=config)
    gh_sessions = gh_secret.load() or {}

    secret = SecretUpdater("STACKRYZE_LOCALS", config_reader=config)
    dt_locals = secret.load()

    if not accounts:
        print("❌ 错误: 未配置 GH_INFO")
        return
    if not proxies:
        print("📢 警告: 未配置 proxy ，将直连")
        proxies = []

    print(f"📊 检测到 {len(accounts)} 个账号和 {len(proxies)} 个代理")

    # 兼容没有代理的情况
    proxy_iter = iter(proxies) if proxies else iter([None] * len(accounts))

    for account, proxy in zip(accounts, proxy_iter):
        username = account['username']
        if username == 'you5102': continue

        print(f"\n🚀 开始处理账号: {mask_name(username)}")
        results.append(f"🚀 账号：{mask_name(username)}\n")
        dt_info = {
            'gh_username': username,
            'gh_password': account.get('password'),
            'dt_proxy': proxy,
            'notify': notify,
            'wz_proxy': proxies[-1] if proxies else None
        }

        if isinstance(gh_sessions, dict):
            gh_session = gh_sessions.get(username, '')
            if isinstance(gh_session, list): gh_session = gh_session[0] if gh_session else ''
            dt_info['gh_session'] = gh_session
        else:
            dt_info['gh_session'] = ''

        if not gh_session:
            print(f"⚠️ 缺少对应账号的 gh_session ，退出！")
            continue

        if isinstance(dt_locals, dict):
            dt_local = dt_locals.get(username, '')
            if dt_local:
                try:
                    dt_info['dt_local'] = json.loads(base64.b64decode(dt_local).decode("utf-8"))
                except Exception as e: pass
        else:
            dt_info['dt_local'] = []

        try:
            auto_login = AutoLogin(dt_info)
            ok, new_local, msg = auto_login.run()

            if ok:
                print(f"    ✅ 执行成功")
                results.append(f"    ✅ {msg}\n")
                if new_local:
                    dt_locals[username] = new_local
            else:
                results.append(f"    ⚠️ 执行失败:{msg}\n")
        except Exception as e:
            results.append(f"    ❌ 执行异常: {e}\n")
            
    secret.update(dt_locals)
    notify.send(title="stackryze 自动登录保活汇总", content="\n".join(results))

if __name__ == "__main__":
    main()
