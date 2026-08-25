import os
import re
import sys
import json
import time
import base64
import random
import requests
import datetime
from urllib.parse import quote,urlparse
from playwright.sync_api import sync_playwright
from playwright.sync_api import TimeoutError as PlaywrightTimeoutError
BASE_URL="https://agentrouter.org"
RENEW_DAYS=120
TIMEOUT=15
BOARD_ENTRY_URL="https://agentrouter.org/login"
DEVICE_VERIFY_WAIT=30
TWO_FACTOR_WAIT=int(os.environ.get("TWO_FACTOR_WAIT","120"))
BASE_DIR=os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0,BASE_DIR)
from engine.notify import TelegramNotifier
try:
    from engine.main import ConfigReader,SecretUpdater,test_proxy
except ImportError:
    class ConfigReader:
        def get_value(self,key):
            return os.environ.get(key)
    class SecretUpdater:
        def __init__(self,*args,**kwargs):
            pass
        def update(self,value):
            return False
        def load(self):
            return {}
    def test_proxy(proxy):
        return proxy
_notifier=None
config=None
def get_notifier():
    global _notifier,config
    if config is None:
        config=ConfigReader()
    if _notifier is None:
        _notifier=TelegramNotifier(config)
    return _notifier
def mask_name(name):
    if not name:
        return "***"
    return f"{name[:2]}***{name[-2:]}"
def slim_storage_state(state):
    if not isinstance(state,dict):
        return state
    state["cookies"]=[c for c in state.get("cookies",[]) if "agentrouter.org" in c.get("domain","") or "github.com" in c.get("domain","")]
    keep=[
        "session",
        "access_token",
        "refresh_token",
        "auth",
        "user",
        "lastLoginUpdateTime",
        "i18nextLng"
    ]
    for o in state.get("origins",[]):
        o["localStorage"]=[
            x for x in o.get("localStorage",[])
            if x.get("name") in keep
        ]
    return state
