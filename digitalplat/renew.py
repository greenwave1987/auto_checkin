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

# 固定自己创建有APP的登录入口，若BOARD_ENTRY_URL = "https://console.run.digitalplat.org/signin"在OAuth后会自动跳转到根据IP定位的区域,
BOARD_ENTRY_URL = "https://dash.domain.digitalplat.org/domains"

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
def slim_storage_state(state):
    """
    精简 storage_state，只保留核心登录凭据，防止超过 GitHub 64KB 限制
    """
    if not isinstance(state, dict):
        return state

    # 1. 精简 Cookies：只保留 .digitalplat.org 的
    if "cookies" in state:
        state["cookies"] = [
            c for c in state["cookies"] 
            if "digitalplat.org" in c.get("domain", "")
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
        # self.gh_password = config.get('gh_password')
        
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
        self.proxy_url=test_proxy(self.dt_proxy)
        if not self.proxy_url:
            self.dt_proxy = config.get('wz_proxy')
            
        self.notify = config.get('notify')
        # self.secret = SecretUpdater()
        self.shots = []
        self.logs = []
        self.n = 0
        
        # 区域相关
        
        self.region_base_url = 'https://ap-northeast-1.run.digitalplat.org'  # 检测到的区域基础 URL
        

        
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
            # 🚀 【新增最高优先级】针对当前系统的超精准 href 选择器，秒杀一切
            'a[href="/auth/login/github"]',
            
            # 原有的兜底规则
            'button:has-text("GitHub")',
            'a:has-text("GitHub")',
            '[data-provider="github"]',
            
            # Chakra Button
            'button.chakra-button',
    
            # 带 GitHub svg 的按钮
            'button:has(svg)',
    
            # XPath 兜底
            '//button[.//text()[contains(., "GitHub")]]',
            '//button[.//*[name()="svg"]]'
        ]
    
        for frame in frames:
            for sel in selectors:
                try:
                    el = frame.locator(sel).first
    
                    # 💡 注意：将最精准的前几条快速过一遍，建议降低单个规则的等待超时（从5s降到2s），提升整体扫描效率
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
        
    def get_digitalplat_cookies(self):
        """
        从 storage_state 中提取 domain 包含 digitalplat.org 的 cookies
        """
        if not isinstance(self.dt_local, dict):
            return []
    
        cookies = self.dt_local.get("cookies", [])
        if not isinstance(cookies, list):
            return []
    
        return [
            c for c in cookies
            if isinstance(c, dict) and "domain" in c and "digitalplat.org" in c["domain"]
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
            # 核心逻辑改进：检查当前 self.host 是否在该 origin 字符串中
            # 比如 host 是 'ap-northeast-1.run.digitalplat.org'，匹配 'https://ap-northeast-1.run.digitalplat.org'
            if self.host in origin_url:
                self.log(f"✅ 成功匹配存储域: {origin_url}", "SUCCESS")
                return o.get("localStorage", [])
                
        return []
    def jjjget_local_storage_by_origin(self):
        """
        根据 origin 获取对应的 localStorage
        """
        if not isinstance(self.dt_local, dict):
            self.log(f"❌ get_local_storage_by_origin: self.dt_local格式不对 {self.dt_local}", "ERROR")
            return []
    
        origins = self.dt_local.get("origins", [])
        if not isinstance(origins, list):
            self.log(f"❌ get_local_storage_by_origin: origins格式不对 {origins}", "ERROR")
            return []
    
        for o in origins:
            if not isinstance(o, dict):
                self.log(f"❌ get_local_storage_by_origin: origin格式不对 {o}", "ERROR")
                continue
            if self.host in o.get("origin"):
                return o.get("localStorage", [])
        self.log(f"❌ get_local_storage_by_origin: []", "ERROR")
        return []
    
        
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
        cookies=self.get_digitalplat_cookies()
        
        try:
            s = requests.Session()
            s.headers.update({
                    "authority": self.host,
                    "accept": "application/json, text/plain, */*",
                    "authorization": "Bearer "+token, # 纯 Token 模式
                    "referer": f"https://{self.host}/",
                    "user-agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/119.0.0.0 Safari/537.36"
            })
                
            for c in cookies:
                s.cookies.set(c["name"], c["value"])
            return s
        except Exception as e:
            print(f"⚠️ [build_session 异常] {e}")
            return None

    def get_balance_with_token(self, page):
        """保活并自动续费（剩余不足120天）"""
        self.log("开始执行保活与自动续费检查...", "STEP")
        
        # 📌 初始化准备返回给 msg 的文本内容
        return_msg = ""
        
        try:
            self.log("正在获取域名列表并检查过期时间...", "INFO")
            
            # 使用 page.evaluate 在浏览器内完成：获取数据 -> 轮询判断 -> 触发续费
            result_data = page.evaluate("""
                async () => {
                    try {
                        // 尝试从本地 Cookie 中提取 XSRF-TOKEN 以免401
                        const getCookie = (name) => {
                            const value = `; ${document.cookie}`;
                            const parts = value.split(`; ${name}=`);
                            if (parts.length === 2) return parts.pop().split(';').shift();
                            return null;
                        };
                        const xsrfToken = getCookie('XSRF-TOKEN');

                        // 1. 先获取所有域名数据
                        const response = await fetch("https://dash.domain.digitalplat.org/_panel_api/api/domains", {
                            "headers": {
                                "accept": "application/json, text/plain, */*",
                                "accept-language": "zh-CN,zh;q=0.9",
                                "cache-control": "no-cache",
                                "pragma": "no-cache",
                                "X-Requested-With": "XMLHttpRequest", // 📌 核心：声明是Ajax异步请求，防401
                                ...(xsrfToken && { "X-XSRF-TOKEN": decodeURIComponent(xsrfToken) })
                            },
                            "referrer": "https://dash.domain.digitalplat.org/domains",
                            "method": "GET",
                            "credentials": "include"
                        });
                        
                        if (!response.ok) {
                            return { success: false, error: `获取列表失败，HTTP 状态码: ${response.status}` };
                        }
                        
                        const resData = await response.json();
                        const domains = resData.domains || [];
                        const logResults = [];
                        
                        // 2. 获取当前日期并转换为时间戳
                        const today = new Date();
                        
                        // 3. 轮询遍历每一个域名
                        for (const item of domains) {
                            const domainName = item.domain;
                            const expiryStr = item.expiry_date; // 格式: "20261120"
                            
                            if (!expiryStr || expiryStr.length !== 8) {
                                logResults.push(`${domainName}: 日期格式异常 (${expiryStr})`);
                                continue;
                            }
                            
                            // 解析 "YYYYMMDD"
                            const year = parseInt(expiryStr.substring(0, 4));
                            const month = parseInt(expiryStr.substring(4, 6)) - 1; // JS 月份从 0 开始
                            const day = parseInt(expiryStr.substring(6, 8));
                            const expiryDate = new Date(year, month, day);
                            
                            // 计算相差天数
                            const diffTime = expiryDate.getTime() - today.getTime();
                            const diffDays = Math.ceil(diffTime / (1000 * 60 * 60 * 24));
                            
                            // 4. 判断是否小于 120 天
                            if (diffDays < 120) {
                                logResults.push(`${domainName}: 剩余 ${diffDays} 天 (< 120天)，正在触发续费...`);
                                
                                // 动态构建续费 URL 并发送 POST 请求
                                const renewRes = await fetch(`https://dash.domain.digitalplat.org/_panel_api/api/domains/${domainName}/renew`, {
                                    "headers": {
                                        "accept": "application/json, text/plain, */*",
                                        "accept-language": "zh-CN,zh;q=0.9",
                                        "cache-control": "no-cache",
                                        "content-type": "application/json",
                                        "pragma": "no-cache",
                                        "X-Requested-With": "XMLHttpRequest", // 📌 同样加上防401
                                        ...(xsrfToken && { "X-XSRF-TOKEN": decodeURIComponent(xsrfToken) })
                                    },
                                    "referrer": `https://dash.domain.digitalplat.org/domains/${domainName}`,
                                    "body": JSON.stringify({ "renewal_type": "free", "years": 1 }),
                                    "method": "POST",
                                    "credentials": "include"
                                });
                                
                                if (renewRes.ok) {
                                    logResults.push(`${domainName}: 续费请求成功发送！`);
                                } else {
                                    logResults.push(`${domainName}: 续费请求失败，状态码: ${renewRes.status}`);
                                }
                            } else {
                                logResults.push(`${domainName}: 剩余 ${diffDays} 天 (>= 120天)，无需续费。`);
                            }
                        }
                        
                        return { success: true, logs: logResults };
                    } catch (error) {
                        return { success: false, error: error.message };
                    }
                }
            """)
            
            # 在 Python 控制台输出执行日志并拼接给 return_msg
            if result_data and result_data.get("success"):
                for log_item in result_data.get("logs", []):
                    self.log(log_item, "INFO")
                    return_msg += log_item + "\n" # 📌 将每行日志加到通知文本中
                self.log("所有域名轮询检查完毕！", "SUCCESS")
                return_msg += "✅ 所有域名轮询检查完毕！\n"
            else:
                err_msg = result_data.get("error") if result_data else "未知错误"
                self.log(f"执行轮询续费脚本失败: {err_msg}", "WARN")
                return_msg += f"⚠️ 执行轮询续费脚本失败: {err_msg}\n"
                
            time.sleep(2)
        except Exception as e:
            self.log(f"续费流程异常: {e}", "WARN")
            return_msg += f"❌ 续费流程异常: {e}\n"
            
        self.shot(page, "完成")
        
        # 📌 极其重要：把拼接好的字符串返回，让外部的 msg += 能够正常执行
        return return_msg
    
    def mask_url(self,url):
        url = re.sub(r'code=[^&]+', 'code=***', url)
        url = re.sub(r'state=[^&]+', 'state=***', url)
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
            self.notify.send(title="digitalplat 自动登录保活",content="🔑 <b>Cookie 已自动更新</b>\n\nGH_SESSION 已保存")
        else:
            # 通过 Telegram 发送
            self.notify.send(title="digitalplat 自动登录保活",content=f"""🔑 <b>新 Cookie</b>

请更新 Secret <b>GH_SESSION</b> (点击查看):
<tg-spoiler>{value}</tg-spoiler>
""")
            self.log("已通过 Telegram 发送 Cookie", "SUCCESS")
    
    def wait_device(self, page):
        """等待设备验证"""
        self.log(f"需要设备验证，等待 {DEVICE_VERIFY_WAIT} 秒...", "WARN")
        self.shot(page, "设备验证")
        
        self.notify.send(title="digitalplat 自动登录保活",content=f"""⚠️ <b>需要设备验证</b>

请在 {DEVICE_VERIFY_WAIT} 秒内批准：
1️⃣ 检查邮箱点击链接
2️⃣ 或在 GitHub App 批准""")
        
        if self.shots:
            self.notify.send(title="digitalplat 自动登录保活",content="设备验证页面",image_path=self.shots[-1])
        
        for i in range(DEVICE_VERIFY_WAIT):
            time.sleep(1)
            if i % 5 == 0:
                self.log(f"  等待... ({i}/{DEVICE_VERIFY_WAIT}秒)")
                url = page.url
                if 'verified-device' not in url and 'device-verification' not in url:
                    self.log("设备验证通过！", "SUCCESS")
                    self.notify.send(title="digitalplat 自动登录保活",content="✅ <b>设备验证通过</b>")
                    return True
                try:
                    page.reload(timeout=10000)
                    page.wait_for_load_state('networkidle', timeout=10000)
                except:
                    pass
        
        if 'verified-device' not in page.url:
            return True
        
        self.log("设备验证超时", "ERROR")
        self.notify.send(title="digitalplat 自动登录保活",content="❌ <b>设备验证超时</b>")
        return False
    
    def wait_two_factor_mobile(self, page):
        """等待 GitHub Mobile 两步验证批准，并把数字截图提前发到电报"""
        self.log(f"需要两步验证（GitHub Mobile），等待 {TWO_FACTOR_WAIT} 秒...", "WARN")
        
        # 先截图并立刻发出去（让你看到数字）
        shot = self.shot(page, "两步验证_mobile")
        self.notify.send(title="digitalplat 自动登录保活",content=f"""⚠️ <b>需要两步验证（GitHub Mobile）</b>

请打开手机 GitHub App 批准本次登录（会让你确认一个数字）。
等待时间：{TWO_FACTOR_WAIT} 秒""")
        if shot:
            self.notify.send(title="digitalplat 自动登录保活",content="两步验证页面（数字在图里）",mage_path=shot)
        
        # 不要频繁 reload，避免把流程刷回登录页
        for i in range(TWO_FACTOR_WAIT):
            time.sleep(1)
            
            url = page.url
            
            # 如果离开 two-factor 流程页面，认为通过
            if "github.com/sessions/two-factor/" not in url:
                self.log("两步验证通过！", "SUCCESS")
                self.notify.send(title="digitalplat 自动登录保活",content="✅ <b>两步验证通过</b>")
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
                    self.notify.send(title="digitalplat 自动登录保活",content=f"两步验证页面（第{i}秒）",image_path=shot)
            
            # 只在 30 秒、60 秒... 做一次轻刷新（可选，频率很低）
            if i % 30 == 0 and i != 0:
                try:
                    page.reload(timeout=30000)
                    page.wait_for_load_state('domcontentloaded', timeout=30000)
                except:
                    pass
        
        self.log("两步验证超时", "ERROR")
        self.notify.send(title="digitalplat 自动登录保活",content="❌ <b>两步验证超时</b>")
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
        self.notify.send(title="digitalplat 自动登录保活",content=f"""🔐 <b>需要验证码登录</b>

用户{self.gh_username}正在登录，请在 Telegram 里发送：
<code>/code 你的6位验证码</code>

等待时间：{TWO_FACTOR_WAIT} 秒""")
        if shot:
            self.notify.send(title="digitalplat 自动登录保活",content="两步验证页面",image_path=shot)

        self.log(f"等待验证码（{TWO_FACTOR_WAIT}秒）...", "WARN")
        code = '真需要的话使用库自动生成'

        if not code:
            self.log("等待验证码超时", "ERROR")
            self.notify.send(title="digitalplat 自动登录保活",content="❌ <b>等待验证码超时</b>")
            return False

        # 不打印验证码明文，只提示收到
        self.log("收到验证码，正在填入...", "SUCCESS")
        self.notify.send(title="digitalplat 自动登录保活",content="✅ 收到验证码，正在填入...")

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
                        self.notify.send(title="digitalplat 自动登录保活",content="✅ <b>验证码验证通过</b>")
                        return True
                    else:
                        self.log("验证码可能错误", "ERROR")
                        self.notify.send(title="digitalplat 自动登录保活",content="❌ <b>验证码可能错误，请检查后重试</b>")
                        return False
            except:
                pass

        self.log("没找到验证码输入框", "ERROR")
        self.notify.send(title="digitalplat 自动登录保活",content="❌ <b>没找到验证码输入框</b>")
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
            
            # 检查是否已跳转到 digitalplat.org
            if 'digitalplat.org' in url and 'login' not in url.lower():
                self.log("重定向成功！", "SUCCESS")
                

                return True
            
            if 'github.com/login/oauth/authorize' in url:
                self.oauth(page)
            
            time.sleep(1)
            if i % 10 == 0:
                self.log(f"  等待... ({i}秒)")
        
        self.log("重定向超时", "ERROR")
        return False
    
    

    def check_and_process_domain(self, domain):

        # 去掉末尾斜杠
        #domain = domain.rstrip('/')
        self.log(f"检查网址: {domain}")
        # 检查是否为 signin 页面
        if domain.endswith('digitalplat.org/auth/login'):
            return "signin"
    
        # 检查是否包含 callback（OAuth 重定向）
        if "callback" in domain:
            return "redirect"
    
        # 检查是否是正常已登录的区域域名
        if domain.endswith('.digitalplat.org/domains'):
            return "logged"
    
        # 其他情况
        return "invalid"
    
    def run(self):

        ok, new_local,msg = False,  None, ""
        self.log(f"用户名: {mask_name(self.gh_username)}")
        self.log(f"Session: {'有' if self.gh_session else '无'}")
        #self.log(f"密码: {'有' if self.password else '无'}")
        self.log(f"登录入口: {BOARD_ENTRY_URL}")
        
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

            if self.dt_proxy:
                try:
                    p_url = self.dt_proxy
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
                        
                # 1. 访问 digitalplat 登录入口
                self.log("步骤1: 打开 digitalplat 登录页", "STEP")

                for i in range(10):
                    try:
                        page.goto(BOARD_ENTRY_URL, timeout=60000)
                        page.wait_for_load_state('domcontentloaded', timeout=60000)
                        # 2. 精准等待 GitHub 登录按钮在页面上出现
                        self.log("正在等待 GitHub 登录按钮渲染...", "INFO")
                        try:
                            # 盯防 href 包含 /auth/login/github 的 a 标签
                            github_btn_selector = 'a[href="/auth/login/github"]'
                            page.wait_for_selector(github_btn_selector, timeout=60000, state="visible")
                            self.log("成功检测到 GitHub 登录按钮！", "SUCCESS")
                        except Exception as e:
                            self.log(f"等待 GitHub 按钮超时或未显现: {e}", "WARN")
                            
                        resault=self.check_and_process_domain(page.url)
                        self.log(f"检测结果: {resault}", "INFO")
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
                                    self.notify.send(title="digitalplat 自动登录保活",content="找不到 GitHub 按钮",image_path=shot)
                                self.log(f"[2.{i}]: 找不到 GitHub 按钮", "WARN")
                                self.shot(page, "找不到 GitHub 按钮")
                                continue
                            else:
                                for j in range(10):
                                    resault=self.check_and_process_domain(page.url)
                                    if resault=="signin":
                                        self.log(f"[2.{i}.{j}]: 未跳转: {page.url}", "INFO")
                                        time.sleep(random.uniform(10, 20))
                                        continue
                                    if resault=="logged":
                                        self.log(f"[2.{i}.{j}]: 已登录: {page.url}", "SUCCESS")
                                        self.shot(page, "找不到 GitHub 按钮")
                                        break
                                    if resault=="redirect":
                                        self.log(f"[2.{i}.{j}]: 正在重定向: {self.mask_url(page.url)}", "INFO")
                                        try:
                                            page.wait_for_url("https://*.digitalplat.org", timeout=60000)
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
                    except Exception as e:
                
                        if i <10:
                            self.log(f"异常: {e}", "ERROR")
                            self.log(f"[1.{i}]: 未打开登录页，重试", "WARN")
                            time.sleep(random.uniform(10, 15))
                        else:
                            self.log(f"[1.{i}]: 访问 {page.url} 失败！", "ERROR")
                            browser.close()
                            return False,  None, f"访问 {BOARD_ENTRY_URL} 失败！"   
                         
                               
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
                
                # 4. 查询有效期信息
                self.log("步骤4: 查询有效期信息", "STEP")
                
                msg+=self.get_balance_with_token(page)
                #msg+= "✅ 成功！"
                print("\n" + "="*50)
                
                
                print("="*50 + "\n")
                if self.shots:
                    self.notify.send(title="digitalplat 自动登录保活",content=f"✅ {self.gh_username}成功！",image_path=self.shots[-1])
            except Exception as e:
                self.log(f"异常: {e}", "ERROR")
                self.shot(page, "异常")
                import traceback
                traceback.print_exc()
                if self.shots:
                    self.notify.send(title="digitalplat 自动登录保活",content=f"❌ {self.gh_username}:{str(e)}",image_path=self.shots[-1])
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
    secret = SecretUpdater("DIGITALPLAT_LOCALS", config_reader=config)
    # 读取
    dt_locals = secret.load() 
    

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
        print("\n" + "="*50)
        print(f"\n🚀 开始处理账号: {mask_name(username)}\n  🌐 使用代理: {proxy['server'][:-4]}***\n")
        print("="*50 + "\n")
        
        results.append(f"🚀 账号：{mask_name(username)}\n    🌐 使用代理: {proxy['server'][:-4]}***\n")
        dt_info={}
        dt_info['gh_username'] = username
        #dt_info['gh_password'] = account.get('password')
        dt_info['dt_proxy'] = proxy
        dt_info['notify'] = notify
        dt_info['wz_proxy'] = proxies[-1]

        if isinstance(gh_sessions, dict):
            gh_session = gh_sessions.get(username,'')
            if isinstance(gh_session, list):
                gh_session = gh_session[0] if gh_session else ''
            dt_info['gh_session'] = gh_session
        else:
            print(f"⚠️ gh_sessions 格式错误！")
            dt_info['gh_session'] = ''

        if not gh_session:
            print(f"⚠️ 缺少对应账号的 gh_session ，退出！")
            continue
        
        if isinstance(dt_locals, dict):
            # 预加载localStorage数据，验证有效不再使用 gh_session
            storage_state = None
            dt_local=dt_locals.get(username,'')
            if dt_local:
                try:
                    dt_info['dt_local'] =  json.loads(base64.b64decode(dt_local).decode("utf-8"))
                    print("✅ 已加载 storage_state")
                except Exception as e:
                    print(f"❌ 加载 storage_state 失败: {e}")
        else:
            print(f"⚠️ dt_locals 格式错误！{dt_locals}")
            dt_locals={}
            dt_info['dt_local'] = []

        try:

            auto_login= AutoLogin(dt_info)
            ok, new_local,msg = auto_login.run()
    
            if ok:
                print(f"    ✅ 执行成功")
                results.append(f"    ✅ {msg}\n")
                if new_local:
                    print(f"    ✅ 保存新 new_local")
                    dt_locals[username]=new_local
            else:
                print(f"    ⚠️ 执行失败，不保存 cookie")
                results.append(f"    ⚠️ 执行失败:{msg}\n")
    
        except Exception as e:
            print(f"    ❌ 执行异常: {e}")
            results.append(f"    ❌ 执行异常: {e}")
        #break
    # 写入
    # 转换为 JSON 字符串前可以检查下大小
    print(f"dt_locals数据大小: {len(json.dumps(dt_locals)) / 1024:.2f} KB")
    secret.update(dt_locals)
    # 发送结果
    notify.send(
        title="digitalplat 自动登录保活汇总",
        content="\n".join(results)
    )


if __name__ == "__main__":
    main()
