# -*- coding: utf-8 -*-
"""
Stackryze 域名保活（GitHub Actions 可用）

流程:
  1. 打开 https://domain.stackryze.com/login（Cloudflare 盾，脚本等待自动通过，失败自动重试）
  2. 点击 <span>Continue with Stackryze SSO</span>
  3. 等待跳转 auth.stackryze.com/en/login?authRequest=V2_xxx（authRequest 不固定，用正则匹配）
  4. 点击 GitHub 登录按钮，等待跳到 github.com
  5. GitHub 侧（参考 renew.py）：
     - 注入 GH_SESSION(user_session cookie) 保持登录
     - 出现 "Continue as <用户名>" 确认按钮(input.btn.btn-sm.btn-primary.m-0) → 点击
     - OAuth 授权页(Authorize 按钮) → 点击
     - 设备验证 / 两步验证 → 等待或报错
  6. 等待回跳 domain.stackryze.com，确认已登录，导出精简后的 storage_state

Secrets / 环境变量:
  GH_USERNAME   GitHub 用户名（必填，用于日志与确认按钮匹配）
  GH_SESSION    GitHub user_session cookie 值（必填）
  PROXY_DSN     代理（推荐必填，国内网络访问 GitHub 不稳定）
                格式: http://user:pass@host:port 或 socks5://user:pass@host:port
  TG_BOT_TOKEN / TG_CHAT_ID  可选，结果推送到 Telegram
  CF_WAIT       Cloudflare 盾最长等待秒数（默认 120）
  REDIRECT_WAIT GitHub OAuth/回跳最长等待秒数（默认 90）
"""

import os
import re
import sys
import json
import time
import base64
import random
import datetime
from urllib.parse import urlparse

import requests
from playwright.sync_api import sync_playwright, TimeoutError as PlaywrightTimeoutError

# ==================== 配置 ====================
ENTRY_URL = "https://domain.stackryze.com/login"
AUTH_URL_RE = re.compile(r"^https://auth\.stackryze\.com/en/login\?authRequest=V2_\d+")
SSO_BTN_TEXT = "Continue with Stackryze SSO"

SUCCESS_URL_KEYWORD = "domain.stackryze.com"

GH_USERNAME = os.environ.get("GH_USERNAME", "").strip()
GH_SESSION = os.environ.get("GH_SESSION", "").strip()
PROXY_DSN = os.environ.get("PROXY_DSN", "").strip()
TG_BOT_TOKEN = os.environ.get("TG_BOT_TOKEN", "").strip()
TG_CHAT_ID = os.environ.get("TG_CHAT_ID", "").strip()

CF_WAIT = int(os.environ.get("CF_WAIT", "120"))
REDIRECT_WAIT = int(os.environ.get("REDIRECT_WAIT", "90"))
ENTRY_RETRY = int(os.environ.get("ENTRY_RETRY", "3"))

STATE_FILE = os.environ.get("STATE_FILE", "stackryze_state.b64")
SHOT_DIR = os.environ.get("SHOT_DIR", "shots")

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36")

logs = []
shots = []
_n = 0


def log(msg, level="INFO"):
    icons = {"INFO": "ℹ️", "SUCCESS": "✅", "ERROR": "❌", "WARN": "⚠️", "STEP": "🔹"}
    line = f"{icons.get(level, '•')} {msg}"
    print(line, flush=True)
    logs.append(line)


def shot(page, name):
    global _n
    _n += 1
    f = os.path.join(SHOT_DIR, f"{_n:02d}_{name}.png")
    try:
        os.makedirs(SHOT_DIR, exist_ok=True)
        page.screenshot(path=f)
        shots.append(f)
    except Exception:
        pass
    return f


def tg_send(content, image_path=None):
    if not (TG_BOT_TOKEN and TG_CHAT_ID):
        return
    try:
        if image_path:
            with open(image_path, "rb") as fp:
                requests.post(
                    f"https://api.telegram.org/bot{TG_BOT_TOKEN}/sendPhoto",
                    data={"chat_id": TG_CHAT_ID, "caption": content[:1000]},
                    files={"photo": fp}, timeout=30,
                )
        else:
            requests.post(
                f"https://api.telegram.org/bot{TG_BOT_TOKEN}/sendMessage",
                data={"chat_id": TG_CHAT_ID, "text": content[:4000]}, timeout=30,
            )
    except Exception as e:
        log(f"Telegram 通知失败: {e}", "WARN")


