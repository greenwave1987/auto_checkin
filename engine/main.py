# -*- coding: utf-8 -*-
import re
import os
import base64
import requests
from nacl import public, encoding
import json
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from hashlib import sha256
from pathlib import Path

REPO = os.getenv("GITHUB_REPOSITORY")
REPO_TOKEN = os.getenv("REPO_TOKEN")

# ==================================================
# 解密函数并读取信息
# ==================================================
def getconfig(key: str) -> dict:
    password = os.getenv("CONFIG_PASSWORD","").strip()
    if not password:
        raise RuntimeError("❌ 未设置 CONFIG_PASSWORD")
    config = getconfig(password)
    # 读取账号信息
    INFO = config.get("key","")
    if not LF_INFO:
        raise RuntimeError(f"❌ 配置文件中不存在 {key}")
    print(f'ℹ️ 已读取: {INFO.get("description","")}')
    value = INFO.get("value","").strip()
    return value

def derive_key(password: str) -> bytes:
    """
    从密码字符串派生 32 字节 AES key
    """
    return sha256(password.encode()).digest()


def decrypt_json(encrypted_str: str, password: str) -> dict:
    """
    解密 AES-GCM base64 编码的 JSON 字符串

    参数:
        encrypted_str: 加密后的 base64 字符串
        password: 加密时使用的密码

    返回:
        解密后的 JSON 数据（dict）

    异常:
        ValueError: 解密失败或内容非 JSON
    """
    try:
        key = derive_key(password)
        raw = base64.b64decode(encrypted_str)

        if len(raw) < 13:  # nonce 12 字节 + 至少 1 字节密文
            raise ValueError("加密数据格式错误")

        nonce = raw[:12]
        ciphertext = raw[12:]

        aesgcm = AESGCM(key)
        plaintext = aesgcm.decrypt(nonce, ciphertext, None)

        return json.loads(plaintext.decode("utf-8"))

    except Exception as e:
        raise ValueError(f"解密失败: {e}")
        
def getconfig(password: str) -> dict:
    """
    从脚本上一级目录读取 config.enc 并解密
    
    # 当前脚本所在目录
    current_dir = Path(__file__).resolve().parent
    # 上一级目录
    parent_dir = current_dir.parent
    # config.enc 路径
    config_path = parent_dir / "config.enc"
    在同一目录，注释掉
    """
    config_path = "config.enc"

    if not config_path.exists():
        raise FileNotFoundError(f"❌ 找不到 config.enc: {config_path}")

    encrypted_content = config_path.read_text(encoding="utf-8").strip()

    try:
        data = decrypt_json(encrypted_content, password)
        print("✅ 解密成功")
        return data
    except ValueError as e:
        print("❌ 解密失败:", e)
        raise
# ==================================================
# GitHub Secret 回写
# ==================================================

class SecretUpdater:
    def __init__(self, name):
        self.name = name
        print(f"🔐 初始化，secret 名称 = {name}")

    def update(self, value):
        print("📝 准备回写 GitHub Secret")

        if not REPO or not REPO_TOKEN:
            print("⚠ 未配置 GITHUB_REPOSITORY / REPO_TOKEN，跳过")
            return

        headers = {
            "Authorization": f"token {REPO_TOKEN}",
            "Accept": "application/vnd.github.v3+json"
        }

        print(f"🌐 获取公钥: {REPO}")
        r = requests.get(
            f"https://api.github.com/repos/{REPO}/actions/secrets/public-key",
            headers=headers,
            timeout=30
        )

        print(f"⬅️ 公钥接口返回 {r.status_code}")
        r.raise_for_status()

        key = r.json()

        print("🔑 开始加密 Secret")
        pk = public.PublicKey(key["key"].encode(), encoding.Base64Encoder())
        encrypted = public.SealedBox(pk).encrypt(value.encode())

        print(f"📤 提交 Secret: {self.name}")
        r = requests.put(
            f"https://api.github.com/repos/{REPO}/actions/secrets/{self.name}",
            headers=headers,
            json={
                "encrypted_value": base64.b64encode(encrypted).decode(),
                "key_id": key["key_id"]
            },
            timeout=30
        )

        print(f"✅ 回写完成，HTTP {r.status_code}")


