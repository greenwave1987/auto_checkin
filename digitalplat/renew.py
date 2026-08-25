#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import os
import json
import time
import random
import base64
import traceback
from playwright.sync_api import sync_playwright, TimeoutError as PlaywrightTimeoutError


class AutoLoginAgentRouter:
    def __init__(self):
        self.login_url = "https://agentrouter.org/login"
        self.username = os.getenv("GH_USERNAME", "")
        self.password = os.getenv("GH_PASSWORD", "")
        self.gh_session = os.getenv("GH_SESSION", "")
        self.storage_file = os.getenv("STORAGE_FILE", "agentrouter_storage_state.txt")
        self.proxy = os.getenv("PROXY", "")
        self.headless = os.getenv("HEADLESS", "true").lower() == "true"

    def log(self, msg):
        print(f"[{time.strftime('%H:%M:%S')}] {msg}", flush=True)

    def load_storage(self):
        if not os.path.exists(self.storage_file):
            return None
        try:
            data = open(self.storage_file).read().strip()
            raw = base64.b64decode(data).decode()
            return json.loads(raw)
        except:
            return None

    def save_storage(self, state):
        try:
            data = json.dumps(state, ensure_ascii=False)
            b64 = base64.b64encode(data.encode()).decode()
            with open(self.storage_file, "w") as f:
                f.write(b64)
            self.log(
                f"保存storage_state成功 {len(b64)/1024:.2f}KB"
            )
            return b64
        except Exception as e:
            self.log(f"保存失败 {e}")

    def add_github_cookie(self, context):
        if not self.gh_session:
            return
        try:
            context.add_cookies([
                {
                    "name": "user_session",
                    "value": self.gh_session,
                    "domain": "github.com",
                    "path": "/"
                },
                {
                    "name": "logged_in",
                    "value": "yes",
                    "domain": "github.com",
                    "path": "/"
                }
            ])
            self.log("GitHub Session加载成功")
        except Exception as e:
            self.log(f"Session加载失败 {e}")

    def proxy_config(self):
        if not self.proxy:
            return None

        return {
            "server": self.proxy
        }

    def click_github(self, page):
        selectors = [
            'a[href*="github"]',
            'button:has-text("GitHub")',
            'button:has-text("Sign in with GitHub")',
            'a:has-text("GitHub")'
        ]

        for s in selectors:
            try:
                el = page.locator(s).first
                if el.is_visible(timeout=2000):
                    el.click(force=True)
                    self.log("点击GitHub登录")
                    return True
            except:
                pass

        return False

    def oauth(self, page):

        try:
            page.wait_for_url(
                "**github.com**",
                timeout=30000
            )

            self.log(
                "GitHub页面:" + page.url
            )

            buttons = [
                'button:has-text("Authorize")',
                'button:has-text("授权")',
                'input[type="submit"]'
            ]

            for b in buttons:
                try:
                    el = page.locator(b).first
                    if el.is_visible(timeout=3000):
                        el.click()
                        self.log("授权完成")
                        return True
                except:
                    pass

            return True

        except Exception as e:
            self.log(e)
            return False

    def wait_success(self,page):

        for i in range(60):

            url = page.url

            if (
                "agentrouter.org" in url
                and "login" not in url
            ):
                return True

            time.sleep(1)

        return False


    def run(self):

        with sync_playwright() as p:

            args={
                "headless":self.headless,
                "args":[
                    "--no-sandbox",
                    "--disable-blink-features=AutomationControlled"
                ]
            }

            proxy=self.proxy_config()

            if proxy:
                args["proxy"]=proxy

            browser=p.chromium.launch(**args)

            try:

                context_args={
                    "viewport":{
                        "width":1920,
                        "height":1080
                    },
                    "user_agent":
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome/128 Safari/537.36"
                }

                old=self.load_storage()

                if old:
                    context_args["storage_state"]=old
                    self.log("加载历史状态")


                context=browser.new_context(
                    **context_args
                )


                page=context.new_page()


                page.add_init_script("""
                Object.defineProperty(
                    navigator,
                    'webdriver',
                    {
                    get:()=>undefined
                    }
                )
                """)


                self.add_github_cookie(context)


                self.log("打开登录页面")

                page.goto(
                    self.login_url,
                    timeout=60000
                )


                time.sleep(3)


                if "login" not in page.url:

                    self.log("已经登录")

                    return self.save_storage(
                        context.storage_state()
                    )


                if not self.click_github(page):

                    raise Exception(
                        "找不到GitHub登录按钮"
                    )


                time.sleep(5)


                self.oauth(page)


                if not self.wait_success(page):

                    raise Exception(
                        "登录超时"
                    )


                self.log("登录成功")


                return self.save_storage(
                    context.storage_state()
                )


            except Exception:

                traceback.print_exc()

            finally:

                browser.close()



if __name__=="__main__":

    AutoLoginAgentRouter().run()