def mask_name(name):
    if not name:
        return "***"
    return f"{name[:2]}***{name[-2:]}" if len(name) > 4 else name[0] + "***"


def parse_proxy(dsn):
    """解析代理 DSN 为 Playwright proxy 配置"""
    u = urlparse(dsn if "://" in dsn else "http://" + dsn)
    server = f"{u.scheme or 'http'}://{u.hostname}:{u.port or 80}"
    cfg = {"server": server}
    if u.username:
        cfg["username"] = u.username
    if u.password:
        cfg["password"] = u.password
    return cfg


def slim_storage_state(state):
    """精简 storage_state，只保留 stackryze/github 相关 cookie，防超 64KB"""
    if not isinstance(state, dict):
        return state
    keep_kw = ("stackryze", "github.com")
    if "cookies" in state:
        state["cookies"] = [
            c for c in state["cookies"]
            if any(k in c.get("domain", "") for k in keep_kw)
        ]
    if "origins" in state:
        state["origins"] = [
            o for o in state["origins"]
            if any(k in o.get("origin", "") for k in keep_kw)
        ]
    return state


def wait_sso_button(page, timeout_s):
    """等 Cloudflare 盾通过 + SSO 按钮渲染（盾页特征: 'Performing security verification'）"""
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        if page.locator(f"text={SSO_BTN_TEXT}").count() > 0:
            return True
        time.sleep(1)
    return False


def open_entry(page):
    """打开入口 → 过盾 → 点 SSO → 等跳到 auth 页（带重试）"""
    for attempt in range(1, ENTRY_RETRY + 1):
        log(f"打开登录页（第 {attempt}/{ENTRY_RETRY} 次）...", "STEP")
        try:
            page.goto(ENTRY_URL, timeout=60000, wait_until="domcontentloaded")
        except Exception as e:
            log(f"goto 失败: {e}", "WARN")
            time.sleep(5)
            continue
        if not wait_sso_button(page, CF_WAIT):
            shot(page, f"盾未通过_{attempt}")
            log("Cloudflare 盾未通过或按钮未渲染", "WARN")
            time.sleep(random.uniform(3, 6))
            continue
        shot(page, "登录页就绪")
        time.sleep(random.uniform(0.5, 1.2))
        try:
            el = page.locator(f'span:has-text("{SSO_BTN_TEXT}")').first
            el.click(force=True)
            log("已点击 Continue with Stackryze SSO", "SUCCESS")
        except Exception as e:
            log(f"点击 SSO 失败: {e}", "WARN")
            continue
        try:
            page.wait_for_url(AUTH_URL_RE, timeout=30000)
            masked = re.sub(r"authRequest=V2_\d+", "authRequest=V2_***", page.url)
            log(f"已跳转: {masked}", "SUCCESS")
            time.sleep(2)
            shot(page, "auth登录页")
            return True
        except PlaywrightTimeoutError:
            log(f"点击 SSO 后未跳转，当前: {page.url[:80]}", "WARN")
    return False


def click_github_on_auth(page):
    """点击 GitHub 按钮，并确认真的跳到了 github.com（失败重试）"""
    sels = [
        'button[type="button"]:has-text("GitHub")',
        'button:has-text("GitHub")',
        '//button[normalize-space()="GitHub"]',
    ]
    for attempt in range(1, 4):
        for sel in sels:
            try:
                el = page.locator(sel).first
                el.wait_for(state="visible", timeout=3000)
                time.sleep(random.uniform(0.4, 1.0))
                el.click(force=True)
                log("已点击 GitHub 登录按钮", "SUCCESS")
                break
            except Exception:
                continue
        # 等待真正到达 github.com（国内网络不加代理时这里会一直卡住）
        try:
            page.wait_for_url(re.compile(r"github\.com"), timeout=30)
            log(f"已到达 GitHub: {page.url[:70]}", "SUCCESS")
            return True
        except PlaywrightTimeoutError:
            shot(page, f"github未跳转_{attempt}")
            log(f"点击后 30s 未跳到 GitHub（第 {attempt} 次），重试", "WARN")
    return False