# ==================================================
# Session 工厂
# ==================================================

def session_from_cookies(cookies, headers=None):
    print("🧩 [Session] 开始从 cookies 构建 session")

    session = requests.Session()

    # ---------- Playwright cookies（list） ----------
    if isinstance(cookies, list):
        print(f"📦 [Session] 检测到 Playwright cookies，数量: {len(cookies)}")
        for c in cookies:
            name = c.get("name")
            value = c.get("value")
            domain = c.get("domain")
            path = c.get("path", "/")

            if not name or value is None:
                print(f"⚠ 跳过非法 cookie: {c}")
                continue

            session.cookies.set(
                name,
                value,
                domain=domain,
                path=path
            )
            print(f"🍪 [Session] 注入 cookie: {name}")

    # ---------- dict cookies ----------
    elif isinstance(cookies, dict):
        print(f"📦 [Session] 检测到 dict cookies，数量: {len(cookies)}")
        for k, v in cookies.items():
            session.cookies.set(k, v)
            print(f"🍪 [Session] 注入 cookie: {k}")

    else:
        print(f"❌ [Session] 不支持的 cookies 类型: {type(cookies)}")
        return session

    session.headers.update({
        "User-Agent": "Mozilla/5.0",
        "Accept": "*/*",
    })

    if headers:
        session.headers.update(headers)
        print("📎 [Session] 已合并自定义 headers")

    print("✅ [Session] Session 构建完成")
    return session



# ==================================================
# 对外统一签到入口（带参数完整性检查）
# ==================================================

def perform_token_checkin(
    cookies: dict,
    account_name: str,
    checkin_url: str = None,
    main_site: str = None,
    headers=None,
):
    print("=" * 60)
    print(f"🚀 [{account_name}] perform_token_checkin 入口")

    # ---------- 参数完整性检查 ----------
    missing = []

    if not cookies:
        missing.append("cookies")
    if not account_name:
        missing.append("account_name")
    if not checkin_url:
        missing.append("checkin_url")
    if not main_site:
        missing.append("main_site")

    if missing:
        print("❗❗❗ 参数不完整警告 ❗❗❗")
        print(f"❌ 缺失参数: {', '.join(missing)}")
        print("⚠ 本次签到流程已跳过（不会发送任何请求）")
        print("=" * 60)
        return False, f"参数不完整: {', '.join(missing)}"

    # ---------- 参数打印 ----------
    print(f"👤 account_name = {account_name}")
    print(f"🔗 checkin_url  = {checkin_url}")
    print(f"🏠 main_site   = {main_site}")
    print(f"🍪 cookies 数量 = {len(cookies)}")

    # ---------- 构建 Session ----------
    session = session_from_cookies(cookies, headers=headers)

    # ---------- 执行签到 ----------
    result = perform_checkin(
        session=session,
        account_name=account_name,
        checkin_url=checkin_url,
        main_site=main_site,
    )

    print(f"🏁 [{account_name}] perform_token_checkin 结束 -> {result}")
    return result


# ==================================================
# 签到主流程
# ==================================================

