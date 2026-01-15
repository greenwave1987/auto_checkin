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
class ConfigReader:
    """
    加密配置文件读取器
    功能：
    - 使用 CONFIG_PASSWORD 解密 config.enc
    - 提供 get_value(key) 获取配置项
    """
    def __init__(self, password: str = None, config_file: str = None):
        # 1️⃣ 密码
        self.password = password or os.getenv("CONFIG_PASSWORD", "").strip()
        if not self.password:
            raise RuntimeError("❌ 未设置 CONFIG_PASSWORD")
        
        # 2️⃣ 配置文件路径
        current_dir = Path(__file__).resolve().parent
        self.config_file = Path(config_file) if config_file else current_dir / "config.enc"
        if not self.config_file.exists():
            raise FileNotFoundError(f"❌ 找不到配置文件: {self.config_file}")

        # 3️⃣ 解密配置
        encrypted_content = self.config_file.read_text(encoding="utf-8").strip()
        try:
            self.config = self._decrypt_json(encrypted_content)
            print("✅ 配置解密成功")
        except ValueError as e:
            print(f"❌ 配置解密失败: {e}")
            raise

    # ===============================
    # 私有方法：派生 AES key
    # ===============================
    def _derive_key(self) -> bytes:
        return sha256(self.password.encode()).digest()

    # ===============================
    # 私有方法：解密 AES-GCM + base64 JSON
    # ===============================
    def _decrypt_json(self, encrypted_str: str) -> dict:
        try:
            key = self._derive_key()
            raw = base64.b64decode(encrypted_str)

            if len(raw) < 13:
                raise ValueError("加密数据格式错误")

            nonce = raw[:12]
            ciphertext = raw[12:]

            aesgcm = AESGCM(key)
            plaintext = aesgcm.decrypt(nonce, ciphertext, None)

            return json.loads(plaintext.decode("utf-8"))
        except Exception as e:
            raise ValueError(f"解密失败: {e}")

    # ===============================
    # 公有方法：获取配置项
    # ===============================
    def get_value(self, key: str):
        info = self.config.get(key, "")
        if not info:
            raise RuntimeError(f"❌ 配置文件中不存在 {key}")

        description = info.get("description", "")
        print(f"ℹ️ 已读取 {key}: {description}")
        return info.get("value", "")


# ==================================================
# GitHub Secret 回写与读取
# ==================================================
class SecretUpdater:
    def __init__(self, name: str):
        self.name = name
        print(f"🔐 初始化 SecretUpdater，secret = {self.name}")

    # ==================================================
    # 回写 GitHub Secret
    # ==================================================
    def update(self, value):
        """
        value 可以是字符串，也可以是 dict/list
        """
        print("📝 准备回写 GitHub Secret")

        if not REPO or not REPO_TOKEN:
            print("⚠ 未配置 GITHUB_REPOSITORY / REPO_TOKEN，跳过回写")
            return False

        headers = {
            "Authorization": f"Bearer {REPO_TOKEN}",
            "Accept": "application/vnd.github+json",
        }

        # 1️⃣ 获取公钥
        print(f"🌐 获取仓库公钥: {REPO}")
        r = requests.get(
            f"https://api.github.com/repos/{REPO}/actions/secrets/public-key",
            headers=headers,
            timeout=30,
        )
        r.raise_for_status()
        key = r.json()

        # 2️⃣ 如果是 dict/list 自动 JSON 化
        if isinstance(value, (dict, list)):
            value_to_store = json.dumps(value)
        else:
            value_to_store = str(value)

        # 3️⃣ 加密
        print("🔑 加密 Secret")
        pk = public.PublicKey(key["key"].encode(), encoding.Base64Encoder())
        encrypted = public.SealedBox(pk).encrypt(value_to_store.encode())

        # 4️⃣ 提交
        print(f"📤 提交 Secret: {self.name}")
        r = requests.put(
            f"https://api.github.com/repos/{REPO}/actions/secrets/{self.name}",
            headers=headers,
            json={
                "encrypted_value": base64.b64encode(encrypted).decode(),
                "key_id": key["key_id"],
            },
            timeout=30,
        )

        if r.status_code not in (201, 204):
            raise RuntimeError(
                f"❌ Secret 回写失败 HTTP {r.status_code}: {r.text}"
            )

        print("✅ Secret 回写成功")
        return True

    # ==================================================
    # 从环境变量加载 Secret
    # ==================================================
    def load(self):
        raw = os.getenv(self.name)
        if not raw:
            print("ℹ️ 未检测到 Secret，首次运行")
            return None  # 没有数据返回 None

        # 尝试 JSON 解析
        try:
            return json.loads(raw)
        except (json.JSONDecodeError, TypeError):
            # 解析失败说明是普通字符串
            return raw

# ==================================================
# Session 工厂
# ==================================================
def session_from_cookies(cookies, headers=None, proxy_url=None):
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

    # ---------- 仅新增：requests 代理 ----------
    if proxy_url:
        session.proxies.update({
            "http": proxy_url,
            "https": proxy_url,
        })
        session.trust_env = False
        print(f"🌐 [Session] 使用代理: {proxy_url}")

    print("✅ [Session] Session 构建完成")
    return session


# ==================================================
# 对外统一签到入口
# ==================================================
def perform_token_checkin(
    cookies: dict,
    account_name: str,
    checkin_url: str = None,
    main_site: str = None,
    headers=None,
    proxy_url=None,
):
    print("=" * 60)
    print(f"🚀 [{account_name}] perform_token_checkin 入口")

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

    print(f"👤 account_name = {account_name}")
    print(f"🔗 checkin_url  = {checkin_url}")
    print(f"🏠 main_site   = {main_site}")
    print(f"🍪 cookies 数量 = {len(cookies)}")

    session = session_from_cookies(
        cookies,
        headers=headers,
        proxy_url=proxy_url,
    )

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