def handle_github(context):
    """GitHub 侧流程（监控 context 内所有页面，兼容 popup）。
    处理: Continue as <user> 确认按钮 / Authorize 按钮 / 登录过期 / 设备验证 / 2FA"""
    deadline = time.time() + REDIRECT_WAIT
    seen_github = False
    notified_2fa = False
    while time.time() < deadline:
        pages = context.pages
        gh_pages = [pg for pg in pages if "github.com" in pg.url]

        if gh_pages:
            seen_github = True
            pg = gh_pages[0]
            url = pg.url

            # 登录页 → session 失效
            if re.match(r"https://github\.com/(login|session)", url):
                shot(pg, "github登录页_session失效")
                log("GitHub session 已失效（落在 github.com 登录页），请更新 GH_SESSION", "ERROR")
                return False

            # 设备验证
            if "verified-device" in url or "device-verification" in url:
                log("GitHub 要求设备验证（去邮箱/GitHub App 批准），CI 环境无法完成", "ERROR")
                shot(pg, "设备验证")
                return False

            # 两步验证
            if "github.com/sessions/two-factor" in url:
                if not notified_2fa:
                    shot(pg, "两步验证")
                    log("GitHub 要求两步验证：请在手机 GitHub App 批准（CI 等待中...）", "WARN")
                    tg_send("⚠️ Stackryze 保活：GitHub 两步验证等待中，请在手机上批准")
                    notified_2fa = True
                time.sleep(2)
                continue

            # "Continue as <用户名>" 确认按钮（已登录状态下的授权确认）
            try:
                cont = pg.locator(
                    'input[aria-label^="Continue as"], '
                    'input.btn.btn-sm.btn-primary.m-0[value="Continue"]'
                ).first
                if cont.is_visible(timeout=2000):
                    log(f"检测到确认按钮: {cont.get_attribute('aria-label')}", "STEP")
                    time.sleep(random.uniform(0.5, 1.0))
                    cont.click()
                    log("已点击 Continue 确认按钮", "SUCCESS")
                    time.sleep(3)
                    continue
            except Exception:
                pass

            # OAuth 授权页 → 点 Authorize
            if "github.com/login/oauth/authorize" in url:
                log("进入 GitHub OAuth 授权页", "STEP")
                shot(pg, "oauth")
                try:
                    btn = pg.locator('button[name="authorize"], button:has-text("Authorize")').first
                    btn.wait_for(state="visible", timeout=8000)
                    time.sleep(random.uniform(0.5, 1.0))
                    btn.click()
                    log("已点击 Authorize", "SUCCESS")
                except Exception:
                    log("授权页未出现 Authorize 按钮（可能自动跳过）", "INFO")
                time.sleep(3)
                continue
        elif seen_github:
            log("GitHub OAuth 完成（已离开/关闭 GitHub 页面）", "SUCCESS")
            return True

        time.sleep(1)
    log("GitHub 流程等待超时", "ERROR")
    for pg in context.pages:
        shot(pg, "github流程超时")
    return False


def wait_back_to_app(context):
    """等待回跳到 domain.stackryze.com 且不在登录页"""
    deadline = time.time() + REDIRECT_WAIT
    while time.time() < deadline:
        for pg in context.pages:
            url = pg.url
            if SUCCESS_URL_KEYWORD in url and "/login" not in url:
                log(f"回跳成功: {url}", "SUCCESS")
                return pg
        time.sleep(1)
    for pg in context.pages:
        shot(pg, "回跳超时")
    log(f"回跳超时，最后 URL: {context.pages[-1].url[:90] if context.pages else '?'}", "ERROR")
    return None