def perform_checkin(session, account_name, checkin_url, main_site):
    print(f"\n🎯 [{account_name}] 开始签到流程")

    try:
        # 1️⃣ 直接访问签到页
        print(f"➡️ [STEP1] GET {checkin_url}")
        resp = session.get(checkin_url, timeout=30)
        print(f"⬅️ [STEP1] HTTP {resp.status_code}")

        if resp.status_code == 200:
            ok, msg = analyze_and_checkin(
                session, resp.text, checkin_url, account_name
            )
            print(f"📊 [STEP1] 解析结果: {ok}, {msg}")
            if ok:
                return True, msg

        # 2️⃣ API fallback
        print("🔁 [STEP2] 尝试 API fallback")
        api_endpoints = [
            f"{checkin_url}/api/checkin",
            f"{checkin_url}/checkin",
            f"{main_site}/api/checkin",
            f"{main_site}/checkin",
        ]

        for ep in api_endpoints:
            print(f"➡️ [API] GET {ep}")
            try:
                r = session.get(ep, timeout=30)
                print(f"⬅️ [API] GET {r.status_code}")
                if r.status_code == 200:
                    ok, msg = check_checkin_response(r.text)
                    print(f"📊 [API] GET 解析: {ok}, {msg}")
                    if ok:
                        return True, msg
            except Exception as e:
                print(f"⚠ [API] GET 异常: {e}")

            print(f"➡️ [API] POST {ep}")
            try:
                r = session.post(ep, data={"checkin": "1"}, timeout=30)
                print(f"⬅️ [API] POST {r.status_code}")
                if r.status_code == 200:
                    ok, msg = check_checkin_response(r.text)
                    print(f"📊 [API] POST 解析: {ok}, {msg}")
                    if ok:
                        return True, msg
            except Exception as e:
                print(f"⚠ [API] POST 异常: {e}")

        print("❌ 所有签到方式均失败")
        return False, "所有签到方式均失败"

    except Exception as e:
        print(f"🔥 签到流程异常: {e}")
        return False, f"签到异常: {e}"


# ==================================================
# 页面分析与辅助函数
# ==================================================

def analyze_and_checkin(session, html, page_url, account_name):
    print(f"🔍 [{account_name}] analyze_and_checkin")

    if already_checked_in(html):
        print("✅ 检测到已签到")
        return True, "今日已签到"

    if not is_checkin_page(html):
        print("❌ 当前页面不是签到页")
        return False, "非签到页面"

    data = {
        "checkin": "1",
        "action": "checkin",
        "daily": "1",
    }

    token = extract_csrf_token(html)
    if token:
        print(f"🔐 提取 CSRF Token: {token[:8]}***")
        data["_token"] = token
        data["csrf_token"] = token
    else:
        print("⚠ 未发现 CSRF Token，继续尝试")

    print(f"📤 POST {page_url} | data={list(data.keys())}")
    r = session.post(page_url, data=data, timeout=30)
    print(f"⬅️ POST 返回 {r.status_code}")

    if r.status_code == 200:
        return check_checkin_response(r.text)

    return False, "POST 签到失败"


def already_checked_in(html):
    print("🔎 [Check] 是否已签到")
    content = html.lower()
    keys = [
        "already checked in", "今日已签到",
        "checked in today", "已完成签到",
        "attendance recorded"
    ]
    return any(k in content for k in keys)


def is_checkin_page(html):
    print("🔎 [Check] 是否签到页面")
    content = html.lower()
    keys = ["check-in", "checkin", "签到", "attendance", "daily"]
    return any(k in content for k in keys)


def extract_csrf_token(html):
    print("🔎 [Check] 提取 CSRF Token")
    patterns = [
        r'name=["\']_token["\'][^>]*value=["\']([^"\']+)["\']',
        r'name=["\']csrf_token["\'][^>]*value=["\']([^"\']+)["\']',
        r'<meta[^>]*name=["\']csrf-token["\'][^>]*content=["\']([^"\']+)["\']',
    ]
    for p in patterns:
        m = re.search(p, html, re.IGNORECASE)
        if m:
            print("✅ CSRF Token 命中")
            return m.group(1)
    print("❌ 未命中 CSRF Token")
    return None


def check_checkin_response(html):
    print("📥 [Check] 解析签到返回")
    content = html.lower()

    success_words = [
        "check-in successful", "签到成功",
        "attendance recorded", "earned reward",
        "success", "成功", "completed"
    ]

    if any(w in content for w in success_words):
        print("🎉 命中成功关键字")
        patterns = [
            r"获得奖励[^\d]*(\d+\.?\d*)",
            r"earned.*?(\d+\.?\d*)",
            r"(\d+\.?\d*)\s*(credits?|points?|元)",
        ]
        for p in patterns:
            m = re.search(p, html, re.IGNORECASE)
            if m:
                return True, f"签到成功，获得 {m.group(1)}"
        return True, "签到成功"

    print("❌ 未检测到成功标志")
    return False, "签到返回失败"
