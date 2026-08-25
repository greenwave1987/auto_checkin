import os
import re
import sys
import json
import time
import base64
import random
from urllib.parse import urlparse
from playwright.sync_api import sync_playwright
from playwright.sync_api import TimeoutError as PlaywrightTimeoutError

# ==================== 基准数据对接 ====================
BASE_URL = "https://agentrouter.org"
RENEW_DAYS = 120
TIMEOUT = 15
BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BASE_DIR)

from engine.notify import TelegramNotifier
try:
    from engine.main import ConfigReader, SecretUpdater, test_proxy
except ImportError:
    class ConfigReader:
        def get_value(self, key): return os.environ.get(key)
    class SecretUpdater:
        def __init__(self, name=None, config_reader=None): pass
        def update(self, value): return False

# ==================== 配置 ====================
PROXY_DSN = os.environ.get("PROXY_DSN", "").strip()
BOARD_ENTRY_URL = "https://agentrouter.org/login"

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
def mask_name(name: str):
    return f"{name[:2]}***{name[-2:]}"

def slim_storage_state(state):
    """精简 storage_state，只保留核心登录凭据"""
    if not isinstance(state, dict):
        return state

    if "cookies" in state:
        state["cookies"] = [
            c for c in state["cookies"] 
            if "agentrouter.org" in c.get("domain", "")
        ]

    if "origins" in state:
        new_origins = []
        essential_keys = ["session", "lastLoginUpdateTime", "i18nextLng"]
        
        for o in state["origins"]:
            storage = o.get("localStorage", [])
            slim_storage = [
                item for item in storage 
                if item.get("name") in essential_keys
            ]
            o["localStorage"] = slim_storage
            new_origins.append(o)
        
        state["origins"] = new_origins

    return state