def run():
    if not GH_USERNAME or not GH_SESSION:
        log("缺少 GH_USERNAME / GH_SESSION", "ERROR")
        return False

    log(f"GitHub 用户: {mask_name(GH_USERNAME)}", "INFO")
    log(f"Session: {'有' if GH_SESSION else '无'}", "INFO")
    if PROXY_DSN:
        log(f"代理: {urlparse(PROXY_DSN).hostname}", "INFO")
    else:
        log("未配置代理（国内网络访问 GitHub 可能不稳定）", "WARN")

    with sync_playwright() as p:
        launch_args = {
            "headless": True,
            "args": ["--no-sandbox", "--disable-blink-features=AutomationControlled"],
        }
        if PROXY_DSN:
            launch_args["proxy"] = parse_proxy(PROXY_DSN)

        # Cloudflare 盾会识别 Playwright 自带 Chromium，必须用真实 Chrome（channel=chrome）
        browser = None
        try:
            browser = p.chromium.launch(channel="chrome", **launch_args)
            log("使用真实 Chrome（channel=chrome）", "SUCCESS")
        except Exception as e:
            log(f"真实 Chrome 不可用({e})，回退自带 Chromium", "WARN")
            browser = p.chromium.launch(**launch_args)

        context = browser.new_context(
            viewport={"width": 1920, "height": 1080},
            user_agent=UA,
        )
        page = context.new_page()
        # 注意：不要加 navigator.webdriver / window.chrome 之类的 init script 反检测注入
        # 实测真实 Chrome + UA 覆盖可通过 Cloudflare；init script 反而制造指纹不一致导致被拒

        ok = False
        try:
            # 注入 GitHub session cookie（参考 renew.py）
            try:
                context.add_cookies([
                    {"name": "user_session", "value": GH_SESSION,
                     "domain": "github.com", "path": "/", "httpOnly": True, "secure": True},
                    {"name": "logged_in", "value": "yes",
                     "domain": "github.com", "path": "/"},
                ])
                log("已注入 GH_SESSION cookie", "SUCCESS")
            except Exception as e:
                log(f"注入 cookie 失败: {e}", "WARN")

            # 1. 打开入口（含 Cloudflare 等待 + 重试）
            log("步骤1: 打开 domain.stackryze.com/login", "STEP")
            if not open_entry(page):
                raise RuntimeError("入口打开失败（Cloudflare 盾或 SSO 按钮未就绪）")

            # 2. 点击 GitHub
            log("步骤2: 点击 GitHub 登录", "STEP")
            if not click_github_on_auth(page):
                raise RuntimeError("点击 GitHub 按钮后未跳转到 github.com")

            # 3. GitHub 侧（Continue as 确认/Authorize/2FA，监控含 popup 的所有页面）
            log("步骤3: 处理 GitHub 登录/授权", "STEP")
            if not handle_github(context):
                raise RuntimeError("GitHub 登录/授权失败")

            # 4. 等回跳 + 确认已登录
            log("步骤4: 等待回跳 Stackryze", "STEP")
            app_page = wait_back_to_app(context)
            if app_page is None:
                raise RuntimeError("回跳 Stackryze 失败")
            try:
                app_page.wait_for_load_state("domcontentloaded", timeout=30000)
            except Exception:
                pass
            time.sleep(random.uniform(3, 5))
            shot(app_page, "已登录")

            # 5. 导出精简 storage_state（base64）
            log("步骤5: 导出 storage_state", "STEP")
            state = slim_storage_state(context.storage_state())
            final_json = json.dumps(state, ensure_ascii=False)
            b64 = base64.b64encode(final_json.encode("utf-8")).decode("utf-8")
            with open(STATE_FILE, "w", encoding="utf-8") as f:
                f.write(b64)
            log(f"storage_state 已保存: {STATE_FILE}（{len(b64)/1024:.1f} KB base64）", "SUCCESS")

            ok = True
            log("✅ Stackryze 保活成功", "SUCCESS")

        except Exception as e:
            log(f"异常: {e}", "ERROR")
            import traceback
            traceback.print_exc()
            try:
                for pg in context.pages:
                    shot(pg, "异常")
            except Exception:
                pass
        finally:
            browser.close()

    return ok


def main():
    start = datetime.datetime.now()
    ok = run()
    dur = (datetime.datetime.now() - start).total_seconds()

    summary = "\n".join(logs)
    print("\n" + "=" * 50)
    print(f"结果: {'✅ 成功' if ok else '❌ 失败'}  用时 {dur:.0f}s")
    print("=" * 50)

    status = "✅" if ok else "❌"
    tg_send(f"{status} Stackryze 保活{'成功' if ok else '失败'}（用时 {dur:.0f}s）\n\n{summary}")
    if shots:
        for s in shots[-2:]:
            tg_send("执行截图", image_path=s)

    gh_out = os.environ.get("GITHUB_OUTPUT")
    if gh_out:
        with open(gh_out, "a", encoding="utf-8") as f:
            f.write(f"result={'success' if ok else 'failure'}\n")

    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()