class AutoLogin:
    def __init__(self,info):
        self.gh_username=info.get("gh_username","")
        self.gh_session=info.get("gh_session","")
        self.ag_local=info.get("ag_local")
        self.ag_proxy=test_proxy(info.get("ag_proxy"))
        if not self.ag_proxy:
            self.ag_proxy=test_proxy(info.get("wz_proxy"))
        self.notify=info.get("notify")
        self.logs=[]
        self.shots=[]
        self.n=0
        self.secret=SecretUpdater("GH_SESSION",config_reader=config)
    def log(self,msg,level="INFO"):
        icon={"INFO":"ℹ️","SUCCESS":"✅","ERROR":"❌","WARN":"⚠️","STEP":"🔹"}.get(level,"•")
        print(f"{icon} {msg}",flush=True)
    def shot(self,page,name):
        self.n+=1
        path=f"{self.n:02d}_{name}.png"
        try:
            page.screenshot(path=path)
            self.shots.append(path)
        except:
            pass
        return path
    def click(self,page,desc=""):
        self.log(f"🔍 尝试查找并点击:{desc}")
        try:
            page.wait_for_load_state("domcontentloaded",timeout=15000)
        except:
            pass
        page.wait_for_timeout(3000)
        selectors=[
            'a[href*="/auth/login/github"]',
            'a[href*="github"]',
            'button:has-text("Continue with GitHub")',
            '[data-provider="github"]',
            'button:has([aria-label="github_logo"])'
        ]
        for sel in selectors:
            try:
                el=page.locator(sel).first
                el.wait_for(state="visible",timeout=3000)
                self.log(f"找到按钮:{sel}","SUCCESS")
                try:
                    with page.expect_navigation(timeout=10000):
                        el.click(timeout=8000)
                    self.log(f"导航成功:{page.url}","SUCCESS")
                    return True
                except:
                    pass
                try:
                    with page.expect_popup(timeout=5000) as pop:
                        el.click(timeout=5000)
                    self.log(f"发现popup:{pop.value.url}","SUCCESS")
                    return True
                except:
                    pass
                try:
                    el.evaluate("(e)=>e.click()")
                    page.wait_for_timeout(5000)
                    if page.url!=BOARD_ENTRY_URL:
                        self.log(f"JS点击跳转:{page.url}","SUCCESS")
                        return True
                except:
                    pass
            except:
                continue
        self.log("找不到GitHub按钮","ERROR")
        return False
    def check_url(self,url):
        if "github.com/login/oauth" in url:
            return "github"
        if "github.com" in url:
            return "github"
        if "agentrouter.org/login" in url:
            return "signin"
        if "agentrouter.org" in url:
            return "logged"
        return "invalid"

    def get_storage(self,context):
        try:
            state=context.storage_state()
            return base64.b64encode(
                json.dumps(
                    slim_storage_state(state),
                    ensure_ascii=False
                ).encode()
            ).decode()
        except Exception as e:
            self.log(f"获取storage失败:{e}","WARN")
            return None
    def add_github_session(self,context):
        if not self.gh_session:
            return
        try:
            context.add_cookies([
                {
                    "name":"user_session",
                    "value":self.gh_session,
                    "domain":"github.com",
                    "path":"/"
                },
                {
                    "name":"logged_in",
                    "value":"yes",
                    "domain":"github.com",
                    "path":"/"
                }
            ])
            self.log("已加载GitHub Session","SUCCESS")
        except Exception as e:
            self.log(f"加载Session失败:{e}","WARN")
    def wait_device(self,page):
        self.log("等待设备验证","WARN")
        for i in range(DEVICE_VERIFY_WAIT):
            time.sleep(1)
            if "device-verification" not in page.url and "verified-device" not in page.url:
                self.log("设备验证通过","SUCCESS")
                return True
        return False
    def wait_mobile_2fa(self,page):
        self.log("等待GitHub Mobile确认","WARN")
        shot=self.shot(page,"mobile_2fa")
        if self.notify:
            self.notify.send(
                title="agentrouter登录",
                content="请打开GitHub APP批准登录",
                image_path=shot
            )
        for i in range(TWO_FACTOR_WAIT):
            time.sleep(1)
            if "two-factor" not in page.url:
                self.log("Mobile验证通过","SUCCESS")
                return True
            if i%30==0 and i:
                try:
                    page.reload(timeout=30000)
                except:
                    pass
        return False
    def oauth_wait(self,page):
        for i in range(60):
            url=page.url
            status=self.check_url(url)
            self.log(f"OAuth检测:{status}:{url}")
            if status=="logged":
                return True
            if "github.com/login/oauth" in url:
                try:
                    page.wait_for_load_state(
                        "networkidle",
                        timeout=10000
                    )
                except:
                    pass
            if "two-factor/mobile" in url:
                if not self.wait_mobile_2fa(page):
                    return False
            if "device-verification" in url:
                if not self.wait_device(page):
                    return False
            time.sleep(1)
        return False
    def run(self):
        ok=False
        new_local=None
        msg=""
        self.log(f"用户名:{mask_name(self.gh_username)}")
        self.log(f"Session:{'有' if self.gh_session else '无'}")
        if not self.gh_username or not self.gh_session:
            return False,None,"缺少GitHub信息"
        with sync_playwright() as p:
            args={
                "headless":True,
                "args":[
                    "--no-sandbox",
                    "--disable-blink-features=AutomationControlled"
                ]
            }
            if self.ag_proxy:
                args["proxy"]={
                    "server":f"http://{self.ag_proxy['server']}:{self.ag_proxy['port']}",
                    "username":self.ag_proxy.get("username"),
                    "password":self.ag_proxy.get("password")
                }
            browser=p.chromium.launch(**args)
            context=browser.new_context(
                storage_state=self.ag_local if self.ag_local else None,
                viewport={
                    "width":1920,
                    "height":1080
                },
                user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome/128"
            )
            self.add_github_session(context)
            page=context.new_page()
            page.add_init_script("""
            Object.defineProperty(navigator,'webdriver',{get:()=>undefined});
            window.chrome={runtime:{}};
            Object.defineProperty(navigator,'languages',{get:()=>['zh-CN','zh']});
            """)
            try:
                self.log("打开登录页面","STEP")
                page.goto(
                    BOARD_ENTRY_URL,
                    timeout=60000
                )
                page.wait_for_load_state(
                    "domcontentloaded",
                    timeout=60000
                )
                time.sleep(5)
                status=self.check_url(page.url)
                if status=="signin":
                    if not self.click(page,"GitHub登录"):
                        return False,None,"点击GitHub失败"
                    if not self.oauth_wait(page):
                        return False,None,"OAuth失败"
                elif status!="logged":
                    return False,None,f"未知状态:{page.url}"
                self.log("登录成功","SUCCESS")
                new_local=self.get_storage(context)
                if new_local:
                    ok=True
                    msg="登录成功，storage已更新"
                else:
                    msg="登录成功但storage为空"
            except Exception as e:
                msg=f"异常:{e}"
                self.log(msg,"ERROR")
            finally:
                browser.close()
        return ok,new_local,msg

    def parse_cookies(self,cookie_string):
        result={}
        for item in cookie_string.split(";"):
            if "=" in item:
                k,v=item.strip().split("=",1)
                result[k]=v
        return result
    def get_headers(self,cookies,json_type=False):
        headers={
            "accept":"application/json, text/plain, */*",
            "user-agent":"Mozilla/5.0",
            "cache-control":"no-cache"
        }
        if json_type:
            headers["content-type"]="application/json"
        csrf=cookies.get("panel_csrf_token")
        if csrf:
            headers["x-csrf-token"]=csrf
        return headers
    def get_domains(self,cookies):
        try:
            r=requests.get(
                "https://dashboard.agentrouter.org/_panel_api/api/domains",
                headers=self.get_headers(cookies),
                cookies=cookies,
                timeout=TIMEOUT
            )
            if not r.ok:
                return []
            data=r.json()
            return data.get("domains",[])
        except Exception as e:
            self.log(f"获取域名失败:{e}","WARN")
            return []
    def renew_domain(self,cookies,domain):
        try:
            url=f"https://dashboard.agentrouter.org/_panel_api/api/domains/{quote(domain,safe='')}/renew"
            r=requests.post(
                url,
                headers=self.get_headers(cookies,True),
                cookies=cookies,
                json={
                    "renewal_type":"free",
                    "years":1
                },
                timeout=TIMEOUT
            )
            self.log(f"{domain}续费:{r.status_code}")
            return r.ok
        except Exception as e:
            self.log(f"{domain}续费异常:{e}","WARN")
            return False
    def check_renew(self,cookies):
        result=[]
        domains=self.get_domains(cookies)
        if not domains:
            return "没有域名"
        today=datetime.datetime.now()
        for item in domains:
            domain=item.get("domain")
            expiry=item.get("expiry_date")
            if not domain or not expiry:
                continue
            try:
                date=datetime.datetime.strptime(
                    expiry,
                    "%Y%m%d"
                )
                days=(date-today).days
            except:
                continue
            if days<RENEW_DAYS:
                if self.renew_domain(cookies,domain):
                    result.append(
                        f"✅ {domain}续费成功"
                    )
                else:
                    result.append(
                        f"❌ {domain}续费失败"
                    )
            else:
                result.append(
                    f"✅ {domain}剩余{days}天"
                )
        return "\n".join(result)