class AutoLogin:
    """自动登录，注入 GH_SESSION 获取并同步 agentrouter LocalStorage"""
    
    def __init__(self, config):
        self.host = urlparse(BOARD_ENTRY_URL).netloc
        self.gh_username = config.get('gh_username')
        
        gh_sess = config.get('gh_session', '')
        if isinstance(gh_sess, str):
            self.gh_session = gh_sess.strip()
        elif isinstance(gh_sess, list):
            self.gh_session = gh_sess[0] if gh_sess else ''
        else:
            self.gh_session = ''
        
        ag_local_val = config.get('ag_local', '')
        if isinstance(ag_local_val, str):
            self.ag_local = ag_local_val.strip()
        else:
            self.ag_local = ag_local_val
        
        self.ag_proxy = config.get('ag_proxy')
        self.proxy_url = test_proxy(self.ag_proxy) if self.ag_proxy else None
        if not self.proxy_url:
            self.ag_proxy = config.get('wz_proxy')
            self.proxy_url = test_proxy(self.ag_proxy) if self.ag_proxy else None
            
        self.notify = config.get('notify')
        self.shots = []
        self.logs = []
        self.n = 0
        self.region_base_url = 'https://ap-northeast-1.run.agentrouter.org'
        
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
        except Exception:
            pass
        return f
    
    def click(self, page, desc=""):
        self.log(f"🔍 尝试查找并点击: {desc}", "INFO")
        try:
            page.wait_for_load_state("domcontentloaded", timeout=15000)
            page.wait_for_timeout(3000)
        except Exception:
            pass
    
        frames = page.frames
        selectors = [
            'button:has([aria-label="github_logo"])',
            'button:has-text("Continue with GitHub")',
            'button:has-text("GitHub")',
            'a[href="/auth/login/github"]',
            'a[href*="/auth/login/github"]',
            '[data-provider="github"]',
            '//button[contains(.,"Continue with GitHub")]',
            '//button[.//span[contains(@aria-label,"github")]]'
        ]
    
        for frame in frames:
            for sel in selectors:
                try:
                    el = frame.locator(sel).first
                    el.wait_for(state="visible", timeout=3000)
                    self.log(f"找到按钮: {sel}", "SUCCESS")
                    time.sleep(random.uniform(0.5, 1.2))
                    
                    try:
                        el.hover()
                    except Exception:
                        pass
    
                    time.sleep(random.uniform(0.2, 0.5))
                    el.click(force=True, timeout=5000)
                    self.log(f"已点击: {desc}", "SUCCESS")
                    time.sleep(random.uniform(5, 8))
                    return True
                except PlaywrightTimeoutError:
                    continue
                except Exception as e:
                    self.log(f"{sel} 点击失败: {e}", "DEBUG")
    
        self.log(f"❌ 找不到按钮: {desc}", "ERROR")
        return False

    def get_balance_with_token(self, page):
        """保活并自动续费（剩余不足120天）"""
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
                        
                        const response = await fetch("https://dashboard.agentrouter.org/_panel_api/api/domains", {
                            headers: commonHeaders,
                            referrer: "https://dashboard.agentrouter.org/domains",
                            method: "GET",
                            credentials: "include"
                        });
                        
                        if (!response.ok) {
                            return { 
                                success: false, 
                                error: `获取列表失败，HTTP 状态码: ${response.status}` 
                            };
                        }
                        
                        const resData = await response.json();
                        const domains = resData.domains || [];
                        const logResults = [];
                        
                        if (domains.length === 0) {
                            return { success: true, logs: ["当前账号下没有域名"] };
                        }
                        
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
                            
                            const diffTime = expiryDate.getTime() - today.getTime();
                            const diffDays = Math.ceil(diffTime / (1000 * 60 * 60 * 24));
                            
                            if (diffDays < 120) {
                                logResults.push(`${domainName}: 剩余 ${diffDays} 天 (< 120天)，正在触发续费...`);
                                
                                const renewRes = await fetch(
                                    `https://dashboard.agentrouter.org/_panel_api/api/domains/${domainName}/renew`, 
                                    {
                                        headers: {
                                            ...commonHeaders,
                                            "content-type": "application/json"
                                        },
                                        referrer: `https://dashboard.agentrouter.org/domains/${domainName}`,
                                        body: JSON.stringify({ 
                                            "renewal_type": "free", 
                                            "years": 1 
                                        }),
                                        method: "POST",
                                        credentials: "include"
                                    }
                                );
                                
                                if (renewRes.ok) {
                                    try {
                                        const renewData = await renewRes.json();
                                        logResults.push(`${domainName}: ✅ 续费成功 → ${JSON.stringify(renewData)}`);
                                    } catch {
                                        logResults.push(`${domainName}: ✅ 续费请求成功发送`);
                                    }
                                } else {
                                    const errText = await renewRes.text();
                                    logResults.push(`${domainName}: ❌ 续费失败 [${renewRes.status}] → ${errText.substring(0, 200)}`);
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
            self.log(f"续费流程异常: {e}", "WARN")
            return_msg += f"❌ 续费流程异常: {e}\n"
            
        self.shot(page, "完成")
        return return_msg
    
    def mask_url(self, url):
        url = re.sub(r'code=[^&]+', 'code=***', url)
        url = re.sub(r'state=[^&]+', 'state=***', url)
        return url
    
    def get_storage(self, context):
        """提取 storage_state"""
        try:
            state = context.storage_state()
            self.ag_local = state
            return state
        except Exception as e:
            self.log(f"获取 storage_state 失败: {e}", "WARN")
            return None

    def check_and_process_domain(self, domain):
        if domain.endswith('agentrouter.org/login'):
            return "signin"
        if "callback" in domain:
            return "redirect"
        if domain.endswith('agentrouter.org/console'):
            return "logged"
        return "invalid"
    
    def run(self):
        ok, new_local, msg = False, None, ""
        self.log(f"用户名: {mask_name(self.gh_username)}")
        self.log(f"Session: {'有' if self.gh_session else '无'}")
        self.log(f"登录入口: {BOARD_ENTRY_URL}")
        
        if not self.gh_username:
            self.log("缺少凭据", "ERROR")
            return False, None, "❌ 缺少凭据"
        
        with sync_playwright() as p:
            launch_args = {
                "headless": True,
                "args": [
                    '--no-sandbox',
                    '--disable-blink-features=AutomationControlled',
                    '--disable-infobars',
                    '--exclude-switches=enable-automation',
                ]
            }

            # 恢复代理配置逻辑
            if self.ag_proxy and isinstance(self.ag_proxy, dict):
                p_url = self.ag_proxy
                proxy_config = {
                    "server": f"http://{p_url['server']}:{p_url['port']}",
                    "username": p_url.get('username', ''),
                    "password": p_url.get('password', '')
                } 
                launch_args["proxy"] = proxy_config
                self.log(f"🌐 启用代理配置: {p_url['server']}:{p_url['port']}", "INFO")

            browser = p.chromium.launch(**launch_args)
            
            if self.ag_local:
                context = browser.new_context(
                    storage_state=self.ag_local,
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
                    except Exception:
                        self.log("加载 Cookie 失败", "WARN")
                        
                self.log("步骤1: 打开 agentrouter 登录页", "STEP")

                for i in range(10):
                    try:
                        page.goto(BOARD_ENTRY_URL, timeout=60000)
                        page.wait_for_load_state('domcontentloaded', timeout=60000)
                        time.sleep(random.uniform(20, 30))
                        shot = self.shot(page, "打开 agentrouter 登录页")
                        if shot:
                            self.notify.send(title="agentrouter 自动登录保活", content="打开 agentrouter 登录页", image_path=shot)
                        
                        self.log("正在等待 GitHub 登录按钮渲染...", "INFO")
                        try:
                            github_selectors = [
                                'button:has-text("Continue with GitHub")',
                                'button:has([aria-label="github_logo"])',
                                'button:has-text("GitHub")',
                                'a[href*="/auth/login/github"]'
                            ]
                        
                            found = False
                            for selector in github_selectors:
                                try:
                                    page.wait_for_selector(selector, timeout=10000, state="visible")
                                    self.log(f"成功检测到 GitHub 登录按钮: {selector}", "SUCCESS")
                                    found = True
                                    break
                                except Exception:
                                    continue
                        
                            if not found:
                                raise Exception("未找到 GitHub 登录按钮")
                        
                        except Exception as e:
                            self.log(f"等待 GitHub 按钮失败: {e}", "WARN")
                            self.log(f"当前URL: {page.url}", "WARN")
                            
                        resault = self.check_and_process_domain(page.url)
                        self.log(f"检测结果: {resault}", "INFO")
                        self.shot(page, "找不到 GitHub 按钮")
                        
                        if resault == "invalid":
                            self.log(f"[1.{i}]: 非域名: {page.url}", "WARN")
                            continue
                        if resault == "logged":
                            self.log(f"[1.{i}]: 已登录: {page.url}", "SUCCESS")
                            break
                        if resault == "signin":
                            self.log(f"[1.{i}]: 需登录: {page.url}", "INFO")
                            self.log("步骤2: 点击 GitHub", "STEP")
                            
                            if not self.click(page, desc="GitHub 登录按钮"):
                                shot = self.shot(page, "找不到 GitHub 按钮")
                                if shot:
                                    self.notify.send(title="agentrouter 自动登录保活", content="找不到 GitHub 按钮", image_path=shot)
                                self.log(f"[2.{i}]: 找不到 GitHub 按钮", "WARN")
                                continue
                            else:
                                for j in range(10):
                                    resault = self.check_and_process_domain(page.url)
                                    if resault == "signin":
                                        self.log(f"[2.{i}.{j}]: 未跳转: {page.url}", "INFO")
                                        if not self.click(page, desc="GitHub 登录按钮"):
                                            shot = self.shot(page, "找不到 GitHub 按钮")
                                            if shot:
                                                self.notify.send(title="agentrouter 自动登录保活", content="找不到 GitHub 按钮", image_path=shot)
                                            self.log(f"[2.{i}.{j}]: 找不到 GitHub 按钮", "WARN")
                                        time.sleep(random.uniform(20, 30))
                                        continue
                                    if resault == "logged":
                                        self.log(f"[2.{i}.{j}]: 已登录: {page.url}", "SUCCESS")
                                        break
                                    if resault == "redirect":
                                        self.log(f"[2.{i}.{j}]: 正在重定向: {self.mask_url(page.url)}", "INFO")
                                        self.log("正在检测 GitHub 账号是否绑定...", "INFO")
                                        
                                        not_bound_selector = 'p.text-slate-600:has-text("This GitHub account is not bound")'
                                        try:
                                            if page.locator(not_bound_selector).is_visible(timeout=5000):
                                                self.log("❌ 登录失败：该 GitHub 账号未绑定到 agentrouter！", "ERROR")
                                                shot = self.shot(page, "GitHub未绑定提示")
                                                if shot:
                                                    self.notify.send(
                                                        title="agentrouter 登录异常", 
                                                        content=f"❌ 账号 {self.gh_username} 未绑定：请先手动用密码登录并在设置中绑定 GitHub！", 
                                                        image_path=shot
                                                    )
                                                return False, None, "❌ GitHub 账号未绑定到平台，请手动绑定！"
                                        except Exception:
                                            self.log("未检测到未绑定错误提示，继续正常流程...", "SUCCESS")
                                            
                                        try:
                                            page.wait_for_url("https://*.agentrouter.org", timeout=60000)
                                            self.log(f"URL 已跳转: {page.url}", "SUCCESS")
                                            break
                                        except PlaywrightTimeoutError:
                                            self.log(f"等待 URL 跳转超时: {page.url}", "ERROR")
                                        continue
                                    if "github.com/login" in page.url:
                                        self.log(f"[2.{i}.{j}]: github登录过期，{page.url}", "ERROR")
                                        return False, None, "github登录过期！"   
                    except Exception as e:
                        if i < 10:
                            self.log(f"异常: {e}", "ERROR")
                            self.log(f"[1.{i}]: 未打开登录页，重试", "WARN")
                            time.sleep(random.uniform(10, 15))
                        else:
                            self.log(f"[1.{i}]: 访问 {page.url} 失败！", "ERROR")
                            browser.close()
                            return False, None, f"访问 {BOARD_ENTRY_URL} 失败！"   
                         
                self.log("步骤3: 更新 local_storage", "STEP")
                storage_state = self.get_storage(context)
                
                if storage_state:
                    self.log("开始为数据瘦身...")
                    slimmest_local = slim_storage_state(storage_state)
                    final_json = json.dumps(slimmest_local, ensure_ascii=False)
                    self.log(f"瘦身完成，最终数据大小: {len(final_json) / 1024:.2f} KB")
                    
                    storage_state_b64 = base64.b64encode(final_json.encode("utf-8")).decode("utf-8")
                    ok = True
                    new_local = storage_state_b64
                else:
                    self.log("未获取到 storage_state", "WARN")
                
                if self.shots:
                    self.notify.send(title="agentrouter 自动登录保活", content=f"✅ {self.gh_username}成功！", image_path=self.shots[-1])
            except Exception as e:
                self.log(f"异常: {e}", "ERROR")
                self.shot(page, "异常")
                import traceback
                traceback.print_exc()
                if self.shots:
                    self.notify.send(title="agentrouter 自动登录保活", content=f"❌ {self.gh_username}:{str(e)}", image_path=self.shots[-1])
                msg = f"访问 {page.url} 失败！"   
            finally:
                if browser:
                    browser.close()
                return ok, new_local, msg

def main():
    global config
    if config is None:
        config = ConfigReader()
    
    results = []
    accounts = config.get_value("GH_INFO")
    proxies = config.get_value("WZ_INFO")

    notify = get_notifier()
    gh_secret = SecretUpdater("GH_SESSION", config_reader=config)
    gh_sessions = gh_secret.load() or {}
    
    secret = SecretUpdater("AGENTROUTER_LOCALS", config_reader=config)
    ag_locals = secret.load() 

    if not accounts:
        print("❌ 错误: 未配置 LEAFLOW_ACCOUNTS")
        return
    if not proxies:
        print("📢 警告: 未配置 proxy ，将直连")

    print(f"📊 检测到 {len(accounts)} 个账号和 {len(proxies)} 个代理")

    for account, proxy in zip(accounts, proxies):
        username = account['username']
        if username != 'greenwave1987':
            continue
        print("\n" + "="*50)
        print(f"\n🚀 开始处理账号: {mask_name(username)}\n  🌐 使用代理: {proxy['server'][:-4]}***\n")
        print("="*50 + "\n")
        
        results.append(f"🚀 账号：{mask_name(username)}\n    🌐 使用代理: {proxy['server'][:-4]}***\n")
        ag_info = {
            'gh_username': username,
            'ag_proxy': proxy,
            'notify': notify,
            'wz_proxy': proxies[-1]
        }

        if isinstance(gh_sessions, dict):
            gh_session = gh_sessions.get(username, '')
            if isinstance(gh_session, list):
                gh_session = gh_session[0] if gh_session else ''
            ag_info['gh_session'] = gh_session
        else:
            print("⚠️ gh_sessions 格式错误！")
            ag_info['gh_session'] = ''

        if not gh_session:
            print("⚠️ 缺少对应账号的 gh_session ，退出！")
            continue
        
        if isinstance(ag_locals, dict):
            ag_local = ag_locals.get(username, '')
            if ag_local:
                try:
                    ag_info['ag_local'] = json.loads(base64.b64decode(ag_local).decode("utf-8"))
                    print("✅ 已加载 storage_state")
                except Exception as e:
                    print(f"❌ 加载 storage_state 失败: {e}")
        else:
            print(f"⚠️ ag_locals 格式错误！{ag_locals}")
            ag_locals = {}
            ag_info['ag_local'] = []

        try:
            auto_login = AutoLogin(ag_info)
            ok, new_local, msg = auto_login.run()
    
            if ok:
                print("    ✅ 执行成功")
                results.append(f"    ✅ {msg}\n")
                if new_local:
                    print("    ✅ 保存新 new_local")
                    ag_locals[username] = new_local
            else:
                print("    ⚠️ 执行失败，不保存 cookie")
                results.append(f"    ⚠️ 执行失败:{msg}\n")
    
        except Exception as e:
            print(f"    ❌ 执行异常: {e}")
            results.append(f"    ❌ 执行异常: {e}")

    print(f"ag_locals数据大小: {len(json.dumps(ag_locals)) / 1024:.2f} KB")
    secret.update(ag_locals)
    notify.send(
        title="agentrouter 自动登录保活汇总",
        content="\n".join(results)
    )

if __name__ == "__main__":
    main()