def main():
    global config
    if config is None:
        config=ConfigReader()
    notify=get_notifier()
    gh_secret=SecretUpdater(
        "GH_SESSION",
        config_reader=config
    )
    local_secret=SecretUpdater(
        "AGENTROUTER_LOCALS",
        config_reader=config
    )
    sessions=gh_secret.load() or {}
    locals_data=local_secret.load() or {}
    accounts=config.get_value("GH_INFO") or []
    proxies=config.get_value("WZ_INFO") or []
    results=[]
    for account,proxy in zip(accounts,proxies):
        username=account.get("username")
        print("="*50)
        print(f"处理账号:{mask_name(username)}")
        info={
            "gh_username":username,
            "gh_session":sessions.get(username,""),
            "ag_local":None,
            "ag_proxy":proxy,
            "wz_proxy":proxies[-1],
            "notify":notify
        }
        old_local=locals_data.get(username)
        if old_local:
            try:
                info["ag_local"]=json.loads(
                    base64.b64decode(old_local).decode()
                )
            except Exception:
                pass
        if not info["gh_session"]:
            results.append(
                f"⚠️ {username}缺少Session"
            )
            continue
        try:
            bot=AutoLogin(info)
            ok,new_local,msg=bot.run()
            if ok and new_local:
                locals_data[username]=new_local
            results.append(
                f"{username}:{msg}"
            )
        except Exception as e:
            results.append(
                f"{username}:异常 {e}"
            )
    local_secret.update(locals_data)
    notify.send(
        title="agentrouter自动登录保活汇总",
        content="\n".join(results)
    )
if __name__=="__main__":
    main()
